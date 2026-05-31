"""Telegram Mini App service (FastAPI).

Holds ENCRYPTION_KEY (it signs real bets), so it is a keyed service like the bot
— SEPARATE from the key-less admin dashboard. Serves:
  * /api/*   — categories, markets, bets (Telegram initData auth)
  * /cards/* — cached Gemini category images
  * /        — the built React Mini App (SPA)

Run:  uvicorn webapp.app:app --host 0.0.0.0 --port 8888
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from core.config import settings
from core.gemini import cards_dir
from core.logging import setup_logging
from db.engine import async_session_factory
from db.repositories.accounts import DbCredentialStore
from polymarket.account_manager import AccountManager
from webapp.routers import api

logger = logging.getLogger(__name__)

_FRONTEND_DIST = Path(__file__).resolve().parent / "frontend" / "dist"


def create_app() -> FastAPI:
    setup_logging()
    app = FastAPI(title="Polymarket Mini App", docs_url=None, redoc_url=None)

    # Per-user signing client factory (this process has ENCRYPTION_KEY).
    app.state.account_manager = AccountManager(DbCredentialStore(async_session_factory()))

    app.include_router(api.router)

    # Cached Gemini category images.
    cdir = cards_dir()
    app.mount("/cards", StaticFiles(directory=str(cdir)), name="cards")

    @app.on_event("startup")
    async def _startup() -> None:
        # Best-effort: refresh categories + fill images within budget, in the
        # background so a slow/unavailable upstream never blocks boot.
        async def _bg() -> None:
            try:
                from webapp import sync
                await sync.sync_categories()
                await sync.generate_pending_images()
            except Exception as exc:  # noqa: BLE001
                logger.warning("startup category sync skipped: %s", type(exc).__name__)
        asyncio.create_task(_bg())

    # The built React SPA (mounted LAST so /api and /cards win). Guard if not built.
    if _FRONTEND_DIST.exists():
        app.mount("/", StaticFiles(directory=str(_FRONTEND_DIST), html=True), name="spa")
    else:
        @app.get("/")
        async def _no_build() -> JSONResponse:
            return JSONResponse(
                {"status": "ok", "note": "Mini App frontend not built yet. Run the Vite build in webapp/frontend."}
            )
        logger.warning("Frontend build not found at %s — serving API only.", _FRONTEND_DIST)

    return app


app = create_app()
