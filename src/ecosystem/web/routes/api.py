"""Read-only JSON for the scheduled Claude review sessions (scripts/portal.py). Same login as the pages; decisions
still go through the normal audited, CSRF-checked form posts (/review/{id}/decide)."""
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ... import budget, models as m, notify
from ...assistant import allowed_choices, card_text, report, to_action
from ...decisions import suggest, summaries
from ..deps import admin_user, csrf_token, current_user, get_db, store_for

router = APIRouter(prefix="/api")


def _run(r: m.JobRun) -> dict:
    return {"id": r.id, "job": r.job, "status": r.status, "started_at": r.started_at, "ended_at": r.ended_at,
            "cost_usd": float(r.cost_usd or 0), "claims_written": r.claims_written, "errors": (r.errors or [])[-5:],
            "error_count": len(r.errors or []), "result": (r.args or {}).get("result"),
            "started_by": (r.args or {}).get("started_by")}


@router.get("/csrf")
def csrf(request: Request, user=Depends(current_user)):
    return {"csrf": csrf_token(request), "user": user.username, "role": user.role}


@router.get("/status")
def status(db: Session = Depends(get_db), user=Depends(current_user)):
    runs = list(db.scalars(select(m.JobRun).order_by(m.JobRun.id.desc()).limit(30)))
    return {"budgets": budget.summary(db), "runs": [_run(r) for r in runs],
            "schedules": [{"job": s.job, "enabled": s.enabled, "every_hours": s.every_hours, "params": s.params,
                           "last_run_at": s.last_run_at, "last_status": s.last_status, "next_run_at": s.next_run_at}
                          for s in db.scalars(select(m.JobSchedule).order_by(m.JobSchedule.job))],
            "open_by_kind": dict(db.execute(select(m.ReviewItem.kind, func.count()).where(m.ReviewItem.status == "open")
                                            .group_by(m.ReviewItem.kind)).all())}


@router.get("/report")
def report_json(hours: int = 24, db: Session = Depends(get_db), user=Depends(current_user)):
    r = report(db, max(1, min(hours, 24 * 31)))
    brief = lambda it: {"id": it.id, "kind": it.kind, "title": it.title, "status": it.status,
                        "decided_by": it.decided_by, "note": it.note}
    return {"since": r["since"], "runs": [_run(x) for x in r["runs"]], "problems": [_run(x) for x in r["problems"]],
            "by_assistant": [brief(i) for i in r["by_assistant"]], "by_people": [brief(i) for i in r["by_people"]],
            "open_total": r["open_total"], "recommended_open": r["recommended_open"], "spend": r["spend"],
            "budgets": r["budgets"]}


@router.get("/review")
def review_json(kind: str = "", limit: int = 50, offset: int = 0, db: Session = Depends(get_db),
                user=Depends(current_user)):
    q = select(m.ReviewItem).where(m.ReviewItem.status == "open")
    if kind:
        q = q.where(m.ReviewItem.kind == kind)
    total = db.scalar(select(func.count()).select_from(q.subquery()))
    items = list(db.scalars(q.order_by(m.ReviewItem.id).limit(max(1, min(limit, 200))).offset(max(offset, 0))))
    summ = summaries(db, {i for it in items for i in it.entity_ids})
    st = store_for(db, user)
    out = []
    for it in items:
        sug = suggest(st, it, summ)
        out.append({
            "id": it.id, "kind": it.kind, "title": it.title, "entity_ids": list(it.entity_ids),
            "card": card_text(db, it, summ),
            "suggestion": sug, "assistant": (it.payload or {}).get("assistant"),
            "actions": {c: to_action(it, c) for c in sorted(allowed_choices(it)) if to_action(it, c)},
        })
    return {"total": total, "offset": offset, "items": out}


@router.post("/notifications")
def post_notification(level: str = Form(...), category: str = Form("daily_review"), title: str = Form(...),
                      body: str = Form(""), link: str = Form(""), key: str = Form(""), db: Session = Depends(get_db),
                      user=Depends(admin_user)):
    """Used by the daily Claude review to put its report and the items that need the owner in the app."""
    try:
        n = notify.add(db, category, level, title, body or None, link or None, source=user.username,
                       dedupe_key=f"{user.username}:{key}" if key else None)
    except ValueError as e:
        raise HTTPException(400, str(e))
    store_for(db, user).audit("notification.add", "notification", n.id, None, {"title": n.title, "level": n.level})
    return {"id": n.id}
