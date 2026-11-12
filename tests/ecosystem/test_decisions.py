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
