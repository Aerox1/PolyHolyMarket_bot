"""News admin pages (server-rendered, Jinja2).

KEYLESS INVARIANT: this process never calls Gemini or Telegram and holds no
secrets. Every action here is a DB flag-write (approve/reject/edit/source CRUD/
settings) that the keyed bot worker acts on — mirroring the category-curation
discipline in pages.py. All routes are sync `def` + PRG redirects.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session
from starlette.status import HTTP_303_SEE_OTHER

from core import audit
from core.audit import AuditEvent
from core.config import SUPPORTED_LANGUAGES
from dashboard import deps, repo
from dashboard.deps import get_db, require_admin, require_superadmin, verify_csrf
from db.models import Admin

logger = logging.getLogger(__name__)

router = APIRouter()

_PAGE_SIZE = 50
_CURATE_EVENT = {
    "approve": AuditEvent.NEWS_ITEM_APPROVED,
    "reject": AuditEvent.NEWS_ITEM_REJECTED,
}


def _ip(request: Request) -> str | None:
    return request.client.host if request.client else None


# ── queue ─────────────────────────────────────────────────────────────────────

@router.get("/news")
def news_queue(
    request: Request,
    status: str | None = None,
    category_id: int | None = None,
    page: int = 1,
    admin: Admin = Depends(require_admin),
    db: Session = Depends(get_db),
):
    page = max(page, 1)
    offset = (page - 1) * _PAGE_SIZE
    items = repo.list_news_items(db, status=status, category_id=category_id, limit=_PAGE_SIZE, offset=offset)
    total = repo.count_news_items(db, status=status, category_id=category_id)
    return deps.render(
        request, "news_queue.html", admin=admin,
        items=items, overview=repo.news_overview(db),
        status_filter=status or "", page=page,
        has_next=(offset + _PAGE_SIZE < total),
    )


@router.get("/news/sources")
def news_sources_page(request: Request, admin: Admin = Depends(require_admin), db: Session = Depends(get_db)):
    return deps.render(request, "news_sources.html", admin=admin,
                       sources=repo.list_news_sources(db), categories=repo.list_categories(db))


@router.get("/news/settings")
def news_settings_page(request: Request, admin: Admin = Depends(require_admin), db: Session = Depends(get_db)):
    return deps.render(request, "news_settings.html", admin=admin, settings=repo.news_settings(db))


@router.get("/news/bets")
def news_bets_page(request: Request, admin: Admin = Depends(require_admin), db: Session = Depends(get_db)):
    """News-driven bets + the deferred-intent conversion funnel (read-only)."""
    return deps.render(request, "news_bets.html", admin=admin,
                       overview=repo.news_overview(db), bets=repo.news_bets_overview(db))


@router.get("/news/{item_id}")
def news_item_page(request: Request, item_id: int,
                   admin: Admin = Depends(require_admin), db: Session = Depends(get_db)):
    detail = repo.news_item_detail(db, item_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="news item not found")
    return deps.render(request, "news_item.html", admin=admin, item=detail, langs=SUPPORTED_LANGUAGES)


# ── item actions (flag-writes) ─────────────────────────────────────────────────

@router.post("/news/{item_id}/action")
def news_item_action(
    request: Request,
    item_id: int,
    action: str = Form(...),
    admin: Admin = Depends(require_admin),
    db: Session = Depends(get_db),
    _csrf: None = Depends(verify_csrf),
):
    if not repo.curate_news_item(db, item_id, action):
        raise HTTPException(status_code=400, detail="invalid action or item")
    if action in _CURATE_EVENT:
        audit.record(db, _CURATE_EVENT[action], actor_type="admin", actor_id=admin.id,
                     detail={"item_id": item_id}, ip=_ip(request))
    # back to the queue for approve/reject; stay on the item for edits/regen
    target = "/news" if action in ("approve", "reject") else f"/news/{item_id}"
    return RedirectResponse(target, status_code=HTTP_303_SEE_OTHER)


@router.post("/news/{item_id}/translations")
def news_item_translations(
    request: Request,
    item_id: int,
    # SUPPORTED_LANGUAGES is fixed (en/fa/ru/zh) so we declare the fields
    # explicitly rather than reaching into the raw form.
    title_en: str = Form(""), summary_en: str = Form(""),
    title_fa: str = Form(""), summary_fa: str = Form(""),
    title_ru: str = Form(""), summary_ru: str = Form(""),
    title_zh: str = Form(""), summary_zh: str = Form(""),
    admin: Admin = Depends(require_admin),
    db: Session = Depends(get_db),
    _csrf: None = Depends(verify_csrf),
):
    pairs = {
        "en": (title_en, summary_en), "fa": (title_fa, summary_fa),
        "ru": (title_ru, summary_ru), "zh": (title_zh, summary_zh),
    }
    translations: dict[str, dict[str, str]] = {}
    for lang, (title, summary) in pairs.items():
        title, summary = title.strip(), summary.strip()
        if title or summary:
            translations[lang] = {"title": title, "summary": summary}
    if not repo.update_news_translations(db, item_id, translations):
        raise HTTPException(status_code=404, detail="news item not found")
    audit.record(db, AuditEvent.NEWS_ITEM_EDITED, actor_type="admin", actor_id=admin.id,
                 detail={"item_id": item_id, "langs": list(translations)}, ip=_ip(request))
    return RedirectResponse(f"/news/{item_id}", status_code=HTTP_303_SEE_OTHER)


# ── sources ─────────────────────────────────────────────────────────────────

@router.post("/news/sources")
def news_source_create(
    request: Request,
    name: str = Form(""),
    url: str = Form(...),
    kind: str = Form("auto"),
    category_id: str = Form(""),  # "" from the empty <option>; coerced below
    admin: Admin = Depends(require_admin),
    db: Session = Depends(get_db),
    _csrf: None = Depends(verify_csrf),
):
    cid = int(category_id) if category_id.strip().isdigit() else None
    src = repo.create_news_source(db, name=name, url=url, category_id=cid, kind=kind)
    if src is None:
        raise HTTPException(status_code=400, detail="invalid or duplicate URL")
    audit.record(db, AuditEvent.NEWS_SOURCE_ADDED, actor_type="admin", actor_id=admin.id,
                 detail={"source_id": src.id, "url": src.url}, ip=_ip(request))
    return RedirectResponse("/news/sources", status_code=HTTP_303_SEE_OTHER)


@router.post("/news/sources/{source_id}")
def news_source_action(
    request: Request,
    source_id: int,
    action: str = Form(...),
    admin: Admin = Depends(require_admin),
    db: Session = Depends(get_db),
    _csrf: None = Depends(verify_csrf),
):
    if not repo.curate_news_source(db, source_id, action):
        raise HTTPException(status_code=400, detail="invalid action or source")
    if action == "delete":
        audit.record(db, AuditEvent.NEWS_SOURCE_REMOVED, actor_type="admin", actor_id=admin.id,
                     detail={"source_id": source_id}, ip=_ip(request))
    return RedirectResponse("/news/sources", status_code=HTTP_303_SEE_OTHER)


# ── settings (superadmin) ──────────────────────────────────────────────────────

@router.post("/news/settings")
def news_settings_save(
    request: Request,
    channel_id: str = Form(""),
    top_n: int = Form(5),
    autosend: bool = Form(False),
    admin: Admin = Depends(require_superadmin),
    db: Session = Depends(get_db),
    _csrf: None = Depends(verify_csrf),
):
    repo.set_news_settings(db, channel_id=channel_id, top_n=top_n, autosend=autosend)
    audit.record(db, AuditEvent.NEWS_SETTINGS_SET, actor_type="admin", actor_id=admin.id,
                 detail={"channel_set": bool(channel_id.strip()), "top_n": top_n, "autosend": autosend},
                 ip=_ip(request))
    return RedirectResponse("/news/settings", status_code=HTTP_303_SEE_OTHER)
