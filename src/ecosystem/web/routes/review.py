"""Review inbox: possible matches, duplicates, unverified research, parse failures. Every decision needs a person."""
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import case, func, select, text
from sqlalchemy.orm import Session

from ... import models as m
from ..deps import admin_user, current_user, flash, get_db, render, store_for
from .home import REVIEW_LABELS

router = APIRouter()
PAGE = 50


def _summaries(db: Session, ids: set[int]) -> dict:
    if not ids:
        return {}
    rows = db.execute(text("""
        SELECT o.id, o.display_name, o.lifecycle, o.kind, o.merged_into_id, p.display_name AS parent,
               (SELECT string_agg(hostname, ', ' ORDER BY hostname) FROM domain d WHERE d.organisation_id = o.id) AS domains,
               (SELECT string_agg(DISTINCT role, ', ') FROM org_role r WHERE r.organisation_id = o.id) AS roles,
               ours.status AS our_status, ours.role AS our_role,
               (SELECT string_agg(DISTINCT c.value #>> '{}', ', ') FROM claim c WHERE c.entity_id = o.id
                  AND c.attribute = 'cfpb_company_name') AS cfpb_name,
               (SELECT count(*) FROM claim c WHERE c.entity_id = o.id) AS facts
        FROM organisation o LEFT JOIN organisation p ON p.id = o.parent_id
        LEFT JOIN our_relationship ours ON ours.organisation_id = o.id
        WHERE o.id = ANY(:ids)"""), {"ids": list(ids)})
    return {r.id: r for r in rows}


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
    return render(request, "review.html", user, items=items, kind=kind, status=status, counts=counts, labels=REVIEW_LABELS,
                  summaries=summaries, total=total, page=page, pages=(total - 1) // PAGE + 1 if total else 1)


@router.post("/review/{item_id}/decide")
def decide(item_id: int, request: Request, action: str = Form(...), note: str = Form(""), db: Session = Depends(get_db),
           user=Depends(admin_user)):
    it = db.get(m.ReviewItem, item_id)
    if it is None or it.status != "open":
        raise HTTPException(404, "No open review item with that id")
    st = store_for(db, user)
    ids = list(it.entity_ids)
    done = ""
    if action.startswith("merge:"):
        keep = int(action.split(":")[1])
        if keep not in ids:
            raise HTTPException(400, "Merge target must be one of the item's organisations")
        for other in ids:
            if other != keep:
                st.merge(keep, other, note=f"review #{it.id}: {note}")
        it.status, done = "approved", f"Merged into {db.get(m.Organisation, keep).display_name}."
    elif action.startswith("link:"):
        parent, child = (int(x) for x in action.split(":")[1:3])
        if {parent, child} - set(ids):
            raise HTTPException(400, "Link must be between the item's organisations")
        src = m.Source(source_type="manual", title=f"Review #{it.id} decided by {user.username}",
                       retrieved_at=datetime.now(timezone.utc))
        db.add(src)
        db.flush()
        pname = st.org(parent).display_name
        c = st.claim(child, "owner_group", {"group": pname, "relation": "owns", "note": note or None}, src,
                     confidence="high", method=f"review:{user.username}")
        st.edge(st.org(parent).id, st.org(child).id, "owns", "confirmed", c)
        child_org = st.org(child)
        if child_org.parent_id is None:
            child_org.parent_id = st.org(parent).id
        st.audit("organisation.link", "organisation", child_org.id, None, {"parent": pname, "note": note})
        it.status, done = "approved", f"Linked: {pname} owns {child_org.display_name}."
    elif action == "approve":
        it.status, done = "approved", "Approved."
    elif action == "reject":
        it.status, done = "rejected", "Rejected."
    else:
        raise HTTPException(400, "Unknown action")
    it.decided_by, it.decided_at, it.note = user.username, datetime.now(timezone.utc), note.strip() or None
    st.audit(f"review.{it.status}", "review_item", it.id, None, {"action": action, "note": note, "title": it.title})
    flash(request, f"#{it.id}: {done}")
    return RedirectResponse(f"/review?kind={it.kind}", status_code=303)
