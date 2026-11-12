"""Web app tests on made-up data: login, roles, CSRF, tables, the two-click evidence trail, review decisions,
relationship and compliance edits, weights and re-scoring, people."""
import re

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from ecosystem import models as m
from ecosystem.auth import create_user
from ecosystem.importers import import_relationship_seed, import_targets, import_traffic
from ecosystem.matching import possible_match_sweep
from ecosystem.scoring import rescore
from ecosystem.store import Store
from ecosystem.web import app
from ecosystem.web.deps import get_db


@pytest.fixture
def web(session, migrated, targets_file, traffic_file, seed_file):
    st = Store(session, "test")
    import_targets(st, targets_file)
    import_traffic(st, traffic_file)
    import_relationship_seed(st, seed_file)
    possible_match_sweep(st)
    rescore(st)
    create_user(session, "boss", "admin-password-1", "admin")
    create_user(session, "reader", "viewer-password-1", "viewer")
    session.commit()

    def _db():
        s = sessionmaker(bind=migrated)()
        try:
            yield s
            s.commit()
        finally:
            s.close()

    app.dependency_overrides[get_db] = _db
    yield TestClient(app)
    app.dependency_overrides.clear()


def csrf(client, path="/"):
    return re.search(r'name="csrf" value="([^"]+)"', client.get(path).text).group(1)


def login(client, user="boss", pw="admin-password-1"):
    r = client.post("/login", data={"username": user, "password": pw, "next": "/", "csrf": csrf(client, "/login")},
                    follow_redirects=False)
    assert r.status_code == 303, r.text
    return client


def org_id(session, name):
    return session.scalar(select(m.Organisation.id).where(m.Organisation.display_name == name,
                                                          m.Organisation.lifecycle != "merged"))


def test_requires_login(web):
    assert web.get("/healthz").text == "ok"
    for path in ("/", "/lenders", "/supply", "/review", "/admin/weights", "/org/1"):
        r = web.get(path, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"].startswith("/login"), path


def test_login_and_home(web):
    r = web.post("/login", data={"username": "boss", "password": "nope", "next": "/", "csrf": csrf(web, "/login")})
    assert "Wrong username or password" in r.text
    login(web)
    r = web.get("/")
    assert r.status_code == 200 and "What matters now" in r.text
    # Prairie is the top unworked buyer, but an open possible match says it may already be a partner:
    # it is held back from the list, with a warning pointing to Review
    assert "Sunrise Lending" not in r.text and "held back" in r.text
    assert r.headers["X-Frame-Options"] == "DENY"


def test_open_redirect_blocked(web):
    r = web.post("/login", data={"username": "boss", "password": "admin-password-1", "next": "//evil.example",
                                 "csrf": csrf(web, "/login")}, follow_redirects=False)
    assert r.headers["location"] == "/"


def test_viewer_is_read_only(web, session):
    login(web, "reader", "viewer-password-1")
    assert web.get("/lenders").status_code == 200
    assert web.get("/admin/weights").status_code == 403
    oid = org_id(session, "Prairie Holdings, LLC")
    r = web.post(f"/org/{oid}/compliance", data={"value": "blocked", "note": "x", "csrf": csrf(web)})
    assert r.status_code == 403
    assert session.get(m.ComplianceStatus, oid) is None


def test_csrf_required(web, session):
    login(web)
    oid = org_id(session, "Prairie Holdings, LLC")
    r = web.post(f"/org/{oid}/compliance", data={"value": "blocked", "note": "x", "csrf": "wrong"})
    assert r.status_code == 403


def test_tables_and_csv(web):
    login(web)
    r = web.get("/lenders")
    assert "Prairie Holdings" in r.text and "Zephyr Loans" in r.text
    assert "Mega Bank" not in r.text  # screened out hidden by default
    assert "Prairie Holdings" not in web.get("/lenders?our=any").text
    assert "Rocket Bucks" in web.get("/supply?tier=B").text
    r = web.get("/supply?format=csv")
    assert r.headers["content-type"].startswith("text/csv")
    assert r.text.splitlines()[0].startswith("id,name,domain,types")


def test_any_field_traces_to_source_in_two_clicks(web, session):
    login(web)
    oid = org_id(session, "Prairie Holdings, LLC")
    page = web.get(f"/org/{oid}").text
    links = re.findall(r'class="ev" href="([^"]+)"', page)
    assert f"/org/{oid}/evidence/litigation_note" in links
    assert any(l.startswith("/evidence?ids=") for l in links)  # brands, domains, roles, metrics, score components
    # click 1: the drawer lists the claim and its sources
    drawer = web.get(f"/org/{oid}/evidence/litigation_note?partial=1").text
    assert "targets_20990101.xlsx › Targets › row 2" in drawer
    assert "https://news.example-times.com/story" in drawer
    # click 2: the spreadsheet row, with everything taken from it
    src = re.search(r'href="(/source/\d+)">targets_20990101', drawer).group(1)
    row = web.get(src).text
    assert "Everything taken from this row" in row and "rent-a-tribe" in row
    # every evidence link on the page resolves to at least one claim with a source
    for link in links:
        body = web.get(link + ("&" if "?" in link else "?") + "partial=1").text
        assert "Source" in body and "No claims found" not in body, link


def test_review_merge_moves_relationship(web, session):
    login(web)
    prairie = org_id(session, "Prairie Holdings, LLC")
    seed = org_id(session, "Quick Cash Now / Plains Finance")
    item = session.scalar(select(m.ReviewItem).where(m.ReviewItem.kind == "possible_match",
                                                     m.ReviewItem.entity_ids.contains([prairie, seed])))
    page = web.get("/review?kind=possible_match").text
    assert f'id="item-{item.id}"' in page
    r = web.post(f"/review/{item.id}/decide", data={"action": f"merge:{prairie}", "note": "same group", "csrf": csrf(web)},
                 follow_redirects=False)
    assert r.status_code == 303
    session.expire_all()
    assert session.get(m.ReviewItem, item.id).status == "approved"
    assert session.get(m.Organisation, seed).lifecycle == "merged"
    ours = session.scalar(select(m.OurRelationship).where(m.OurRelationship.organisation_id == prairie))
    assert ours.status == "contracting"
    assert session.scalar(select(m.AuditLog).where(m.AuditLog.action == "review.approved")).actor == "boss"
    # the merged record redirects to the kept one
    assert web.get(f"/org/{seed}", follow_redirects=False).headers["location"] == f"/org/{prairie}"


def test_review_link_and_reject(web, session):
    login(web)
    items = session.scalars(select(m.ReviewItem).where(m.ReviewItem.kind.in_(("dedup", "possible_match")),
                                                       m.ReviewItem.status == "open")).all()
    a, b = items[0].entity_ids
    web.post(f"/review/{items[0].id}/decide", data={"action": f"link:{a}:{b}", "note": "", "csrf": csrf(web)})
    session.expire_all()
    edge = session.scalar(select(m.Relationship).where(m.Relationship.from_id == a, m.Relationship.to_id == b,
                                                       m.Relationship.type == "owns"))
    assert edge.status == "confirmed"
    web.post(f"/review/{items[1].id}/decide", data={"action": "reject", "note": "different firms", "csrf": csrf(web)})
    session.expire_all()
    assert session.get(m.ReviewItem, items[1].id).note == "different firms"


def test_relationship_and_compliance_edits(web, session):
    login(web)
    oid = org_id(session, "Prairie Holdings, LLC")
    web.post(f"/org/{oid}/relationship", data={"role": "buyer", "status": "in_talks", "owner": "LB", "notes": "call booked",
                                                "needs_confirmation": "", "csrf": csrf(web)})
    session.expire_all()
    ours = session.scalar(select(m.OurRelationship).where(m.OurRelationship.organisation_id == oid))
    assert (ours.status, ours.source, ours.needs_confirmation, ours.notes) == ("in_talks", "manual", False, "call booked")
    web.post(f"/org/{oid}/compliance", data={"value": "blocked", "note": "rent-a-tribe exposure", "csrf": csrf(web)})
    assert "Prairie Holdings" not in web.get("/lenders").text  # blocked hidden from ranked views
    assert "Prairie Holdings" in web.get("/lenders?compliance=blocked").text
    web.post(f"/org/{oid}/compliance", data={"value": "review", "note": "lifted pending check", "csrf": csrf(web)})
    session.expire_all()
    assert len(session.scalars(select(m.ComplianceStatusHistory).where(m.ComplianceStatusHistory.entity_id == oid)).all()) == 2
    assert "Compliance review:" in web.get(f"/org/{oid}").text


def test_weights_versioned_and_rescore_logged(web, session):
    login(web)
    before = session.scalar(select(m.Score.value).join(m.Organisation, m.Organisation.id == m.Score.entity_id)
                            .where(m.Organisation.display_name == "Prairie Holdings, LLC", m.Score.score_type == "buyer_value"))
    data = {f"w_{k}": "0" for k in ("lead_buying", "size", "price_volume_fit", "type_fit", "state_overlap")}
    data.update(w_segment="10", note="segment only", csrf=csrf(web, "/admin/weights"))
    web.post("/admin/weights/buyer_value", data=data)
    web.post("/admin/rescore", data={"csrf": csrf(web, "/admin/weights")})
    session.expire_all()
    labels = session.scalars(select(m.WeightsVersion.label).where(m.WeightsVersion.score_type == "buyer_value")).all()
    assert labels == ["v1 (default)", "v2"]
    scores = session.scalars(select(m.Score).join(m.Organisation, m.Organisation.id == m.Score.entity_id)
                             .where(m.Organisation.display_name == "Prairie Holdings, LLC", m.Score.score_type == "buyer_value")
                             .order_by(m.Score.id)).all()
    assert len(scores) == 2 and float(scores[0].value) == float(before) and float(scores[1].value) == 100.0
    assert session.scalar(select(m.AuditLog).where(m.AuditLog.action == "scores.rescore", m.AuditLog.actor == "boss"))
    r = web.post("/admin/weights/buyer_value", data={"w_segment": "-1", "csrf": csrf(web, "/admin/weights")})
    assert "Not saved" in r.text


def test_people_add_and_delete(web, session):
    login(web)
    oid = org_id(session, "Zephyr Loans")
    web.post(f"/org/{oid}/people", data={"name": "Pat Example", "title": "Head of Partnerships",
                                          "role_category": "affiliate_partnerships", "source": "Sales Navigator (manual)",
                                          "csrf": csrf(web)})
    assert "Pat Example" in web.get("/people").text
    pid = session.scalar(select(m.Person.id).where(m.Person.name == "Pat Example"))
    web.post(f"/person/{pid}/delete", data={"reason": "deletion request", "csrf": csrf(web)})
    session.expire_all()
    assert session.get(m.Person, pid) is None
    assert session.scalar(select(m.AuditLog).where(m.AuditLog.action == "person.delete")).after["note"] == "deletion request"


def test_admin_users(web, session):
    login(web)
    web.post("/admin/users", data={"username": "newbie", "password": "long-enough-pass", "role": "viewer",
                                   "csrf": csrf(web, "/admin/users")})
    u = session.scalar(select(m.AppUser).where(m.AppUser.username == "newbie"))
    web.post(f"/admin/users/{u.id}", data={"action": "deactivate", "csrf": csrf(web, "/admin/users")})
    session.expire_all()
    assert session.get(m.AppUser, u.id).is_active is False


def test_reset_needs_typed_confirmation(web, session):
    login(web)
    before = session.scalar(select(func.count()).select_from(m.Organisation))
    r = web.post("/admin/reset", data={"confirm": "reset", "csrf": csrf(web, "/admin/imports")})
    assert "type RESET in capitals" in r.text
    session.expire_all()
    assert session.scalar(select(func.count()).select_from(m.Organisation)) == before
    page = web.get("/admin/imports").text
    assert "Start again" in page and "Rows imported per sheet" in page and "Never reset." in page


def test_map_and_state_pages(web, session):
    login(web)
    assert "Ecosystem map" in web.get("/map").text
    data = web.get("/map/data?view=all").json()
    assert data["nodes"] and "stats" in data
    assert any(n.get("legal") == "Prairie Holdings, LLC" for n in data["nodes"])
    # the map opens on prospects; an explicit "Any" is respected
    assert '<option value="none" selected>' in web.get("/map").text
    assert '<option value="none" selected>' not in web.get("/map?view=focus").text
    lev = web.get("/leverage").text
    assert "Owner groups" in lev and "Acme Ventures" not in lev  # owner groups come from the owner_groups file, not loaded here
    assert web.get("/map/data?view=all&expand=999999").status_code == 200
    page = web.get("/states").text
    assert "lenders known to serve each state" in page and 'href="/lenders?state=TX"' in page
    assert "Prairie Holdings" in web.get("/lenders?state=TX").text
    assert "Prairie Holdings" not in web.get("/lenders?state=NY").text
    assert web.get("/static/vendor/cytoscape-3.30.4.min.js").status_code == 200


def test_possible_partner_safeguards(web, session):
    login(web)
    r = web.get("/lenders?our=none").text
    assert "may already be a partner" in r
    prairie = org_id(session, "Prairie Holdings, LLC")
    page = web.get(f"/org/{prairie}").text
    assert "May already be a partner" in page
    # the relationship form is closed by default: no accidental saves
    assert "<details><summary>Add a relationship</summary>" in page


def test_recent_activity_folds_repeats(web, session):
    from ecosystem.web.views import recent_activity
    acts = recent_activity(session)
    assert all(not (a["action"] == b["action"] and a["actor"] == b["actor"]) for a, b in zip(acts, acts[1:]))
    assert any(a["count"] > 1 for a in acts)


def test_admin_research_page(web, session, monkeypatch):
    login(web)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("AHREFS_API_KEY", raising=False)
    page = web.get("/admin/research").text
    assert "Monthly budgets" in page and "not set" in page
    tok = csrf(web, "/admin/research")
    r = web.post("/admin/research/run", data={"job_name": "research", "n": "5", "csrf": tok})
    assert "ANTHROPIC_API_KEY isn&#39;t set" in r.text
    web.post("/admin/research/budget", data={"anthropic": "150", "ahrefs": "90000", "csrf": csrf(web, "/admin/research")})
    session.expire_all()
    assert float(session.get(m.ProviderBudget, "anthropic").monthly_limit) == 150
    web.post("/admin/research/keywords", data={"action": "seed", "csrf": csrf(web, "/admin/research")})
    assert "bad credit loans" in web.get("/admin/research").text
    # viewers can't reach it
    web.post("/logout", data={"csrf": csrf(web)})
    login(web, "reader", "viewer-password-1")
    assert web.get("/admin/research").status_code == 403


def test_review_bulk_and_schedule_forms(web, session):
    login(web)
    r = web.get("/review?kind=possible_match")
    assert r.status_code == 200
    r = web.post("/review/bulk", data={"kind": "dedup", "csrf": csrf(web, "/review")}, follow_redirects=True)
    assert r.status_code == 200 and "suggested decisions" in r.text
    page = web.get("/admin/research")
    assert "Schedules" in page.text and "Upkeep" in page.text
    r = web.post("/admin/research/schedule", data={"job": "upkeep", "enabled": "1", "every_hours": "24",
                                                    "action": "save", "csrf": csrf(web, "/admin/research")}, follow_redirects=False)
    assert r.status_code == 303
    session.expire_all()
    from ecosystem import models as m
    sc = session.get(m.JobSchedule, "upkeep")
    assert sc.enabled and sc.next_run_at is not None
    r = web.post("/admin/research/schedule", data={"job": "research", "every_hours": "24", "p_n": "9999",
                                                    "action": "save", "csrf": csrf(web, "/admin/research")})
    session.expire_all()
    assert session.get(m.JobSchedule, "research").params == {"n": 200}


def test_report_and_recommendation_on_card(web, session):
    from ecosystem import models as m
    login(web)
    assert web.get("/report").status_code == 200
    it = session.scalar(__import__("sqlalchemy").select(m.ReviewItem).where(m.ReviewItem.status == "open"))
    it.payload = {**it.payload, "assistant": {"choice": "unsure", "action": None, "label": "Needs a person",
                                              "confidence": "low", "why": "Thin evidence.", "key": "x"}}
    session.commit()
    page = web.get(f"/review?kind={it.kind}").text
    assert "Assistant recommends" in page and "Thin evidence." in page
    assert "Needs a person" in web.get("/report").text


def test_bulk_add_keywords(web, session):
    from sqlalchemy import select
    from ecosystem import models as m
    login(web)
    r = web.post("/admin/research/keywords", data={"action": "add", "csrf": csrf(web, "/admin/research"),
                                                   "keyword": "Payday Loans Texas\n\n$1000 loan, cash advance apps\npayday loans texas"},
                 follow_redirects=True)
    assert "Added 3 keywords." in r.text
    assert set(session.scalars(select(m.TrackedKeyword.keyword))) == {"payday loans texas", "$1000 loan", "cash advance apps"}
    r = web.post("/admin/research/keywords", data={"action": "add", "csrf": csrf(web, "/admin/research"),
                                                   "keyword": "cash advance apps\nsame day loans"}, follow_redirects=True)
    assert "Added 1 keyword; 1 already tracked" in r.text
