"""Review inbox: possible matches, duplicates, unverified research, parse failures. Every decision needs a person."""
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import case, func, select, text
from sqlalchemy.orm import Session

from ... import models as m
from ...decisions import DecisionError, apply_safe, decide as decide_item, suggest, summaries
from ..deps import admin_user, current_user, flash, get_db, render, store_for
from .home import REVIEW_LABELS

router = APIRouter()
PAGE = 50


def _summaries(db: Session, ids: set[int]) -> dict:
    return summaries(db, ids)


@router.get("/review")
def inbox(request: Request, kind: str = "", status: str = "open", page: int = 1, db: Session = Depends(get_db),
          user=Depends(current_user)):
    counts = dict(db.execute(select(m.ReviewItem.kind, func.count()).where(m.ReviewItem.status == "open")
                             .group_by(m.ReviewItem.kind)).all())
    kind = kind if kind in REVIEW_LABELS else next((k for k in REVIEW_LABELS if counts.get(k)), "possible_match")
    q = select(m.ReviewItem).where(m.ReviewItem.kind == kind)
    q = q.where(m.ReviewItem.status == "open") if status == "open" else q.where(m.ReviewItem.status != "open")
    total = db.scalar(select(func.count()).select_from(q.subquery()))
    # strongest evidence first: shared domain/name, then the spreadsheet's hand matches, then weak lookalikes
    strength = case((m.ReviewItem.payload["strength"].astext == "strong", 0),
                    (m.ReviewItem.payload["strength"].astext == "weak", 2), else_=1)
    order = [strength, m.ReviewItem.id] if status == "open" else [m.ReviewItem.decided_at.desc()]
    items = list(db.scalars(q.order_by(*order).limit(PAGE).offset((max(page, 1) - 1) * PAGE)))
    summaries = _summaries(db, {i for it in items for i in it.entity_ids})
    proposed = {}
    for it in items:
        if it.kind == "research" and it.entity_ids:
            proposed[it.id] = [
                (c, db.get(m.Source, c.source_id)) for c in db.scalars(select(m.Claim).where(
                    m.Claim.entity_id == it.entity_ids[0], m.Claim.job_run_id == it.payload.get("run"),
                    m.Claim.method.like("research:%")).order_by(m.Claim.attribute, m.Claim.id))]
    st = store_for(db, user)
    suggestions = {it.id: suggest(st, it, summaries) for it in items} if status == "open" else {}
    safe_on_tab = 0
    if status == "open":  # one summaries query for the whole tab, not one per item
        tab = list(db.scalars(select(m.ReviewItem).where(m.ReviewItem.kind == kind, m.ReviewItem.status == "open")))
        tab_summ = _summaries(db, {i for it in tab for i in it.entity_ids})
        safe_on_tab = sum(1 for it in tab if (sug := suggest(st, it, tab_summ)) and sug["safe"])
    return render(request, "review.html", user, items=items, kind=kind, status=status, counts=counts, labels=REVIEW_LABELS,
                  summaries=summaries, proposed=proposed, total=total, page=page, suggestions=suggestions,
                  safe_on_tab=safe_on_tab, pages=(total - 1) // PAGE + 1 if total else 1)


@router.post("/review/{item_id}/decide")
def decide(item_id: int, request: Request, action: str = Form(...), note: str = Form(""), db: Session = Depends(get_db),
           user=Depends(admin_user)):
    it = db.get(m.ReviewItem, item_id)
    if it is None or it.status != "open":
        raise HTTPException(404, "No open review item with that id")
    try:
        done = decide_item(store_for(db, user), it, action, note)
    except DecisionError as e:
        raise HTTPException(400, str(e))
    flash(request, f"#{it.id}: {done}")
    return RedirectResponse(f"/review?kind={it.kind}", status_code=303)


@router.post("/review/bulk")
def bulk(request: Request, kind: str = Form(...), db: Session = Depends(get_db), user=Depends(admin_user)):
    if kind not in REVIEW_LABELS:
        raise HTTPException(400, "Unknown tab")
    out = apply_safe(store_for(db, user), kind)
    msg = f"Applied {out['applied']} suggested decisions; {out['left_for_you']} left for you to decide."
    if out["errors"]:
        msg += f" {len(out['errors'])} couldn't be applied: {'; '.join(out['errors'][:3])}"
    flash(request, msg)
    return RedirectResponse(f"/review?kind={kind}", status_code=303)
