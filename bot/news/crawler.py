"""Fetch + parse news articles from admin-curated RSS / single-article HTML.

Ported from NabzarSocial's crawler, hardened for PHM:
* ``trust_env=settings.news_crawl_trust_env`` (default False) — the local VPN/proxy
  breaks egress otherwise (see memory: vpn-blocks-egress).
* SSRF guard: http(s) only; reject hosts that resolve to private/loopback/
  link-local/reserved IPs; capped redirects, body size and timeout.
* v1 scope is RSS + single-article HTML only (no t.me/s/ scraping).
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import logging
import re
import socket
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse, urlsplit, urlunsplit

import httpx

from core.config import settings

logger = logging.getLogger(__name__)

# Heavy parse deps are OPTIONAL at import time: the bot must boot even when the
# news pipeline is disabled and these aren't installed. fetch_articles() raises a
# clear error if invoked without them (the crawl job catches it per source).
try:
    import feedparser
    import trafilatura
    from bs4 import BeautifulSoup
    _DEPS_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised only on minimal installs
    feedparser = None  # type: ignore[assignment]
    trafilatura = None  # type: ignore[assignment]
    BeautifulSoup = None  # type: ignore[assignment]
    _DEPS_AVAILABLE = False


@dataclass
class FetchedArticle:
    url: str
    url_hash: str
    title: str
    body: str
    lang: str | None
    hero_image: str | None


# ── hashing / scoring ────────────────────────────────────────────────────────

def url_hash(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


def dedup_hash(title: str) -> str:
    """sha256 of a normalized title — collapses cross-source reposts of the same
    story (whitespace-folded, lowercased)."""
    norm = re.sub(r"\s+", " ", (title or "").strip().lower())
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()


_DATELINE_RE = re.compile(r"^\s*(published|updated)\s+on\b.*$", re.IGNORECASE)


def _norm_line(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def clean_body(title: str, body: str) -> str:
    """Strip feed boilerplate so the stored summary doesn't repeat the headline.

    Many RSS feeds / trafilatura extractions lead the body with the article's own
    H1 (an exact duplicate of the title) and trail it with a 'Published On <date>'
    dateline — both render as noise under the bold title (see the Adam Hamawy
    item). Drops leading blank/title-duplicate lines and any dateline; otherwise
    leaves the body untouched. Conservative: only removes lines that EXACTLY match
    the title (normalized) so real content is never trimmed."""
    if not body:
        return body or ""
    nt = _norm_line(title)
    lines = body.splitlines()
    # drop leading blank lines and lines that just repeat the headline
    while lines and (_norm_line(lines[0]) == "" or (nt and _norm_line(lines[0]) == nt)):
        lines.pop(0)
    # drop trailing blank lines and datelines ('Published On 3 Jun 2026')
    while lines and (_norm_line(lines[-1]) == "" or _DATELINE_RE.match(lines[-1])):
        lines.pop()
    return "\n".join(lines).strip()


def score_article(article: FetchedArticle) -> float:
    """Cheap heuristic in [0, 1] — no LLM. Longer body + a hero image rank higher."""
    score = 0.5
    if article.body and len(article.body) > 400:
        score += 0.2
    if article.hero_image:
        score += 0.1
    return min(score, 1.0)


# ── SSRF guard ───────────────────────────────────────────────────────────────

class UnsafeUrlError(ValueError):
    """Raised when a URL fails the SSRF allowlist."""


def _ip_is_blocked(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return True
    return (addr.is_private or addr.is_loopback or addr.is_link_local
            or addr.is_reserved or addr.is_multicast or addr.is_unspecified)


async def _assert_public_url(url: str) -> str:
    """Reject non-http(s) schemes and hosts resolving to non-public IPs, and
    return a single validated IP to pin the connection to.

    Pinning the resolved IP (see :func:`_connect_target`) closes the DNS-rebinding
    TOCTOU gap: without it httpx would independently RE-resolve the hostname at
    connect time, so a host whose DNS flips public→internal between this check and
    the connect could still reach 169.254.169.254 / internal services."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise UnsafeUrlError(f"scheme not allowed: {parsed.scheme!r}")
    host = parsed.hostname
    if not host:
        raise UnsafeUrlError("missing host")
    # IP literal → check directly; hostname → resolve all addresses and check each.
    try:
        ipaddress.ip_address(host)
        addrs = [host]
    except ValueError:
        try:
            infos = await asyncio.to_thread(socket.getaddrinfo, host, None)
        except OSError as exc:
            raise UnsafeUrlError(f"dns resolution failed for {host}") from exc
        addrs = [info[4][0] for info in infos]
    if not addrs or any(_ip_is_blocked(a) for a in addrs):
        raise UnsafeUrlError(f"host resolves to a non-public address: {host}")
    return addrs[0]  # pin the connection to this vetted IP


def _connect_target(url: str, pinned_ip: str) -> tuple[str, dict, dict | None]:
    """Build ``(connect_url, extra_headers, extensions)`` that pin the TCP socket to
    the pre-validated ``pinned_ip`` while preserving the original hostname for the
    Host header and TLS SNI / certificate verification.

    For IP-literal hosts there is nothing to rebind (no DNS lookup), so connect
    unchanged. For DNS hosts we connect to the IP directly so httpx cannot
    re-resolve to a different (internal) address between the SSRF check and connect."""
    parts = urlsplit(url)
    host = parts.hostname or ""
    if not host or pinned_ip == host:  # IP literal or nothing to pin
        return url, {}, None
    ip_netloc = f"[{pinned_ip}]" if ":" in pinned_ip else pinned_ip
    if parts.port:
        ip_netloc = f"{ip_netloc}:{parts.port}"
    connect_url = urlunsplit((parts.scheme, ip_netloc, parts.path, parts.query, parts.fragment))
    host_header = host if not parts.port else f"{host}:{parts.port}"
    return connect_url, {"Host": host_header}, {"sni_hostname": host}


# ── HTTP ─────────────────────────────────────────────────────────────────────

_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,application/rss+xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

_CHALLENGE_MARKERS = (
    "security verification", "403 forbidden", "access denied",
    "just a moment...", "checking your browser", "attention required",
)


def _looks_like_rss(content: str) -> bool:
    head = content.lstrip()[:200].lower()
    return "<rss" in head or "<feed" in head or ("<?xml" in head and ("rss" in head or "atom" in head))


def _looks_like_challenge_page(text: str) -> bool:
    if not text:
        return False
    head = text[:2000].lower()
    return any(m in head for m in _CHALLENGE_MARKERS) or ("status code" in head and "403" in head)


_MAX_REDIRECTS = 5


async def _stream_capped(client: httpx.AsyncClient, url: str, extra_headers: dict,
                         extensions: dict | None = None) -> tuple[int, httpx.Headers, str]:
    """One GET (no auto-redirect). Redirects return empty body; other responses
    are streamed with a hard byte cap (defeats gzip-bombs / lying content-length)."""
    kwargs: dict = {"headers": extra_headers}
    if extensions:  # only set when pinning a DNS host (keeps IP-literal calls unchanged)
        kwargs["extensions"] = extensions
    async with client.stream("GET", url, **kwargs) as r:
        if 300 <= r.status_code < 400:
            return r.status_code, r.headers, ""
        clen = r.headers.get("content-length")
        if clen and clen.isdigit() and int(clen) > settings.news_crawl_max_bytes:
            raise UnsafeUrlError(f"response too large: {clen} bytes")
        raw = bytearray()
        async for chunk in r.aiter_bytes():
            raw.extend(chunk)
            if len(raw) > settings.news_crawl_max_bytes:
                raise UnsafeUrlError("response exceeded max bytes")
        text = bytes(raw).decode(r.charset_encoding or "utf-8", "replace")
        return r.status_code, r.headers, text


async def _http_get(url: str) -> tuple[int, str, str]:
    """GET with an SSRF guard re-applied on EVERY redirect hop. Returns
    (status, text, content_type). Referer is per-request and dropped on a 403
    retry (client carries none, so the retry is genuinely bare)."""
    pinned_ip = await _assert_public_url(url)
    # NOTE: no Referer at client level — httpx merges client headers onto request
    # headers, so a client-level Referer would survive the "bare" 403 retry.
    limits = httpx.Limits(max_connections=10)
    async with httpx.AsyncClient(
        timeout=settings.news_crawl_timeout_seconds, follow_redirects=False,
        headers=dict(_BROWSER_HEADERS), trust_env=settings.news_crawl_trust_env, limits=limits,
    ) as client:
        current = url
        current_ip = pinned_ip
        for _ in range(_MAX_REDIRECTS + 1):
            host = urlparse(current).hostname or ""
            # Connect to the vetted IP (no DNS re-resolution) while keeping the
            # hostname for Host + TLS; revalidated + repinned on every redirect hop.
            connect_url, host_header, extensions = _connect_target(current, current_ip)
            referer = {"Referer": f"https://{host}/"} if host else {}
            status, headers, text = await _stream_capped(
                client, connect_url, {**referer, **host_header}, extensions)
            if status == 403 and referer:  # some CDNs reject the first hit with a Referer
                status, headers, text = await _stream_capped(client, connect_url, host_header, extensions)
            if 300 <= status < 400 and headers.get("location"):
                nxt = urljoin(current, headers["location"])
                current_ip = await _assert_public_url(nxt)  # re-validate + re-pin the redirect target
                current = nxt
                continue
            return status, text, headers.get("content-type", "")
        raise UnsafeUrlError("too many redirects")


# ── parsing ──────────────────────────────────────────────────────────────────

def _hero_from_html(html: str) -> str | None:
    soup = BeautifulSoup(html, "html.parser")
    og = soup.find("meta", property="og:image")
    if og and og.has_attr("content"):
        src = (og["content"] or "").strip()
        # The og:image comes from attacker-controllable article HTML and is later
        # handed to Telegram to fetch. Only accept http(s) URLs — reject
        # javascript:/data:/relative/other schemes outright.
        if src and urlparse(src).scheme in ("http", "https"):
            return src
    return None


def _title_from_html(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    el = soup.find("title")
    return (el.text.strip() if el else "")[:512]


async def fetch_articles(url: str, kind: str = "auto", limit: int = 10) -> list[FetchedArticle]:
    """Fetch up to ``limit`` articles from an RSS feed or a single HTML page.

    Returns [] on any fetch error (caller logs + marks the source). Raises
    UnsafeUrlError or a missing-deps RuntimeError, which the job treats as a
    source-level error."""
    if not _DEPS_AVAILABLE:
        raise RuntimeError("news crawl deps not installed (feedparser/trafilatura/beautifulsoup4)")
    status, body, ctype = await _http_get(url)
    if status >= 400:
        return []

    items: list[FetchedArticle] = []

    # feedparser / trafilatura / BeautifulSoup are CPU-heavy synchronous parsers
    # working on untrusted HTML/XML up to news_crawl_max_bytes. Run them in a worker
    # thread so a large feed/article can't stall the event loop (and every
    # concurrent user's trade command) for the duration of the parse.
    if kind in {"auto", "rss"} and ("xml" in ctype or _looks_like_rss(body)):
        feed = await asyncio.to_thread(feedparser.parse, body)
        # <language> is a channel-level element; per-entry language is rare. Fall
        # back to the feed language so non-English sources are labelled correctly.
        feed_lang = feed.feed.get("language") if getattr(feed, "feed", None) else None
        for entry in feed.entries[:limit]:
            link = entry.get("link")
            if not link:
                continue
            try:
                art_status, art_body, _ = await _http_get(link)
            except (httpx.HTTPError, UnsafeUrlError):
                continue
            if art_status >= 400:
                continue
            text = await asyncio.to_thread(
                trafilatura.extract, art_body, include_links=False, include_images=False)
            text = text or entry.get("summary", "")
            if _looks_like_challenge_page(text):
                continue
            title = (entry.get("title") or "")[:512]
            items.append(FetchedArticle(
                url=link, url_hash=url_hash(link), title=title,
                body=clean_body(title, text or ""), lang=entry.get("language") or feed_lang,
                hero_image=await asyncio.to_thread(_hero_from_html, art_body),
            ))
        return items

    # single HTML article
    text = await asyncio.to_thread(
        trafilatura.extract, body, include_links=False, include_images=False)
    if not text or _looks_like_challenge_page(text):
        return []
    title = await asyncio.to_thread(_title_from_html, body)
    items.append(FetchedArticle(
        url=url, url_hash=url_hash(url), title=title,
        body=clean_body(title, text), lang=None,
        hero_image=await asyncio.to_thread(_hero_from_html, body),
    ))
    return items
