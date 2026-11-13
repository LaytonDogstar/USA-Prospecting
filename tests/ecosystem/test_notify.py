"""Notifications: raised by jobs, budgets and the review summary; idempotent; read per person. Made-up names."""
from datetime import datetime, timezone

from ecosystem import budget, models as m, notify


def test_add_is_idempotent_and_reappears_when_changed(session):
    a = notify.add(session, "review", "action", "3 items waiting", "x", "/review", dedupe_key="review:d1")
    notify.mark_read(session, "boss")
    assert notify.unread_count(session, "boss") == 0
    b = notify.add(session, "review", "action", "3 items waiting", "x", "/review", dedupe_key="review:d1")
    assert a.id == b.id and notify.unread_count(session, "boss") == 0   # same text: stays read
    notify.add(session, "review", "action", "5 items waiting", "x", "/review", dedupe_key="review:d1")
    assert notify.unread_count(session, "boss") == 1                     # changed: unread again
    assert notify.unread_count(session, "reader") == 1                   # read state is per person


def test_link_must_stay_inside_the_app(session):
    import pytest
    with pytest.raises(ValueError):
        notify.add(session, "review", "info", "x", link="https://evil.example/")


def test_job_problems_and_budget_forecast(session):
    run = m.JobRun(job="research", status="done", args={}, claims_written=3, cost_usd=1,
                   started_at=datetime.now(timezone.utc), errors=[{"message": "web search failed"}])
    session.add(run)
    session.flush()
    notify.after_job(session, run, "research", "done")
    notify.after_job(session, None, "ahrefs_fill_traffic", "skipped: AHREFS_API_KEY isn't set")
    budget.set_limit(session, "anthropic", 50, "t")
    budget.record(session, "anthropic", 0, 30, None)
    notify.budget_check(session, now=datetime(2026, 11, 10, 12, tzinfo=timezone.utc))
    titles = [n.title for n in notify.unread(session, "boss")]
    assert "Research finished with 1 error" in titles and "Ahrefs traffic didn't run" in titles
    assert any(t.startswith("Anthropic budget runs out around") for t in titles)


def test_review_summary_puts_partner_matches_first(store):
    a, b = store.new_org("Alder Loans"), store.new_org("Alder Loan Co")
    store.s.flush()
    store.review("possible_match", "pm:1", "Alder", [a.id, b.id], {"reasons": []})
    store.review("dedup", "d:1", "Alder dup", [a.id, b.id], {"reasons": []})
    notify.review_summary(store.s)
    n = notify.unread(store.s, "boss")[0]
    assert n.title == "2 review items waiting, 1 possible partner matches" and n.link == "/review?kind=possible_match"
