"""Schedules: off by default, run when due, skip without a key, move their next run on."""
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from ecosystem import models as m, scheduler


def test_schedules_off_by_default_and_upkeep_runs(session, migrated, monkeypatch):
    rows = scheduler.ensure_defaults(session)
    assert {r.job for r in rows} == set(scheduler.DEFAULTS) and not any(r.enabled for r in rows)
    session.commit()
    factory = sessionmaker(bind=migrated)
    assert scheduler.run_due(factory) == []  # nothing on, nothing runs

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    for job in ("upkeep", "research"):
        r = session.get(m.JobSchedule, job)
        r.enabled, r.next_run_at = True, scheduler.now() - timedelta(minutes=1)
    session.commit()
    done = dict(scheduler.run_due(factory))
    assert done["upkeep"] == "done"
    assert done["research"].startswith("skipped") and "ANTHROPIC_API_KEY" in done["research"]
    session.expire_all()
    up = session.get(m.JobSchedule, "upkeep")
    assert up.next_run_at > scheduler.now() + timedelta(hours=23) and up.last_run_id
    assert session.scalar(select(m.JobRun.status).where(m.JobRun.job == "upkeep")) == "done"
    assert scheduler.run_due(factory) == []  # not due again yet


def test_params_are_clamped():
    assert scheduler.clean_params("research", {"n": "5000"}) == {"n": 200}
    assert scheduler.clean_params("research", {"bogus": 1}) == {"n": 40}
