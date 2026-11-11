"""Scheduled jobs: research, Ahrefs traffic and search snapshots, and nightly upkeep run by themselves.

Every schedule is off until an admin turns it on (Admin → Research & budgets). Each run goes through the same job
functions as a manual run, so budgets are checked before every paid call and every run is logged in job_run.
Results are still only *proposed* claims and review items: a schedule never decides anything.

The app checks for due schedules every few minutes in a background thread. A Postgres advisory lock makes sure
only one copy runs a schedule, even if Railway briefly runs two instances during a deploy.
"""
import logging
import os
import threading
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from . import models as m
from .store import Store

log = logging.getLogger("ecosystem.scheduler")
LOCK_KEY = 482_901_337  # arbitrary, constant: the advisory lock id for the scheduler
CHECK_EVERY_SECONDS = 300

# job → (label, default every_hours, default params, env var it needs)
DEFAULTS = {
    "research": ("Research the next companies (Claude)", 24, {"n": 40}, "ANTHROPIC_API_KEY"),
    "ahrefs_fill_traffic": ("Fill website traffic (Ahrefs)", 24, {"limit": 100}, "AHREFS_API_KEY"),
    "ahrefs_serp_snapshot": ("Search results snapshot for tracked keywords (Ahrefs)", 168, {}, "AHREFS_API_KEY"),
    "upkeep": ("Upkeep: duplicate check, partner check, re-score (free)", 24, {}, None),
}
LIMITS = {"n": (1, 200), "limit": (1, 500)}


def now() -> datetime:
    return datetime.now(timezone.utc)


def ensure_defaults(s) -> list[m.JobSchedule]:
    have = {r.job: r for r in s.scalars(select(m.JobSchedule))}
    for job, (_, hours, params, _) in DEFAULTS.items():
        if job not in have:
            have[job] = m.JobSchedule(job=job, enabled=False, every_hours=hours, params=dict(params),
                                      updated_by="system", updated_at=now())
            s.add(have[job])
    s.flush()
    return [have[j] for j in DEFAULTS]


def clean_params(job: str, params: dict) -> dict:
    out = {}
    for k, v in params.items():
        if k in LIMITS and k in DEFAULTS[job][2]:
            lo, hi = LIMITS[k]
            out[k] = max(lo, min(int(v), hi))
    return {**DEFAULTS[job][2], **out}


def upkeep(store: Store) -> dict:
    """Free housekeeping: new shared-website/name duplicates, the full existing-partner check, and a re-score."""
    from .jobs import job
    from .matching import name_dedup_suggestions, possible_match_sweep, shared_key_suggestions
    from .scoring import rescore
    with job(store.s, "upkeep", {}, store.actor) as ctx:
        out = {"shared_key_suggestions": shared_key_suggestions(store), "name_dedup": name_dedup_suggestions(store),
               "possible_match_sweep": possible_match_sweep(store), "rescore": rescore(store, audit=False)}
        ctx.run.args = {**ctx.run.args, "result": out}
    return out


def run_job(s, job: str, actor: str, kwargs: dict) -> None:
    from . import enrich, research
    fn = {"ahrefs_fill_traffic": enrich.fill_traffic, "ahrefs_serp_snapshot": enrich.serp_snapshot,
          "research": research.research_batch, "upkeep": upkeep}[job]
    fn(Store(s, actor=actor), **kwargs)


def run_schedule(s, sched: m.JobSchedule, actor: str = "scheduler") -> str:
    """Run one schedule now and move its next run on. Returns the status recorded."""
    need = DEFAULTS[sched.job][3]
    started = now()
    if need and not os.environ.get(need):
        status = f"skipped: {need} isn't set"
    elif s.scalar(select(m.JobRun.id).where(m.JobRun.job == sched.job, m.JobRun.status == "running")):
        status = "skipped: already running"
    else:
        failed = None
        try:
            run_job(s, sched.job, actor, clean_params(sched.job, sched.params or {}))
        except Exception as e:  # the run itself is logged as failed in job_run
            log.exception("scheduled %s failed", sched.job)
            failed = f"failed: {type(e).__name__}"
            if isinstance(e, DBAPIError):
                s.rollback()
                sched = s.get(m.JobSchedule, sched.job)
        run = s.scalar(select(m.JobRun).where(m.JobRun.job == sched.job, m.JobRun.started_at >= started)
                       .order_by(m.JobRun.id.desc()))
        status = run.status if run else (failed or "done")
        sched.last_run_id = run.id if run else None
    sched.last_run_at, sched.last_status = started, status
    sched.next_run_at = started + timedelta(hours=max(1, sched.every_hours))
    s.flush()
    return status


def run_due(session_factory, at: datetime | None = None) -> list[tuple[str, str]]:
    """Run every enabled schedule that is due. Safe to call from several processes: one holds the lock."""
    at = at or now()
    done = []
    s = session_factory()
    try:
        if not s.scalar(text("SELECT pg_try_advisory_lock(:k)"), {"k": LOCK_KEY}):
            return done
        try:
            ensure_defaults(s)
            s.commit()
            due = [r.job for r in s.scalars(select(m.JobSchedule).where(m.JobSchedule.enabled.is_(True)))
                   if r.next_run_at is None or r.next_run_at <= at]
            for job in due:
                sched = s.get(m.JobSchedule, job)
                status = run_schedule(s, sched)
                s.commit()  # each job's results are kept even if a later one fails
                done.append((job, status))
                log.info("scheduled %s: %s", job, status)
        finally:
            s.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": LOCK_KEY})
            s.commit()
    finally:
        s.close()
    return done


_thread: threading.Thread | None = None
_stop = threading.Event()


def start(session_factory) -> None:
    """Start the background checker once per process (no-op if it is already running)."""
    global _thread
    if _thread and _thread.is_alive():
        return

    def loop():
        _stop.wait(60)  # let the app finish starting first
        while not _stop.is_set():
            try:
                run_due(session_factory)
            except Exception:
                log.exception("scheduler check failed")
            _stop.wait(CHECK_EVERY_SECONDS)

    _thread = threading.Thread(target=loop, name="ecosystem-scheduler", daemon=True)
    _thread.start()
    log.info("scheduler started (checks every %ss)", CHECK_EVERY_SECONDS)


def enabled_by_env() -> bool:
    v = os.environ.get("ECOSYSTEM_SCHEDULER")
    return v == "on" if v else os.environ.get("ECOSYSTEM_ENV") == "production"
