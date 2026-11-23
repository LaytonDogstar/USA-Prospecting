"""Suggested review decisions and bulk apply (made-up names)."""
from sqlalchemy import select

from ecosystem import models as m
from ecosystem.decisions import apply_safe, decide, suggest, summaries


def _item(store, kind, a, b, reasons, **extra):
    return store.review(kind, f"{kind}:{a.id}:{b.id}", f"{a.display_name} / {b.display_name}", [a.id, b.id],
                        {"reasons": reasons, **extra})


def _sug(store, it):
    return suggest(store, it, summaries(store.s, set(it.entity_ids)))


def test_suggestions_and_safe_tiers(store):
    a = store.new_org("Zephyr Loans LLC")
    b = store.new_org("Zephyr Loans")
    c = store.new_org("Orchid Cash")
    d = store.new_org("Different Name Co")
    store.s.flush()
    same = _item(store, "dedup", a, b, ["name similarity 0.95"])
    other = _item(store, "dedup", c, d, ["name similarity 0.91"])
    sug = _sug(store, same)
    assert sug["safe"] and sug["action"].startswith("merge:")
    assert _sug(store, other) is None

    rel = store.new_org("Kestrel Credit")
    look = store.new_org("Kestrel Credit Group")
    store.s.flush()
    store.set_our_relationship(rel.id, role="buyer", status="live", source="manual")
    hand = _item(store, "possible_match", look, rel, ["Matched buyer in the spreadsheet"])
    assert _sug(store, hand) is None or not _sug(store, hand)["safe"]  # judgement, never bulk


def test_research_suggestions(store):
    o = store.new_org("Nimbus App")
    store.s.flush()
    app = store.review("research", f"research:{o.id}", "Research: Nimbus App", [o.id],
                       {"lender_category": "fintech_app", "is_us_consumer_lender": False, "counts": {"proposed": 2}})
    assert _sug(store, app)["action"] == "screen" and _sug(store, app)["safe"]
    lender = store.new_org("Basalt Loans")
    store.s.flush()
    thin = store.review("research", f"research:{lender.id}", "Research: Basalt", [lender.id],
                        {"lender_category": "tribal", "is_us_consumer_lender": True, "counts": {"proposed": 4},
                         "rejected_uncited": 1})
    assert _sug(store, thin) is None  # a finding was dropped: a person looks


def test_bulk_applies_only_safe_and_logs(store):
    a = store.new_org("Quill Funding Inc")
    b = store.new_org("Quill Funding")
    c = store.new_org("Larch Money")
    d = store.new_org("Unrelated Finance")
    store.s.flush()
    safe = _item(store, "dedup", a, b, ["name similarity 0.96"])
    unsure = _item(store, "dedup", c, d, ["name similarity 0.90"])
    out = apply_safe(store, "dedup")
    assert out["applied"] == 1 and out["left_for_you"] == 1
    assert safe.status == "approved" and safe.note.startswith("bulk:") and unsure.status == "open"
    assert {a.lifecycle, b.lifecycle} & {"merged"}
    logs = [r.action for r in store.s.scalars(select(m.AuditLog))]
    assert "review.approved" in logs and "review.bulk" in logs


def test_decide_rejects_bad_targets(store):
    import pytest
    from ecosystem.decisions import DecisionError
    a, b, c = store.new_org("Alpha Test"), store.new_org("Beta Test"), store.new_org("Gamma Test")
    store.s.flush()
    it = _item(store, "dedup", a, b, ["x"])
    with pytest.raises(DecisionError):
        decide(store, it, f"merge:{c.id}")
    decide(store, it, "reject", "different")
    with pytest.raises(DecisionError):
        decide(store, it, "reject")


def test_failed_research_is_never_screened_out_and_can_be_undone(store):
    from ecosystem.decisions import undo_screen
    o = store.new_org("Granite Loans")
    store.s.flush()
    empty = store.review("research", f"research:{o.id}:1", "Research found 0 facts: Granite Loans", [o.id],
                         {"lender_category": "unclear", "is_us_consumer_lender": False, "counts": {}})
    sug = _sug(store, empty)
    assert sug["action"] == "reject" and sug["safe"]  # rejected so it's researched again, never screened
    thin = store.review("research", f"research:{o.id}:2", "Research found 1 fact", [o.id],
                        {"lender_category": "fintech_app", "is_us_consumer_lender": False, "counts": {"description": 1}})
    assert _sug(store, thin)["action"] == "screen" and not _sug(store, thin)["safe"]  # one fact: a person checks
    # a wrong screen-out (as happened before this rule) can be undone
    decide(store, thin, "screen", "test")
    assert store.org(o.id).lifecycle == "screened_out"
    undo_screen(store, thin, "was a lender")
    assert store.org(o.id).lifecycle == "candidate" and thin.status == "rejected"


def test_debt_relief_is_a_buyer_not_screened_out(store):
    from ecosystem.decisions import decide, suggest
    from ecosystem.research import restore_debt_relief, screen_out
    from ecosystem.scoring import rescore
    org = store.new_org("Wren Debt Help")
    store.s.flush()
    src = store.url_source("https://www.wrendebthelp.example/about", "site_read")
    for attr, val in (("description", "Debt settlement programme for US consumers"), ("website", "wrendebthelp.example")):
        c = store.claim(org.id, attr, {"value": val, "quote": val}, src, confidence="high", method="research:x:v1",
                        status="proposed")
        c.job_run_id = 7
    it = store.review("research", f"research:{org.id}:7", "Research found 2 facts", [org.id],
                      {"run": 7, "counts": {"description": 1, "website": 1}, "lender_category": "debt_relief",
                       "is_us_consumer_lender": False, "summary": "A debt settlement company."})
    s = suggest(store, it, {})
    assert s["action"] == "approve" and s["safe"]
    decide(store, it, "approve", "test")
    roles = {r.role for r in store.s.scalars(select(m.OrgRole).where(m.OrgRole.organisation_id == org.id))}
    assert roles == {"debt_relief"}
    rescore(store, org_ids={org.id}, audit=False)
    assert store.s.scalar(select(m.Score.value).where(m.Score.entity_id == org.id, m.Score.score_type == "buyer_value"))

    # one screened out earlier as "not a lender" is put back and sent to the researchers
    old = store.new_org("Teal Credit Counselling")
    store.s.flush()
    it2 = store.review("research", f"research:{old.id}:8", "Research found 2 facts", [old.id],
                       {"run": 8, "counts": {"description": 2}, "lender_category": "not_a_lender",
                        "summary": "A non-profit credit counselling agency."})
    it2.status = "approved"
    screen_out(store, old, "Not a US consumer lender (research)")
    store.s.flush()
    out = restore_debt_relief(store)
    assert out == {"restored": 1, "asked": 1}
    store.s.refresh(old)
    assert old.lifecycle != "screened_out"
    assert restore_debt_relief(store) == {"restored": 0, "asked": 0}
