from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from ... import models as m
from ...auth import authenticate
from ..deps import check_csrf, current_user, get_db, render

router = APIRouter()


def _safe_next(nxt: str | None) -> str:
    return nxt if nxt and nxt.startswith("/") and not nxt.startswith("//") else "/"


@router.get("/login")
def login_form(request: Request, next: str | None = None):
    return render(request, "login.html", None, next=_safe_next(next), error=None)


@router.post("/login")
def login(request: Request, username: str = Form(...), password: str = Form(...), next: str = Form("/"),
          db: Session = Depends(get_db), _=Depends(check_csrf)):
    user = authenticate(db, username, password)
    db.add(m.AuditLog(actor=username.strip().lower()[:80], action="login.success" if user else "login.failure"))
    if not user:
        return render(request, "login.html", None, next=_safe_next(next),
                      error="Wrong username or password, or the account is locked for 15 minutes after 5 failed attempts.")
    request.session.clear()
    request.session["uid"] = user.id
    return RedirectResponse(_safe_next(next), status_code=303)


@router.post("/logout")
def logout(request: Request, user=Depends(current_user), _=Depends(check_csrf)):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)
