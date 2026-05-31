"""Admin authentication: login / logout + per-session dashboard language.

The dashboard authenticates admins against the ``admins`` table using an
argon2id password hash (verified via :func:`core.crypto.verify_password` —
the ONLY thing this process uses from ``core.crypto``; it never touches wallet
key material). A successful login stores ``admin_id`` in the signed session
cookie; logout clears it.

This module also handles switching the dashboard UI language (stored per
session as ``dash_lang``).
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.status import HTTP_303_SEE_OTHER, HTTP_401_UNAUTHORIZED

from core import audit
from core.audit import AuditEvent
from core.crypto import verify_password
from core.i18n import SUPPORTED, t
from dashboard import deps
from dashboard.deps import current_admin, get_db, require_admin
from db.models import Admin

router = APIRouter()

# A well-formed argon2id hash used to keep timing roughly constant when the
# supplied username does not exist, reducing user-enumeration via response time.
# (Password "x" — it can never match a real login since no admin has it.)
_DUMMY_HASH = (
    "$argon2id$v=19$m=65536,t=3,p=4$"
    "ZHVtbXlzYWx0ZHVtbXk$"
    "Q2hhbmdlTWVUaGlzSXNOb3RBUmVhbEhhc2hWYWx1ZQ"
)


def _client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


@router.get("/login", response_class=HTMLResponse)
def login_form(request: Request, db: Session = Depends(get_db)):
    # Already authenticated → straight to the dashboard.
    if current_admin(request, db) is not None:
        return RedirectResponse("/", status_code=HTTP_303_SEE_OTHER)
    return deps.render(request, "login.html")


@router.post("/login")
def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    admin = db.execute(
        select(Admin).where(Admin.username == username)
    ).scalar_one_or_none()

    # Always run a verify to keep timing similar whether or not the user exists.
    stored_hash = admin.password_hash if admin is not None else _DUMMY_HASH
    ok = verify_password(stored_hash, password)

    if admin is None or not ok:
        audit.record(
            db,
            AuditEvent.ADMIN_LOGIN_FAIL,
            actor_type="admin",
            detail={"username": username},
            ip=_client_ip(request),
        )
        lang = deps.dash_lang(request)
        response = deps.render(request, "login.html", error=t("dash.login.failed", lang))
        response.status_code = HTTP_401_UNAUTHORIZED
        return response

    request.session["admin_id"] = admin.id
    admin.last_login_at = datetime.now(timezone.utc)
    audit.record(
        db,
        AuditEvent.ADMIN_LOGIN,
        actor_type="admin",
        actor_id=admin.id,
        ip=_client_ip(request),
    )
    return RedirectResponse("/", status_code=HTTP_303_SEE_OTHER)


@router.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=HTTP_303_SEE_OTHER)


def _safe_redirect_target(request: Request) -> str:
    """Pick a SAME-SITE redirect target from the Referer, defaulting to ``/``.

    The raw Referer header is attacker-influenceable, so we never redirect to it
    directly (open-redirect). We only keep the path+query of a same-origin
    referer and require it to be a single-leading-slash relative path
    (rejecting ``//host`` and ``/\\host`` protocol-relative tricks).
    """
    referer = request.headers.get("referer")
    if not referer:
        return "/"
    from urllib.parse import urlsplit

    ref = urlsplit(referer)
    # Cross-origin referer → ignore it entirely.
    if ref.netloc and ref.netloc != request.url.netloc:
        return "/"
    path = ref.path or "/"
    # Must be a same-site absolute path; reject protocol-relative ("//", "/\").
    if not path.startswith("/") or path.startswith("//") or path.startswith("/\\"):
        return "/"
    return path + (f"?{ref.query}" if ref.query else "")


@router.post("/me/language")
def set_language(
    request: Request,
    lang: str = Form(...),
    admin: Admin = Depends(require_admin),
):
    if lang in SUPPORTED:
        request.session["dash_lang"] = lang
    return RedirectResponse(_safe_redirect_target(request), status_code=HTTP_303_SEE_OTHER)
