"""Request dependencies: database session, current user, admin check, CSRF."""
import hmac
import re
import secrets

from markupsafe import Markup
from pathlib import Path
from urllib.parse import urlencode

from fastapi import Depends, Request, HTTPException
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
    # the markets this user may see, in the app's order (US first)
    request.state.markets = [mk for mk in m.MARKETS if mk in (user.markets or [])] or ["US"]
    if user.role in m.REVIEW_ROLES:
        from ..notify import unread_count
        request.state.unread = unread_count(db, user.username)  # the header bell
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


def admin_view(user: m.AppUser = Depends(current_user)) -> m.AppUser:
    """For admin-only pages (GET): Admin, Review, Report, Notifications, Guide and the JSON API."""
    if user.role != "admin":
        raise Forbidden("This page is for admins only.")
    return user


def reviewer_view(user: m.AppUser = Depends(current_user)) -> m.AppUser:
    """Review, Report, Notifications and the Guide: admins and reviewers."""
    if user.role not in m.REVIEW_ROLES:
        raise Forbidden("This page is for admins and reviewers.")
    return user


def reviewer_user(request: Request, user: m.AppUser = Depends(current_user), _=Depends(check_csrf)) -> m.AppUser:
    """Review decisions (POST): admins and reviewers, with a valid CSRF token. Logged under the user's name."""
    if user.role not in m.REVIEW_ROLES:
        raise Forbidden("Only admins and reviewers can decide review items.")
    return user


def admin_user(request: Request, user: m.AppUser = Depends(current_user), _=Depends(check_csrf)) -> m.AppUser:
    """For every state-changing POST: must be an admin and send a valid CSRF token."""
    if user.role != "admin":
        raise Forbidden("Only admins can change data. Ask an admin for edit access.")
    return user


def store_for(db: Session, user: m.AppUser) -> Store:
    return Store(db, actor=user.username)


def current_market(request: Request) -> str:
    """The market the user is looking at (header switch), kept in the session. US until they switch."""
    allowed = allowed_markets(request)
    mk = request.session.get("market", allowed[0])
    return mk if mk in allowed else allowed[0]


def allowed_markets(request: Request) -> list[str]:
    return getattr(getattr(request, "state", None), "markets", None) or list(m.MARKETS)


def require_market(request: Request, *orgs) -> None:
    """Refuse (as not found) an organisation in a market this user can't see."""
    allowed = allowed_markets(request)
    if any(o is not None and o.market not in allowed for o in orgs):
        raise HTTPException(404, "No such organisation")


def render(request: Request, template: str, user: m.AppUser | None, **ctx):
    ctx.setdefault("flash", request.session.pop("flash", None))
    ctx.setdefault("market", current_market(request))
    ctx.setdefault("markets", {k: v for k, v in m.MARKETS.items() if k in allowed_markets(request)})
    ctx.setdefault("all_markets", m.MARKETS)
    return templates.TemplateResponse(request, template, {"user": user, "csrf": csrf_token(request), **ctx})


def flash(request: Request, message: str) -> None:
    request.session["flash"] = message


def qs(params: dict, **changes) -> str:
    """Query string with some parameters changed; empty values dropped."""
    merged = {**params, **changes}
    return "?" + urlencode({k: v for k, v in merged.items() if v not in (None, "", False) and k != "market"})


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
        if "value" in v and "quote" in v:  # researched fact: show the value, then the supporting quote
            return f"{v['value']} (“{v['quote'][:160]}”)" if v.get("quote") else str(v["value"])
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


def note_text(text: str | None) -> Markup:
    """Notification text: escaped, then **bold**, "- " lines as a list, and links to pages inside the app only."""
    import html as _html
    out, in_list = [], False
    for line in (text or "").splitlines():
        t = _html.escape(line)
        t = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", t)
        t = re.sub(r"(https://usa-prospecting-production\.up\.railway\.app)?(/(?:review|admin|org|report|lenders|supply|map|leverage|notifications)[^\s<,)]*)",
                   lambda mm: f'<a href="{mm.group(2)}">{mm.group(2) if not mm.group(1) else "open"}</a>', t)
        if line.lstrip().startswith(("- ", "• ")):
            if not in_list:
                out.append("<ul>"); in_list = True
            item = re.sub(r"^\s*(-|•) ", "", t)
            out.append(f"<li>{item}</li>")
        else:
            if in_list:
                out.append("</ul>"); in_list = False
            out.append(f"<p>{t}</p>" if t.strip() else "")
    if in_list:
        out.append("</ul>")
    return Markup("".join(out))


templates.env.filters["note"] = note_text
templates.env.filters["val"] = fmt_value
templates.env.globals["qs"] = qs
