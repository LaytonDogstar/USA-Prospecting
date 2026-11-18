"""Notifications: the header bell, /notifications, and marking them read. Anyone signed in sees them; marking read
is per person (it doesn't hide them from anyone else)."""
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import any_, literal, select
from sqlalchemy.orm import Session

from ... import models as m, notify
from ..deps import admin_user, check_csrf, current_user, get_db, render

router = APIRouter()


@router.get("/notifications")
def notifications(request: Request, db: Session = Depends(get_db), user=Depends(current_user)):
    notify.auto_resolve(db)
    unread = notify.unread(db, user.username)
    earlier = list(db.scalars(select(m.Notification).where(notify.OPEN, literal(user.username) == any_(m.Notification.read_by))
                              .order_by(m.Notification.created_at.desc()).limit(50)))
    resolved = list(db.scalars(select(m.Notification).where(m.Notification.resolved_at.is_not(None))
                               .order_by(m.Notification.resolved_at.desc()).limit(30)))
    return render(request, "notifications.html", user, unread=unread, earlier=earlier, resolved=resolved)


@router.post("/notifications/{nid}/resolve")
def resolve_one(nid: int, request: Request, note: str = Form(""), db: Session = Depends(get_db), user=Depends(admin_user)):
    n = db.get(m.Notification, nid)
    if n is not None:
        notify.resolve(db, n, user.username, note or "marked resolved")
    return RedirectResponse("/notifications", status_code=303)


@router.post("/notifications/read")
def mark_read(request: Request, nid: int = Form(0), go: str = Form(""), db: Session = Depends(get_db),
              user=Depends(current_user), _=Depends(check_csrf)):
    notify.mark_read(db, user.username, [nid] if nid else None)
    target = go if go.startswith("/") and not go.startswith("//") else "/notifications"
    return RedirectResponse(target, status_code=303)
