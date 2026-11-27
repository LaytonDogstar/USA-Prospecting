"""Review inbox: possible matches, duplicates, unverified research, parse failures. Every decision needs a person."""
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import case, func, select, text
from sqlalchemy.orm import Session

from ... import models as m
from ...decisions import DecisionError, apply_safe, decide as decide_item, suggest, summaries, undo_screen
from ..deps import reviewer_user, reviewer_view, require_market, current_market, admin_view, admin_user, current_user, flash, get_db, render, store_for
from .home import REVIEW_LABELS

router = APIRouter()
PAGE = 50


def _summaries(db: Session, ids: set[int]) -> dict:
    return summaries(db, ids)


@router.get("/review")
def inbox(request: Request, kind: str = "", status: str = "open", page: int = 1, db: Session = Depends(get_db),
          user=Depends(reviewer_view)):
    from ..views import REVIEW_IN_MARKET
    in_market = text(REVIEW_IN_MARKET).bindparams(market=current_market(request))
    counts = dict(db.execute(select(m.ReviewItem.kind, func.count()).where(m.ReviewItem.status == "open", in_market)
                             .group_by(m.ReviewItem.kind)).all())
    kind = kind if kind in REVIEW_LABELS else next((k for k in REVIEW_LABELS if counts.get(k)), "possible_match")
    q = select(m.ReviewItem).where(m.ReviewItem.kind == kind, in_market)
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
    from ...research import linked_answers
    followups = {it.id: linked_answers(db, it.id) for it in items}
    st = store_for(db, user)
    suggestions = {it.id: suggest(st, it, summaries) for it in items} if status == "open" else {}
    safe_on_tab = 0
    if status == "open":  # one summaries query for the whole tab, not one per item
        tab = list(db.scalars(select(m.ReviewItem).where(m.ReviewItem.kind == kind, m.ReviewItem.status == "open",
                                                         in_market)))
        tab_summ = _summaries(db, {i for it in tab for i in it.entity_ids})
        safe_on_tab = sum(1 for it in tab if (sug := suggest(st, it, tab_summ)) and sug["safe"])
    return render(request, "review.html", user, items=items, kind=kind, status=status, counts=counts, labels=REVIEW_LABELS,
                  summaries=summaries, proposed=proposed, total=total, page=page, suggestions=suggestions,
                  followups=followups,
                  safe_on_tab=safe_on_tab, pages=(total - 1) // PAGE + 1 if total else 1)


@router.post("/review/{item_id}/decide")
def decide(item_id: int, request: Request, action: str = Form(...), note: str = Form(""), db: Session = Depends(get_db),
           user=Depends(reviewer_user)):
    it = db.get(m.ReviewItem, item_id)
    if it is None or it.status != "open":
        raise HTTPException(404, "No open review item with that id")
    require_market(request, *(db.get(m.Organisation, i) for i in it.entity_ids))
    if user.role == "reviewer" and len(note.strip()) < 3:
        flash(request, f"#{it.id}: add a short reason in 'Note (why)' before deciding; it goes in the team decisions log.")
        return RedirectResponse(f"/review?kind={it.kind}#item-{it.id}", status_code=303)
    try:
        done = decide_item(store_for(db, user), it, action, note)
    except DecisionError as e:
        raise HTTPException(400, str(e))
    flash(request, f"#{it.id}: {done}")
    return RedirectResponse(f"/review?kind={it.kind}", status_code=303)


@router.post("/review/bulk")
def bulk(request: Request, kind: str = Form(...), db: Session = Depends(get_db), user=Depends(reviewer_user)):
    if kind not in REVIEW_LABELS:
        raise HTTPException(400, "Unknown tab")
    out = apply_safe(store_for(db, user), kind, market=current_market(request))
    msg = f"Applied {out['applied']} suggested decisions; {out['left_for_you']} left for you to decide."
    if out["errors"]:
        msg += f" {len(out['errors'])} couldn't be applied: {'; '.join(out['errors'][:3])}"
    flash(request, msg)
    return RedirectResponse(f"/review?kind={kind}", status_code=303)


@router.post("/review/{item_id}/undo-screen")
def undo_screen_route(item_id: int, request: Request, note: str = Form(""), db: Session = Depends(get_db),
                      user=Depends(reviewer_user)):
    it = db.get(m.ReviewItem, item_id)
    if it is None:
        raise HTTPException(404, "No review item with that id")
    require_market(request, *(db.get(m.Organisation, i) for i in it.entity_ids))
    try:
        done = undo_screen(store_for(db, user), it, note)
    except DecisionError as e:
        raise HTTPException(400, str(e))
    flash(request, f"#{it.id}: {done}")
    return RedirectResponse("/review?kind=research&status=done", status_code=303)


@router.post("/review/{item_id}/ask")
def ask_route(item_id: int, request: Request, questions: str = Form(...), investigate: str = Form("record"),
              mode: str = Form("ownership"), db: Session = Depends(get_db), user=Depends(reviewer_user)):
    """Send a card back to the researchers with specific questions; the answers come back on this card."""
    from ...research import RequestRefused, ask
    it = db.get(m.ReviewItem, item_id)
    if it is None or it.status != "open":
        raise HTTPException(404, "No open review item with that id")
    require_market(request, *(db.get(m.Organisation, i) for i in it.entity_ids))
    ids = list(it.entity_ids)
    target = {"left": ids[0] if ids else None, "right": ids[1] if len(ids) > 1 else None}.get(investigate, ids[0] if ids else None)
    try:
        req = ask(store_for(db, user), target, questions.splitlines(), mode, review_item=it,
                  context=f"{user.username} is deciding card #{it.id} ({it.title})")
    except RequestRefused as e:
        raise HTTPException(400, str(e))
    flash(request, f"#{it.id}: sent to the researchers ({len(req.questions)} question{'s' if len(req.questions) != 1 else ''}). "
                   "The answers come back on this card after the next research run.")
    return RedirectResponse(f"/review?kind={it.kind}#item-{it.id}", status_code=303)
