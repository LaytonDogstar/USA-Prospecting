import logging
import os
import secrets
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from .deps import Forbidden, NotAuthenticated, templates

HERE = Path(__file__).parent
log = logging.getLogger("ecosystem.web")


def _secret_key() -> str:
    key = os.environ.get("SECRET_KEY")
    if key:
        return key
    if os.environ.get("ECOSYSTEM_ENV") == "production":
        raise RuntimeError("SECRET_KEY must be set in production")
    log.warning("SECRET_KEY not set: using a random key, so sessions end when the app restarts")
    return secrets.token_urlsafe(32)


def create_app() -> FastAPI:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    production = os.environ.get("ECOSYSTEM_ENV") == "production"
    app = FastAPI(title="Ecosystem", docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(SessionMiddleware, secret_key=_secret_key(), session_cookie="eco_session",
                       max_age=12 * 3600, same_site="lax", https_only=production)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        resp = await call_next(request)
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Referrer-Policy"] = "same-origin"
        resp.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self'; frame-ancestors 'none'")
        if production:
            resp.headers["Strict-Transport-Security"] = "max-age=31536000"
        return resp

    @app.exception_handler(NotAuthenticated)
    async def _login(request: Request, exc: NotAuthenticated):
        return RedirectResponse(f"/login?next={request.url.path}", status_code=303)

    @app.exception_handler(Forbidden)
    async def _forbidden(request: Request, exc: Forbidden):
        return templates.TemplateResponse(request, "error.html", {"user": None, "message": str(exc)}, status_code=403)

    @app.get("/healthz", include_in_schema=False)
    def healthz():
        return PlainTextResponse("ok")

    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")

    from .routes import admin, auth, entity, home, maps, review, tables
    for module in (auth, home, tables, maps, entity, review, admin):
        app.include_router(module.router)

    from .. import scheduler
    if scheduler.enabled_by_env():
        @app.on_event("startup")
        def _start_scheduler():
            from sqlalchemy.orm import sessionmaker
            from ..db import engine
            scheduler.start(sessionmaker(bind=engine(), future=True))
    return app
