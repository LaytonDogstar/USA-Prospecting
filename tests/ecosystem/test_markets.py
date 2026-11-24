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
