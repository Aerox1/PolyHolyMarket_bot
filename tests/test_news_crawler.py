"""News crawler: hashing/scoring, the SSRF guard, and RSS/HTML parsing with
HTTP mocked (no network)."""

import httpx
import pytest

from bot.news import crawler
from bot.news.crawler import FetchedArticle, UnsafeUrlError

RSS = """<?xml version="1.0"?>
<rss version="2.0"><channel>
  <language>fa</language>
  <item><title>Fed holds rates</title><link>https://news.example.com/a1</link><language>en</language></item>
  <item><title>Bitcoin surges</title><link>https://news.example.com/a2</link></item>
</channel></rss>"""

HTML_A1 = '<html><head><meta property="og:image" content="https://img/a1.jpg"></head><body>x</body></html>'
HTML_A2 = "<html><head></head><body>y</body></html>"
HTML_PAGE = ('<html><head><title>Big News Today</title>'
             '<meta property="og:image" content="https://img/p.jpg"></head><body>z</body></html>')


# ── hashing / scoring ────────────────────────────────────────────────────────

def test_url_hash_and_dedup_hash():
    assert crawler.url_hash("https://x") == crawler.url_hash("https://x")
    assert crawler.url_hash("https://x") != crawler.url_hash("https://y")
    # dedup_hash normalizes case + whitespace
    assert crawler.dedup_hash("Hello   World") == crawler.dedup_hash("hello world")


def test_score_article_bounds():
    short = FetchedArticle("u", "h", "t", "short", None, None)
    rich = FetchedArticle("u", "h", "t", "x" * 500, None, "https://img")
    assert crawler.score_article(short) == 0.5
    assert crawler.score_article(rich) == pytest.approx(0.8)
    assert 0.0 <= crawler.score_article(rich) <= 1.0


# ── SSRF guard ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("bad", [
    "ftp://example.com/x",
    "file:///etc/passwd",
    "http://127.0.0.1/x",
    "http://10.0.0.5/x",
    "http://169.254.169.254/latest/meta-data",  # cloud metadata SSRF classic
    "http://[::1]/x",
    "http://192.168.1.1/x",
])
async def test_assert_public_url_rejects_unsafe(bad):
    with pytest.raises(UnsafeUrlError):
        await crawler._assert_public_url(bad)


async def test_assert_public_url_accepts_public_ip_literal():
    await crawler._assert_public_url("https://8.8.8.8/feed")  # no raise


class _FakeStream:
    """Stand-in for httpx's client.stream(...) async context manager."""
    def __init__(self, status, headers, body=b""):
        self.status_code = status
        self.headers = httpx.Headers(headers)
        self.charset_encoding = "utf-8"
        self._body = body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def aiter_bytes(self):
        yield self._body


class _FakeClient:
    def __init__(self, responses):
        self._responses = list(responses)
        self.requested: list[str] = []

    def stream(self, method, url, headers=None):
        self.requested.append(url)
        return self._responses.pop(0)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


async def test_http_get_rejects_redirect_to_private_ip(monkeypatch):
    # public initial URL → 302 to the cloud-metadata IP → MUST be rejected
    fake = _FakeClient([_FakeStream(302, {"location": "http://169.254.169.254/latest/meta-data"})])
    monkeypatch.setattr(crawler.httpx, "AsyncClient", lambda *a, **k: fake)
    with pytest.raises(UnsafeUrlError):
        await crawler._http_get("https://8.8.8.8/feed")


async def test_http_get_follows_safe_redirect(monkeypatch):
    fake = _FakeClient([
        _FakeStream(301, {"location": "https://8.8.8.8/final"}),
        _FakeStream(200, {"content-type": "text/html"}, b"hello body"),
    ])
    monkeypatch.setattr(crawler.httpx, "AsyncClient", lambda *a, **k: fake)
    status, text, ctype = await crawler._http_get("https://8.8.8.8/start")
    assert status == 200 and text == "hello body" and "text/html" in ctype
    assert fake.requested == ["https://8.8.8.8/start", "https://8.8.8.8/final"]


async def test_http_get_caps_oversize_body(monkeypatch):
    monkeypatch.setattr(crawler.settings, "news_crawl_max_bytes", 10)
    fake = _FakeClient([_FakeStream(200, {"content-type": "text/html"}, b"x" * 50)])
    monkeypatch.setattr(crawler.httpx, "AsyncClient", lambda *a, **k: fake)
    with pytest.raises(UnsafeUrlError):
        await crawler._http_get("https://8.8.8.8/big")


async def test_assert_public_url_resolves_hostname(monkeypatch):
    # public-resolving host passes; private-resolving host is rejected
    monkeypatch.setattr(crawler.socket, "getaddrinfo",
                        lambda *a, **k: [(2, 1, 6, "", ("93.184.216.34", 0))])
    await crawler._assert_public_url("https://example.com/feed")
    monkeypatch.setattr(crawler.socket, "getaddrinfo",
                        lambda *a, **k: [(2, 1, 6, "", ("10.1.2.3", 0))])
    with pytest.raises(UnsafeUrlError):
        await crawler._assert_public_url("https://intranet.local/feed")


# ── parsing (HTTP mocked) ────────────────────────────────────────────────────

def _router(routes):
    async def _fake_http_get(url):
        return routes[url]
    return _fake_http_get


# The parse tests exercise real feedparser + BeautifulSoup; skip if the optional
# news deps aren't installed (e.g. behind a VPN that blocks pip).
pytestmark_parse = pytest.mark.skipif(not crawler._DEPS_AVAILABLE, reason="news parse deps not installed")


@pytestmark_parse
async def test_fetch_articles_rss(monkeypatch):
    routes = {
        "https://feed/rss": (200, RSS, "application/rss+xml"),
        "https://news.example.com/a1": (200, HTML_A1, "text/html"),
        "https://news.example.com/a2": (200, HTML_A2, "text/html"),
    }
    monkeypatch.setattr(crawler, "_http_get", _router(routes))
    monkeypatch.setattr(crawler.trafilatura, "extract", lambda body, **k: "Full extracted article body.")
    arts = await crawler.fetch_articles("https://feed/rss", kind="auto", limit=10)
    assert [a.title for a in arts] == ["Fed holds rates", "Bitcoin surges"]
    assert arts[0].lang == "en"           # entry-level <language>
    assert arts[1].lang == "fa"           # falls back to channel-level <language>
    assert arts[0].hero_image == "https://img/a1.jpg"
    assert arts[1].hero_image is None
    assert all(a.url_hash == crawler.url_hash(a.url) for a in arts)


@pytestmark_parse
async def test_fetch_articles_rss_skips_challenge_pages(monkeypatch):
    routes = {
        "https://feed/rss": (200, RSS, "application/rss+xml"),
        "https://news.example.com/a1": (200, HTML_A1, "text/html"),
        "https://news.example.com/a2": (200, HTML_A2, "text/html"),
    }
    monkeypatch.setattr(crawler, "_http_get", _router(routes))
    monkeypatch.setattr(crawler.trafilatura, "extract", lambda body, **k: "Just a moment... checking your browser")
    arts = await crawler.fetch_articles("https://feed/rss", kind="auto", limit=10)
    assert arts == []  # both entries look like challenge pages


@pytestmark_parse
async def test_fetch_articles_single_html(monkeypatch):
    routes = {"https://site/article": (200, HTML_PAGE, "text/html")}
    monkeypatch.setattr(crawler, "_http_get", _router(routes))
    monkeypatch.setattr(crawler.trafilatura, "extract", lambda body, **k: "Article body extracted.")
    arts = await crawler.fetch_articles("https://site/article", kind="auto")
    assert len(arts) == 1
    assert arts[0].title == "Big News Today"
    assert arts[0].hero_image == "https://img/p.jpg"
    assert arts[0].body == "Article body extracted."


@pytestmark_parse
async def test_fetch_articles_http_error_returns_empty(monkeypatch):
    monkeypatch.setattr(crawler, "_http_get", _router({"https://x/feed": (503, "", "")}))
    assert await crawler.fetch_articles("https://x/feed") == []
