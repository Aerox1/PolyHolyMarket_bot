"""FastAPI admin dashboard.

Runs WITHOUT ``ENCRYPTION_KEY`` by design — it can read user/account metadata,
orders, trades and audit rows, and live positions via the public Data API, but
it cannot decrypt wallet keys.

Run:  uvicorn dashboard.app:app --host 0.0.0.0 --port 8877
"""

from __future__ import annotations

import logging
import secrets
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from core.config import settings
from core.logging import setup_logging

logger = logging.getLogger(__name__)

_STATIC_DIR = Path(__file__).resolve().parent / "static"


def create_app() -> FastAPI:
    setup_logging()
    app = FastAPI(title="Polymarket Bot Admin", docs_url=None, redoc_url=None)

    secret = settings.session_secret or secrets.token_urlsafe(32)
    if not settings.session_secret:
        logger.warning("SESSION_SECRET not set — using an ephemeral key (sessions reset on restart).")
    app.add_middleware(
        SessionMiddleware,
        secret_key=secret,
        https_only=settings.dashboard_cookie_secure,  # set DASHBOARD_COOKIE_SECURE=true behind TLS
        same_site="lax",
    )

    _STATIC_DIR.mkdir(parents=True, exist_ok=True)
    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

    # Serve the generated category card images (read-only; no key needed) so the
    # admin can preview them.
    from core.gemini import cards_dir
    app.mount("/cards", StaticFiles(directory=str(cards_dir())), name="cards")

    # Routers (each module exposes `router`).
    from dashboard import auth
    from dashboard.routers import pages

    app.include_router(auth.router)
    app.include_router(pages.router)

    @app.on_event("startup")
    def _bootstrap() -> None:
        try:
            from db.bootstrap import bootstrap_admin
            bootstrap_admin()
        except Exception as exc:  # noqa: BLE001 — never crash startup on bootstrap
            logger.warning("admin bootstrap skipped: %s", type(exc).__name__)

    return app


app = create_app()
