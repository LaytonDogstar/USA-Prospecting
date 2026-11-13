"""Notifications: tell people where to look and what to do.

Raised by the system (a job failed or hit its budget, a budget will run out before month end, review items are
waiting) and by the daily Claude review (its report and the items that need the owner, via /api/notifications).
Idempotent through dedupe_key: raising the same notice again updates it, and makes it unread again only if what it
says changed. Nothing here sends anything outside the app.
"""
from calendar import monthrange
from datetime import datetime, timezone

from sqlalchemy import any_, case, func, literal, select

from . import budget, models as m

LEVELS = ("problem", "action", "warning", "info")   # most urgent first
CATEGORIES = ("review", "job", "budget", "daily_review")
JOB_LABELS = {"research": "Research", "ahrefs_fill_traffic": "Ahrefs traffic", "ahrefs_serp_snapshot": "Search snapshot",
              "upkeep": "Upkeep", "assistant": "Assistant"}


def add(s, category: str, level: str, title: str, body: str | None = None, link: str | None = None,
        source: str = "system", dedupe_key: str | None = None) -> m.Notification:
    if level not in LEVELS or category not in CATEGORIES:
        raise ValueError("unknown level or category")
    if link and not link.startswith("/"):
        raise ValueError("link must be a path inside the app")
    title, body = title.strip()[:300], (body or "").strip()[:8000] or None
    n = s.scalar(select(m.Notification).where(m.Notification.dedupe_key == dedupe_key)) if dedupe_key else None
    if n is None:
        n = m.Notification(category=category, level=level, title=title, body=body, link=link, source=source,
                           dedupe_key=dedupe_key, read_by=[], created_at=datetime.now(timezone.utc))
        s.add(n)
    elif (n.title, n.body, n.level) != (title, body, level):
        n.title, n.body, n.level, n.link, n.read_by = title, body, level, link, []
        n.created_at = datetime.now(timezone.utc)
    s.flush()
    return n


def unread(s, username: str, limit: int = 100) -> list[m.Notification]:
    order = [case({lv: i for i, lv in enumerate(LEVELS)}, value=m.Notification.level, else_=len(LEVELS)),
             m.Notification.created_at.desc()]
    return list(s.scalars(select(m.Notification).where(~(literal(username) == any_(m.Notification.read_by)))
                          .order_by(*order).limit(limit)))


def unread_count(s, username: str) -> int:
    return s.scalar(select(func.count()).select_from(m.Notification)
                    .where(~(literal(username) == any_(m.Notification.read_by)))) or 0


def mark_read(s, username: str, ids: list[int] | None = None) -> int:
    q = select(m.Notification).where(~(literal(username) == any_(m.Notification.read_by)))
    if ids is not None:
        q = q.where(m.Notification.id.in_(ids))
    rows = list(s.scalars(q))
    for n in rows:
        n.read_by = list(n.read_by or []) + [username]
    s.flush()
    return len(rows)


def after_job(s, run: m.JobRun | None, job: str, status: str) -> None:
    """A notice when a scheduled job failed, stopped on its budget, was skipped, or logged errors."""
    label = JOB_LABELS.get(job, job)
    day = datetime.now(timezone.utc).date().isoformat()
    errors = len(run.errors or []) if run else 0
    if status == "failed":
        last = (run.errors or [{}])[-1].get("message", "") if run else ""
        add(s, "job", "problem", f"{label} failed", f"The run stopped with an error: {last[:400]}",
            "/admin/research", dedupe_key=f"job:{job}:{day}")
    elif status == "budget_stopped":
        add(s, "job", "problem", f"{label} stopped: monthly budget reached",
            "It will start again next month, or when the budget is raised under Monthly budgets.",
            "/admin/research", dedupe_key=f"job:{job}:{day}")
    elif status.startswith("skipped"):
        add(s, "job", "warning", f"{label} didn't run", status.replace("skipped: ", "").capitalize() + ".",
            "/admin/research", dedupe_key=f"job:{job}:{day}")
    elif errors:
        sample = "; ".join(e.get("message", "")[:120] for e in (run.errors or [])[:3])
        add(s, "job", "warning", f"{label} finished with {errors} error{'s' if errors != 1 else ''}",
            f"The rest of the run completed. First errors: {sample}", "/admin/research",
            dedupe_key=f"job:{job}:{day}")


def budget_check(s, now: datetime | None = None) -> None:
    """Warn when a provider's budget is mostly used, or on course to run out before the month ends."""
    now = now or datetime.now(timezone.utc)
    days_in_month = monthrange(now.year, now.month)[1]
    elapsed = max(now.day - 1 + now.hour / 24, 0.5)
    month = now.strftime("%Y-%m")
    for b in budget.summary(s):
        limit, used = float(b["limit"] or 0), float(b["used"] or 0)
        if limit <= 0 or used <= 0:
            continue
        forecast = used / elapsed * days_in_month
        fmt = (lambda v: f"${v:,.2f}") if b["unit"] == "usd" else (lambda v: f"{v:,.0f} units")
        name = b["provider"].capitalize()
        if used >= limit * 0.8 or forecast > limit:
            run_out = now.day + (limit - used) / max(used / elapsed, 1e-9)
            when = f"around {min(int(run_out), days_in_month)} {now.strftime('%b')}" if used < limit else "now"
            add(s, "budget", "warning" if used < limit else "problem",
                f"{name} budget runs out {when}",
                f"Used {fmt(used)} of {fmt(limit)} this month; at this pace the month needs about {fmt(forecast)}. "
                "Jobs stop cleanly at the cap. Raise the budget or lower the batch sizes if you want them to keep going.",
                "/admin/research", dedupe_key=f"budget:{b['provider']}:{month}")


def review_summary(s) -> None:
    """One notice a day with what is waiting in the review inbox, partner matches first."""
    from .web.routes.home import REVIEW_LABELS
    counts = dict(s.execute(select(m.ReviewItem.kind, func.count()).where(m.ReviewItem.status == "open")
                            .group_by(m.ReviewItem.kind)).all())
    total = sum(counts.values())
    day = datetime.now(timezone.utc).date().isoformat()
    if not total:
        return
    lines = [f"- {REVIEW_LABELS.get(k, k)}: {n}" for k, n in sorted(counts.items(), key=lambda kv: (kv[0] != "possible_match", -kv[1]))]
    pm = counts.get("possible_match", 0)
    add(s, "review", "action",
        f"{total} review item{'s' if total != 1 else ''} waiting" + (f", {pm} possible partner matches" if pm else ""),
        "Start with possible partner matches: those organisations are held back from prospect lists until decided.\n"
        + "\n".join(lines), "/review" + ("?kind=possible_match" if pm else ""), dedupe_key=f"review:{day}")


def daily(s) -> None:
    budget_check(s)
    review_summary(s)
