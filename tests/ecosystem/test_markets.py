"""US / Australia: one record per market, a header switch, no cross-market merges. Made-up names only."""
from sqlalchemy import select

from ecosystem import models as m
from ecosystem.decisions import DecisionError, decide
from ecosystem.importers import detect_kind, import_relationship_seed, seed_market
from ecosystem.matching import name_dedup_suggestions, possible_match_sweep
from ecosystem.store import Store

from conftest import write_csv
from test_web import csrf, login, web  # noqa: F401  (fixture)


def _au_seed(tmp_path):
    return write_csv(tmp_path / "au_partners.csv", ["name", "aliases", "domains", "role", "status", "notes", "market"], [
        ["Zephyr Loans", "", "", "buyer", "live", "AU buyer", "AU"],          # same name as a US buyer
        ["Wombat Credit", "", "wombatcredit.example.au", "buyer", "paused", "AU buyer, inactive", "AU"],
        ["Quokka Cash", "", "", "buyer", "live", "AU buyer", "AU"],
    ])


def test_au_seed_creates_au_records_beside_us_ones(store, tmp_path, seed_file):
    import_relationship_seed(store, seed_file)
    au = _au_seed(tmp_path)
    assert detect_kind(au) == "relationship_seed" and seed_market(au) == "AU" and seed_market(seed_file) == "US"
    out = import_relationship_seed(store, au)
    assert out["created"] == 3 and out["attached_existing"] == 0
    zephyrs = list(store.s.scalars(select(m.Organisation).where(m.Organisation.display_name == "Zephyr Loans")))
    assert sorted(z.market for z in zephyrs) == ["AU", "US"]
    assert store.market == "US"  # the importer puts the store back
    # a US company with an AU partner's exact name is flagged (same group?), a lookalike is not
    us_q = store.new_org("Quokka Cash")
    store.new_org("Quokka Cash Express Lending")
    store.s.flush()
    possible_match_sweep(store)
    items = list(store.s.scalars(select(m.ReviewItem).where(m.ReviewItem.kind == "possible_match")))
    cross = [i for i in items if us_q.id in i.entity_ids]
    assert len(cross) == 1 and any("other market" in r for r in cross[0].payload["reasons"])
    assert not any("Express" in i.title for i in items)
    # never merged across markets, and no duplicate suggestions across markets
    try:
        decide(store, cross[0], f"merge:{us_q.id}", "test")
        assert False, "cross-market merge should be refused"
    except DecisionError:
        pass
    name_dedup_suggestions(store)
    assert not store.s.scalar(select(m.ReviewItem.id).where(
        m.ReviewItem.kind == "dedup", m.ReviewItem.entity_ids.contains([z.id for z in zephyrs])))


def test_header_switch_filters_pages(web, session, tmp_path):
    import_relationship_seed(Store(session, "test"), _au_seed(tmp_path))
    session.commit()
    login(web, "reader", "viewer-password-1")  # viewers can switch too: it is a view, not an edit
    us = web.get("/lenders").text
    assert "Wombat Credit" not in us
    r = web.post("/market", data={"market": "AU", "back": "/supply?q=x", "csrf": csrf(web)}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/supply"
    home = web.get("/").text
    assert 'class="on"' in home and ">AU<" in home
    assert "Prairie Holdings" not in web.get("/lenders").text
    assert "Australian credit licences are national" in web.get("/states").text
    web.post("/market", data={"market": "XX", "back": "//evil.example", "csrf": csrf(web)})
    assert "Prairie Holdings" not in web.get("/lenders").text  # unknown market ignored
    web.post("/market", data={"market": "US", "back": "/", "csrf": csrf(web)})
    assert "Prairie" in web.get("/lenders?q=Prairie").text


def test_budgets_are_per_market(session):
    from ecosystem import budget
    budget.set_limit(session, "anthropic", 100, "test")
    assert budget.limit(session, "anthropic", "AU") == (0.0, "usd")  # nothing spent in a new market until set
    try:
        budget.check(session, "anthropic", 0.1, market="AU")
        assert False
    except budget.BudgetExceeded as e:
        assert "Australia" in str(e)
    budget.set_limit(session, "anthropic", 40, "test", market="AU")
    budget.record(session, "anthropic", 0, 30, None, market="AU")
    budget.record(session, "anthropic", 0, 5, None)
    assert budget.used(session, "anthropic", "AU") == 30 and budget.used(session, "anthropic") == 5
    rows = {(r["provider"], r["market"]): r for r in budget.summary(session)}
    assert rows[("anthropic", "AU")]["limit"] == 40 and rows[("anthropic", "US")]["limit"] == 100


def test_user_limited_to_one_market(web, session, tmp_path):
    from ecosystem.auth import create_user
    import_relationship_seed(Store(session, "test"), _au_seed(tmp_path))
    u = create_user(session, "aussie", "aussie-password-1", "admin")
    u.markets = ["AU"]
    session.commit()
    us_org = session.scalar(select(m.Organisation.id).where(m.Organisation.market == "US"))
    au_org = session.scalar(select(m.Organisation.id).where(m.Organisation.market == "AU"))
    login(web, "aussie", "aussie-password-1")
    home = web.get("/").text
    assert 'action="/market"' not in home and ">AU<" in home  # no switch, the AU chip instead
    assert "Prairie Holdings" not in web.get("/lenders?q=Prairie").text
    web.post("/market", data={"market": "US", "back": "/", "csrf": csrf(web)})
    assert "Prairie Holdings" not in web.get("/lenders?q=Prairie").text  # can't switch to a market they don't have
    assert web.get(f"/org/{us_org}").status_code == 404 and web.get(f"/org/{au_org}").status_code == 200
    r = web.post(f"/org/{us_org}/compliance", data={"value": "blocked", "note": "x", "csrf": csrf(web)})
    assert r.status_code == 404
    # an admin gives them the US too
    web.post("/logout", data={"csrf": csrf(web)})
    login(web)
    web.post(f"/admin/users/{u.id}", data={"action": "markets", "markets": ["AU", "US"], "csrf": csrf(web, "/admin/users")})
    session.expire_all()
    assert sorted(session.get(m.AppUser, u.id).markets) == ["AU", "US"]
    r = web.post(f"/admin/users/{u.id}", data={"action": "markets", "csrf": csrf(web, "/admin/users")}, follow_redirects=True)
    assert "choose at least one market" in r.text
