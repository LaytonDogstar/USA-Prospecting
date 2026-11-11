"""Request dependencies: database session, current user, admin check, CSRF."""
import hmac
import secrets
from pathlib import Path
from urllib.parse import urlencode

from fastapi import Depends, Request
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session, sessionmaker

from .. import models as m
from ..db import engine
from ..store import Store

templates = Jinja2Templates(directory=Path(__file__).parent / "templates")


class NotAuthenticated(Exception):
    pass


class Forbidden(Exception):
    pass


def get_db():
    s: Session = sessionmaker(bind=engine(), future=True)()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def current_user(request: Request, db: Session = Depends(get_db)) -> m.AppUser:
    uid = request.session.get("uid")
    user = db.get(m.AppUser, uid) if uid else None
    if user is None or not user.is_active:
        request.session.clear()
        raise NotAuthenticated()
    return user


def csrf_token(request: Request) -> str:
    tok = request.session.get("csrf")
    if not tok:
        tok = request.session["csrf"] = secrets.token_urlsafe(32)
    return tok


async def check_csrf(request: Request) -> None:
    form = await request.form()
    sent = str(form.get("csrf", ""))
    if not sent or not hmac.compare_digest(sent, request.session.get("csrf", "")):
        raise Forbidden("The form expired. Go back, reload the page and try again.")


def admin_user(request: Request, user: m.AppUser = Depends(current_user), _=Depends(check_csrf)) -> m.AppUser:
    """For every state-changing POST: must be an admin and send a valid CSRF token."""
    if user.role != "admin":
        raise Forbidden("Only admins can change data. Ask an admin for edit access.")
    return user


def store_for(db: Session, user: m.AppUser) -> Store:
    return Store(db, actor=user.username)


def render(request: Request, template: str, user: m.AppUser | None, **ctx):
    ctx.setdefault("flash", request.session.pop("flash", None))
    return templates.TemplateResponse(request, template, {"user": user, "csrf": csrf_token(request), **ctx})


def flash(request: Request, message: str) -> None:
    request.session["flash"] = message


def qs(params: dict, **changes) -> str:
    """Query string with some parameters changed; empty values dropped."""
    merged = {**params, **changes}
    return "?" + urlencode({k: v for k, v in merged.items() if v not in (None, "", False)})


def fmt_num(v) -> str:
    if v is None or v == "":
        return "—"
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v)
    if abs(f) >= 1_000_000:
        return f"{f / 1_000_000:.1f}M"
    if abs(f) >= 10_000:
        return f"{f / 1000:.0f}k"
    return f"{f:,.0f}" if f == int(f) else f"{f:,.1f}"


def fmt_value(v) -> str:
    """Claim values are JSON: render lists and dicts readably."""
    if isinstance(v, list):
        return ", ".join(fmt_value(x) for x in v)
    if isinstance(v, dict):
        if "name" in v and "domains" in v:
            return v["name"] + (f" ({', '.join(v['domains'])})" if v["domains"] else "")
        if "domain" in v:
            return v["domain"] + (f" (+{v['family_size'] - 1} sites)" if v.get("family_size", 1) > 1 else "")
        return "; ".join(f"{k}: {fmt_value(x)}" for k, x in v.items() if x not in (None, ""))
    if isinstance(v, bool):
        return "yes" if v else "no"
    if isinstance(v, float) and v == int(v):
        return f"{int(v):,}"
    return str(v)


templates.env.filters["num"] = fmt_num
templates.env.filters["val"] = fmt_value
templates.env.globals["qs"] = qs
