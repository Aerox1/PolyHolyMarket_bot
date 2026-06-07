"""Authenticated dashboard pages (server-rendered).

SECURITY: this process runs WITHOUT ``ENCRYPTION_KEY``. It never reads, decrypts
or exposes wallet private keys / API secrets. User & account data comes only from
``dashboard.repo`` (public columns). Live positions are fetched from Polymarket's
PUBLIC Data API by wallet address (read-only creds, no key) on a best-effort basis.

Every route requires an authenticated admin; ``/broadcast`` requires a superadmin.
All routes are sync ``def`` — FastAPI runs them in a threadpool, so the blocking
Polymarket HTTP calls are fine without asyncio.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.status import HTTP_303_SEE_OTHER

from core import audit
from core.audit import AuditEvent
from dashboard import deps, repo
from dashboard.deps import get_db, require_admin, require_superadmin, verify_csrf
from db.models import Admin, Command, User, UserStatus
from polymarket.client import Polymarket
from polymarket.credentials import PolymarketCreds

logger = logging.getLogger(__name__)

router = APIRouter()

_PAGE_SIZE_USERS = 50
_PAGE_SIZE_AUDIT = 100

# Map a target status to the audit event it should record.
_STATUS_EVENT = {
    UserStatus.ACTIVE.value: AuditEvent.USER_ACTIVATED,
    UserStatus.SUSPENDED.value: AuditEvent.USER_SUSPENDED,
    UserStatus.BANNED.value: AuditEvent.USER_BANNED,
}


# ── helpers ─────────────────────────────────────────────────────────────────

def _client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


def _position_rows(raw) -> list[dict]:
    """Normalise a Data-API positions response into a plain list of row dicts.

    The endpoint may return a bare list or a dict wrapping ``data``/``positions``.
    """
    rows = raw
    if isinstance(raw, dict):
        rows = raw.get("data") or raw.get("positions") or []
    return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []


def _field(row: dict, *names):
    for n in names:
        v = row.get(n)
        if v not in (None, ""):
            return v
    return None


def _live_positions(accounts: list[dict]) -> list[dict]:
    """Best-effort live positions across every wallet on this user.

    Public Data API only (address-keyed, read-only creds). Any failure for a
    given wallet is swallowed (logged by exception type only) so the page still
    renders. Returns a flat list of ``{wallet, title, outcome, size, value}``.
    """
    out: list[dict] = []
    for acc in accounts:
        wallet = acc.get("wallet_address")
        if not wallet:
            continue
        pm = None
        try:
            pm = Polymarket.from_creds(PolymarketCreds.read_only(wallet))
            raw = pm.get_positions()
        except Exception as exc:  # noqa: BLE001 — never let a wallet break the page
            logger.warning("live positions fetch failed for a wallet: %s", type(exc).__name__)
            if pm is not None:
                try:
                    pm.close()
                except Exception:  # noqa: BLE001
                    pass
            continue
        else:
            try:
                pm.close()
            except Exception:  # noqa: BLE001
                pass

        for row in _position_rows(raw):
            try:
                size = float(_field(row, "size") or 0)
            except (TypeError, ValueError):
                size = 0.0
            try:
                value = float(_field(row, "currentValue", "value") or 0)
            except (TypeError, ValueError):
                value = 0.0
            out.append({
                "wallet": wallet,
                "title": _field(row, "title", "market") or "",
                "outcome": _field(row, "outcome") or "",
                "size": size,
                "value": value,
            })
    return out


# ── routes ──────────────────────────────────────────────────────────────────

@router.get("/")
def index(admin: Admin = Depends(require_admin)) -> RedirectResponse:
    return RedirectResponse("/metrics", status_code=HTTP_303_SEE_OTHER)


@router.get("/metrics")
def metrics(
    request: Request,
    admin: Admin = Depends(require_admin),
    db: Session = Depends(get_db),
):
    return deps.render(request, "metrics.html", admin=admin, metrics=repo.metrics_summary(db))


@router.get("/users")
def users_list(
    request: Request,
    status: str | None = None,
    q: str | None = None,
    page: int = 1,
    admin: Admin = Depends(require_admin),
    db: Session = Depends(get_db),
):
    page = max(page, 1)
    offset = (page - 1) * _PAGE_SIZE_USERS
    users = repo.list_users(db, status=status, q=q, limit=_PAGE_SIZE_USERS, offset=offset)
    total = repo.count_users(db, status=status, q=q)
    return deps.render(
        request,
        "users_list.html",
        admin=admin,
        users=users,
        total=total,
        page=page,
        status_filter=status or "",
        q=q or "",
        has_next=(offset + _PAGE_SIZE_USERS < total),
    )


@router.get("/users/{user_id}")
def user_detail(
    request: Request,
    user_id: int,
    admin: Admin = Depends(require_admin),
    db: Session = Depends(get_db),
):
    detail = repo.user_detail(db, user_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="User not found")
    positions = _live_positions(detail["accounts"])
    return deps.render(request, "user_detail.html", admin=admin, detail=detail, positions=positions,
                       rewards=repo.user_rewards(db, user_id), referees=repo.user_referees(db, user_id))


@router.get("/referrals")
def referrals_page(request: Request, admin: Admin = Depends(require_admin), db: Session = Depends(get_db)):
    leaders = repo.top_referrers(db, 25)
    for ldr in leaders:  # attach each referrer's referees so rows can expand
        ldr["referees"] = repo.user_referees(db, ldr["user_id"], limit=10)
    return deps.render(request, "referrals.html", admin=admin,
                       overview=repo.referral_overview(db), leaders=leaders)


@router.get("/referrals/export.csv")
def referrals_export(admin: Admin = Depends(require_admin), db: Session = Depends(get_db)):
    import csv
    import io

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["inviter_id", "inviter_username", "invitee_id", "invitee_username",
                "status", "created_at", "unlocked_at", "invitee_bets"])
    for e in repo.referral_edges(db):
        w.writerow([e["inviter_id"], e["inviter_username"] or "", e["invitee_id"],
                    e["invitee_username"] or "", e["status"], e["created_at"] or "",
                    e["unlocked_at"] or "", e["bets"]])
    return Response(content=buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": "attachment; filename=referrals.csv"})


@router.post("/users/{user_id}/status")
def user_set_status(
    request: Request,
    user_id: int,
    status: str = Form(...),
    admin: Admin = Depends(require_admin),
    db: Session = Depends(get_db),
    _csrf: None = Depends(verify_csrf),
):
    if status not in _STATUS_EVENT:
        raise HTTPException(status_code=400, detail="Invalid status")
    if repo.set_user_status(db, user_id, status):
        audit.record(
            db,
            _STATUS_EVENT[status],
            actor_type="admin",
            actor_id=admin.id,
            user_id=user_id,
            ip=_client_ip(request),
        )
    return RedirectResponse(f"/users/{user_id}", status_code=HTTP_303_SEE_OTHER)


@router.get("/broadcast")
def broadcast_form(
    request: Request,
    admin: Admin = Depends(require_superadmin),
):
    return deps.render(request, "broadcast.html", admin=admin)


@router.post("/broadcast")
def broadcast_send(
    request: Request,
    message: str = Form(...),
    language: str | None = Form(None),
    only_active: bool = Form(False),
    admin: Admin = Depends(require_superadmin),
    db: Session = Depends(get_db),
    _csrf: None = Depends(verify_csrf),
):
    stmt = select(User)
    if only_active:
        stmt = stmt.where(User.status == UserStatus.ACTIVE.value)
    if language:
        stmt = stmt.where(User.language == language)
    targets = list(db.scalars(stmt))

    for u in targets:
        db.add(Command(
            user_id=u.id,
            action="BROADCAST",
            payload={"message": message},
            status="pending",
        ))

    sent_count = len(targets)
    audit.record(
        db,
        AuditEvent.BROADCAST_SENT,
        actor_type="admin",
        actor_id=admin.id,
        detail={"count": sent_count, "language": language},
        ip=_client_ip(request),
    )
    return deps.render(request, "broadcast.html", admin=admin, sent_count=sent_count)


@router.get("/audit")
def audit_log(
    request: Request,
    event: str | None = None,
    user_id: int | None = None,
    page: int = 1,
    admin: Admin = Depends(require_admin),
    db: Session = Depends(get_db),
):
    page = max(page, 1)
    offset = (page - 1) * _PAGE_SIZE_AUDIT
    entries = repo.list_audit(db, event=event, user_id=user_id, limit=_PAGE_SIZE_AUDIT, offset=offset)
    return deps.render(
        request,
        "audit.html",
        admin=admin,
        entries=entries,
        page=page,
        event_filter=event or "",
        user_id_filter=user_id or "",
        has_next=(len(entries) == _PAGE_SIZE_AUDIT),
    )


@router.get("/miniapp")
def miniapp_page(request: Request, admin: Admin = Depends(require_admin), db: Session = Depends(get_db)):
    return deps.render(request, "miniapp.html", admin=admin,
                       categories=repo.list_categories(db), gemini=repo.gemini_stats(db),
                       welcome=repo.welcome_banner(db))


@router.post("/miniapp/welcome")
def miniapp_welcome_save(
    request: Request,
    prompt: str = Form(""),
    regenerate: bool = Form(False),
    admin: Admin = Depends(require_admin),
    db: Session = Depends(get_db),
    _csrf: None = Depends(verify_csrf),
):
    repo.set_welcome_prompt(db, prompt, regenerate=regenerate)
    return RedirectResponse("/miniapp", status_code=HTTP_303_SEE_OTHER)


@router.post("/miniapp/welcome/upload")
def miniapp_welcome_upload(
    request: Request,
    image: UploadFile = File(...),
    admin: Admin = Depends(require_admin),
    db: Session = Depends(get_db),
    _csrf: None = Depends(verify_csrf),
):
    data = image.file.read()
    if not (data[:8] == b"\x89PNG\r\n\x1a\n" or data[:3] == b"\xff\xd8\xff" or data[8:12] == b"WEBP"):
        raise HTTPException(status_code=400, detail="upload a PNG, JPEG or WEBP image")
    if len(data) > 8 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="image too large (max 8MB)")
    repo.save_welcome_image(db, data)
    return RedirectResponse("/miniapp", status_code=HTTP_303_SEE_OTHER)


@router.post("/miniapp/budget")
def miniapp_set_budget(
    request: Request,
    weekly_budget: float = Form(...),
    admin: Admin = Depends(require_superadmin),
    db: Session = Depends(get_db),
    _csrf: None = Depends(verify_csrf),
):
    if weekly_budget < 0:
        raise HTTPException(status_code=400, detail="budget must be >= 0")
    repo.set_gemini_budget(db, weekly_budget)
    audit.record(db, AuditEvent.GEMINI_BUDGET_SET, actor_type="admin", actor_id=admin.id,
                 detail={"weekly_budget": weekly_budget}, ip=_client_ip(request))
    return RedirectResponse("/miniapp", status_code=HTTP_303_SEE_OTHER)


@router.get("/miniapp/categories/{category_id}")
def miniapp_category_edit(request: Request, category_id: int,
                          admin: Admin = Depends(require_admin), db: Session = Depends(get_db)):
    cat = repo.get_category(db, category_id)
    if cat is None:
        raise HTTPException(status_code=404, detail="category not found")
    from core.gemini import build_prompt
    return deps.render(request, "category_edit.html", admin=admin, cat=cat,
                       default_prompt=build_prompt(cat.title))


@router.post("/miniapp/categories/{category_id}/edit")
def miniapp_category_save(
    request: Request,
    category_id: int,
    title: str = Form(""),
    prompt_override: str = Form(""),
    regenerate: bool = Form(False),
    admin: Admin = Depends(require_admin),
    db: Session = Depends(get_db),
    _csrf: None = Depends(verify_csrf),
):
    if not repo.update_category(db, category_id, title=title, prompt_override=prompt_override, regenerate=regenerate):
        raise HTTPException(status_code=404, detail="category not found")
    return RedirectResponse(f"/miniapp/categories/{category_id}", status_code=HTTP_303_SEE_OTHER)


@router.post("/miniapp/categories/{category_id}/upload")
def miniapp_category_upload(
    request: Request,
    category_id: int,
    image: UploadFile = File(...),
    admin: Admin = Depends(require_admin),
    db: Session = Depends(get_db),
    _csrf: None = Depends(verify_csrf),
):
    data = image.file.read()
    # accept PNG / JPEG / WEBP only
    if not (data[:8] == b"\x89PNG\r\n\x1a\n" or data[:3] == b"\xff\xd8\xff" or data[8:12] == b"WEBP"):
        raise HTTPException(status_code=400, detail="upload a PNG, JPEG or WEBP image")
    if len(data) > 8 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="image too large (max 8MB)")
    if not repo.save_category_image(db, category_id, data):
        raise HTTPException(status_code=404, detail="category not found")
    return RedirectResponse(f"/miniapp/categories/{category_id}", status_code=HTTP_303_SEE_OTHER)


@router.post("/miniapp/categories/{category_id}")
def miniapp_curate(
    request: Request,
    category_id: int,
    action: str = Form(...),
    admin: Admin = Depends(require_admin),
    db: Session = Depends(get_db),
    _csrf: None = Depends(verify_csrf),
):
    if not repo.curate_category(db, category_id, action):
        raise HTTPException(status_code=400, detail="invalid action or category")
    return RedirectResponse("/miniapp", status_code=HTTP_303_SEE_OTHER)


@router.get("/settings")
def settings_page(
    request: Request,
    admin: Admin = Depends(require_admin),
    db: Session = Depends(get_db),
):
    return deps.render(request, "settings.html", admin=admin, access=repo.access_settings(db))


@router.post("/settings/access")
def settings_access_save(
    request: Request,
    access_code: str = Form(""),
    access_enabled: bool = Form(False),
    admin: Admin = Depends(require_superadmin),
    db: Session = Depends(get_db),
    _csrf: None = Depends(verify_csrf),
):
    repo.set_access_settings(db, code=access_code, enabled=access_enabled)
    audit.record(db, AuditEvent.ACCESS_SETTINGS_SET, actor_type="admin", actor_id=admin.id,
                 detail={"enabled": access_enabled, "code_set": bool(access_code.strip())})
    return RedirectResponse("/settings", status_code=HTTP_303_SEE_OTHER)
