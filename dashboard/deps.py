"""Dashboard dependencies: DB session, admin auth, and i18n-aware rendering.

Auth uses a signed session cookie (Starlette SessionMiddleware) holding
``admin_id``. Passwords are argon2id (verified via ``core.crypto``). The
dashboard language is per-session (``dash_lang``), default English.
"""

from __future__ import annotations

import secrets
from collections.abc import Iterator
from pathlib import Path

from fastapi import Depends, Form, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from starlette.exceptions import HTTPException
from starlette.status import HTTP_303_SEE_OTHER, HTTP_400_BAD_REQUEST, HTTP_403_FORBIDDEN

from core.i18n import SUPPORTED, normalize_lang, t, text_dir
from db.engine import SessionLocal
from db.models import Admin

_TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))


# ── DB session ────────────────────────────────────────────────────────────────

def get_db() -> Iterator[Session]:
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


# ── auth ──────────────────────────────────────────────────────────────────────

def current_admin(request: Request, db: Session) -> Admin | None:
    admin_id = request.session.get("admin_id")
    if not admin_id:
        return None
    return db.get(Admin, int(admin_id))


def require_admin(request: Request, db: Session = Depends(get_db)) -> Admin:
    admin = current_admin(request, db)
    if admin is None:
        # Redirect browsers to the login page; fetch() callers see the redirect.
        raise HTTPException(status_code=HTTP_303_SEE_OTHER, headers={"Location": "/login"})
    return admin


def require_superadmin(request: Request, db: Session = Depends(get_db)) -> Admin:
    admin = require_admin(request, db)
    if not admin.is_superadmin:
        raise HTTPException(status_code=HTTP_403_FORBIDDEN, detail="Superadmin required")
    return admin


# ── i18n-aware rendering ───────────────────────────────────────────────────────

def dash_lang(request: Request) -> str:
    return normalize_lang(request.session.get("dash_lang", "en"))


# ── CSRF (double-submit via session) ──────────────────────────────────────────

def csrf_token(request: Request) -> str:
    tok = request.session.get("csrf")
    if not tok:
        tok = secrets.token_urlsafe(32)
        request.session["csrf"] = tok
    return tok


def verify_csrf(request: Request, csrf_token: str = Form("")) -> None:
    """Validate the CSRF token on state-changing POSTs."""
    expected = request.session.get("csrf")
    if not expected or not secrets.compare_digest(csrf_token or "", expected):
        raise HTTPException(status_code=HTTP_400_BAD_REQUEST, detail="CSRF check failed")


def render(request: Request, name: str, *, admin: Admin | None = None, **ctx) -> HTMLResponse:
    """Render a Jinja template with i18n + theme direction injected."""
    lang = dash_lang(request)
    context = {
        "admin": admin,
        "lang": lang,
        "dir": text_dir(lang),
        "supported_langs": SUPPORTED,
        "csrf_token": csrf_token(request),
        "t": lambda key, **kw: t(key, lang, **kw),
        **ctx,
    }
    # Current Starlette signature: TemplateResponse(request, name, context).
    return templates.TemplateResponse(request, name, context)
