# Implementation Plan: NabzarSocial News Pipeline → PolyHolyMarket (PHM)

> Plan-only design (2026-06). Merges NabzarSocial's editorial news pipeline into PolyHolyMarket,
> reusing PHM infrastructure rather than porting Nabzar's stack. Delivery model: Off / Daily digest /
> Real-time, user picks. Each news item carries a CTA deep-link back into the bot (tap-to-trade).

## 1. Goal & Summary

Integrate NabzarSocial's editorial news pipeline (crawl → translate/summarize → image-render → approve →
publish) into PolyHolyMarket as a native subsystem, reusing PHM's existing infrastructure rather than
porting Nabzar's stack wholesale. The pipeline runs on PHM's PTB `JobQueue` (no APScheduler, no Redis),
shares the single `core/gemini.py` budget-gated client, stores content in portable SQLAlchemy tables
alongside the existing spine, and surfaces an admin queue through the keyless Jinja2 dashboard. The product
value-add is a **news → relevant-Polymarket-market CTA deep-link**: approved items post to one news channel
and, per user preference (Off / Daily digest / Real-time), to bot DMs, each carrying a tap-to-trade button.
The whole merge is **+4 runtime deps, −7 deps** plus the entire Node toolchain dropped, shippable in six
independent phases.

## 2. Architecture Decisions (mismatch resolution table)

| # | Mismatch | Resolution | Rationale |
|---|---|---|---|
| D1 | Next.js/React admin → Jinja2 | **Jinja2 server-rendered pages** under `dashboard/routers/news.py`; entire Nabzar `app/components/lib` + Node toolchain discarded | One UI stack; reuses `deps.render`, CSRF, session auth, `theme.css`; removes Node from the image (attack-surface reduction, kills Next CVE exposure) |
| D2 | APScheduler + Redis → JobQueue | **PTB `JobQueue.run_repeating`**; drop both deps | `bot/jobs.py` already proves the model (`broadcast_job`/`settlement_job` at jobs.py:121-122 with `begin_nested` savepoints); Redis was unused even in Nabzar; idempotency comes from a DB unique constraint, not a job store |
| D3 | Duplicate Gemini SDK → reuse `core/gemini.py` | **One client.** Add budget-gated `generate_text()` + `translate_summarize_news()` mirroring `generate_image()`; delete `google-generativeai` + `claude.py` | PHM's gate (gemini.py:111-116) + `gemini_usage` ledger (already accepts `kind=`) is the entire point of reuse; SDK has no budget gate or `trust_env` control |
| D4 | Farsi-only → 4-lang | **`translations` JSON column** `{lang:{title,summary}}`, produced in ONE Gemini call for all target langs; **channel posts in one configured content lang — LOCKED to English (`news_channel_lang="en"`)**; bot DMs use the recipient's `User.language` slot | Resolves the column-explosion vs i18n tension; one JSON column scales to en/fa/ru/zh; one Gemini call = one budget charge; multi-lang *channel* posts would be 4× Telegram spam. Owner chose English as the channel language; per-user DMs are the multilingual layer |
| D5 | JWT/localStorage → session auth | **Reuse `dashboard/deps.py` verbatim**: `require_admin`/`require_superadmin`, `get_db`, `verify_csrf`, `deps.render`; drop `python-jose`, `passlib` | No new auth code; PHM already has session-cookie + CSRF + argon2 + `Admin` table |
| D6 | Single-channel → +per-user | **Both, layered**: one canonical channel post for approved items + a per-user DM stream (Off/Daily/Real-time) via the `Command` queue | The channel is the broadcast; per-user delivery is a separate DM layer keyed on `UserNewsPrefs` — a single channel cannot itself be per-user |
| D7 | Nabzar `Integration` cred table → env | **Drop the table.** Gemini/Telegram secrets live in `core/config.Settings` (env); the news channel id is a non-secret, admin-editable `app_config` scalar | Keyless-dashboard invariant forbids the dashboard holding/echoing secrets; no second config subsystem |
| D8 | Nabzar `Setting` JSON store → ? | **Decompose**: typed columns (`user_news_prefs`), `Category` rows (taxonomy), or `app_config` string scalars (knobs); **drop `tones` entirely** | `app_config.value` is `Text` (string K/V), not JSON; PHM has no tone-preset concept and tone-rewrite must run keyed anyway |
| D9 | `NewsItem.status` vocabulary (pipeline's 7-state `backlog→approved→translating→rendering→ready→published→rejected` vs data-model's 4-state `backlog→approved→sent→rejected`) | **Adopt the pipeline's fuller state machine, but keep `String(16)`+CheckConstraint** (never native enum) | The transient `translating`/`rendering`/`ready` states are load-bearing for the keyed render job's crash-resume; "approved/sent/rejected" remain the admin-visible labels. Rename Nabzar's misleading `pending`→`approved` |
| D10 | News prefs: `UserSettings.extra` JSON vs dedicated table | **Dedicated `user_news_prefs` + `user_topic_follows`** | Delivery fan-out needs indexed `WHERE delivery=… AND digest_hour=…`; SQLite can't index inside a JSON blob; M2M follows can't live in `extra` at all |
| D11 | `bot_users` (Nabzar) | **Drop** | PHM `User`/`UserSettings` fully supersede it |

## 3. Data Model

**Single migration chain.** `migrations/versions/` is empty — there is no committed baseline. So:
- **`0001_baseline.py`** (`down_revision=None`): autogenerate from current `db/models.py` to capture the live
  schema (users, accounts, categories, gemini_usage, app_config, …); `alembic stamp 0001_baseline` against
  existing prod/dev DBs so `create_all`-built schemas adopt the chain without re-creating tables.
- **`0002_news_pipeline.py`** (`down_revision="0001_baseline"`): all news objects + the `Category.kind` alter.

Authoring rules (from `migrations/env.py`, which already sets `render_as_batch=is_sqlite`, `compare_type=True`):
engine-agnostic ops only; wrap existing-table alters in `op.batch_alter_table`; **no `CREATE TYPE`, no
`sa.Enum(name=…)`** — every status is `String` + `CheckConstraint`; `server_default` on new NOT NULL columns
so existing rows backfill.

### 3.1 Changed table: `Category` (the only existing table touched)
Add one discriminator column (reuses the table as the news topic taxonomy — no parallel categories table):
```python
kind: Mapped[str] = mapped_column(String(12), default="market", nullable=False, index=True)
#  "market" (existing cards, backfill default) | "news" (topic only) | "both"
# CheckConstraint("kind in ('market','news','both')", name="ck_category_kind")
```
`db/repositories/categories.py` `needing_images`/`list_visible` extend to filter `kind`. News topics get the
existing Gemini-image budget lifecycle, pin/hide/order, and admin editing for free.

### 3.2 `news_sources`
PK `BigIntPK`. Columns: `name String(255)`, `url String(2048)`, `url_hash String(64)` **UNIQUE**
(`sha256(url)` — a 2 KB unique index is fragile), `category_id → categories.id ON DELETE SET NULL`,
`kind String(8)` (`auto|rss|html|telegram`, CHECK), `lang_hint String(8)`, `enabled Boolean index`,
`last_checked_at`, `last_status String(64)`, `created_at`/`updated_at`. Indexes: `(enabled, category_id)`.

### 3.3 `news_items` (the cache; dedup key)
PK `BigIntPK`. FKs `source_id`/`category_id` → `ON DELETE SET NULL` (preserve history). Core columns:
- `url String(2048)`, `url_hash String(64)` **UNIQUE** (cross-fetch dedup), `dedup_hash String(64) index`
  (`sha256(normalized title)` — cross-source same-story dedup)
- `lang_orig String(8)`, `title_orig Text`, `body_orig Text`, `hero_image_url String(2048)`
- **`translations JSON default dict`** = `{lang:{"title":…,"summary":…}}` (D4; mutate with `flag_modified`)
- `rendered_image_path String(512)` (under `cards_dir/news/`)
- `image_status String(12)` (`none|generating|ready|failed`, CHECK) — mirrors `Category`
- **`status String(16)`** default `backlog`, CHECK `in ('backlog','approved','translating','rendering','ready','sent','rejected')` (D9)
- `score Numeric(6,4)` (heuristic, 0–1), `excluded_from_autopublish Boolean`
- **CTA**: `market_id String(128)` (article-level hint), `cta_market_id String(128)` (resolved/pinned target),
  `cta_url String(512)`, `cta_resolved_at`
- **channel linkage**: `channel_msg_id BigInteger` (for edit/share)
- `fetched_at`, `approved_at`, `published_at`
- Indexes: `(status, score)`, `(category_id, status)`, `published_at`.

> Note on `status` width: data-model facet proposed `String(12)`; pipeline facet's
> `translating`/`rendering`/`ready`/`published` need `String(16)`. **`String(16)` wins** (D9).

### 3.4 `user_news_prefs` (per-user delivery)
PK `user_id → users.id ON DELETE CASCADE`. `delivery String(8)` default `daily` (CHECK `off|daily|realtime`),
`digest_hour SmallInteger` default 9 (CHECK 0–23, interpreted in the existing `UserSettings.timezone` — **do
not duplicate tz**), `quiet_start`/`quiet_end SmallInteger nullable`, `only_relevant Boolean`,
`max_per_digest SmallInteger` default 5, `last_digest_at`, `updated_at`. Index on `delivery`.

### 3.5 `user_topic_follows` (M2M over `Category`)
Composite PK `(user_id, category_id)`, both `ON DELETE CASCADE`. `created_at`. Index `category_id` (reverse,
for per-item realtime fan-out to followers).

### 3.6 `news_delivered` (per-user dedup ledger)
Composite PK `(user_id, news_item_id)` both `ON DELETE CASCADE`, `channel String(8)` (`digest|realtime`),
`sent_at`. Index `news_item_id`. The PK guarantees a user never gets the same item via both realtime and
digest. Delivery flows through the `Command` queue; this table is the dedup guard.

### 3.7 `news_channel_posts` (channel idempotency)
PK `BigIntPK`. `news_item_id → news_items.id CASCADE`, `chat_id BigInteger`, `message_id BigInteger`,
`lang String(8)`, `posted_at`. **UNIQUE `(news_item_id, chat_id, lang)`** so a re-running publish job posts
an item at most once per channel per language.

### 3.8 Migration data step (in `0002`, guarded `WHERE NOT EXISTS`)
- Seed four `Category` rows by slug (`economy`, `crypto`, `gold`, `iran`) with `kind="news"`,
  `tag_id/tag_slug=NULL`, `volume=0`, `hidden=False` — maps Nabzar's free-string categories onto the taxonomy.
- Seed `app_config` scalars: `news_channel_enabled="0"`, `news_top_n="5"`, `news_crawl_interval_s="900"`,
  `news_digest_default_hour="9"`. (`news_channel_id` left unset until an admin configures it.)
- `downgrade()`: drop tables in reverse FK order, then batch-drop `Category.kind`, then delete seeded
  `app_config` keys; **leave** the four `kind='news'` category rows (harmless; may be FK-referenced).

No ETL from Nabzar's Postgres is in scope; back-loading later is a one-off script (`category`→`category_id` by
slug, `*_fa`→`translations["fa"]`, `pending→approved`/`published→sent`).

**`gemini_usage` is unchanged** — news reuses `kind="news_text"` / `kind="news_image"`.

## 4. News Pipeline (on JobQueue)

New cohesive package `bot/news/` (registration only in `bot/jobs.py`). Three jobs with separate cadences and
fault domains; each writes a status flag the next job drains — the same `image_status` handoff PHM uses for
category cards.

```
bot/news/
  crawler.py     # RSS/HTML/Telegram auto-detect; httpx.AsyncClient(trust_env=settings.news_crawl_trust_env)
  rendering.py   # Pillow LOGO overlay only (NO baked article text); hero→AI→overlay
  publisher.py   # channel post + per-user fan-out via Command queue
  cta.py         # news → Polymarket market resolution + deep-link buttons
  jobs.py        # crawl_job, render_job, publish_job + register_news_jobs(application)
```

**`core/gemini.py` additions** (mirror `generate_image()` exactly — budget gate → `asyncio.to_thread` sync
REST call → `gemini_usage.record(kind=…)`):
```python
def _call_gemini_text(prompt: str, *, attempts: int = 3) -> str: ...   # responseMimeType=application/json, trust_env, retry
async def generate_text(session, *, prompt, kind="news_text", category_id=None) -> str | None: ...
async def translate_summarize_news(session, *, title, body, target_langs=SUPPORTED_LANGUAGES, tone_prompt="") -> dict[str, dict[str,str]] | None:
    """ONE call → {lang:{"title","summary"}} for all langs → ONE gemini_text_cost_usd charge."""
```
Text ($0.002) and image ($0.04) draw from the **same** weekly pool via `gemini_usage.weekly_spend()`.
`render_job` translates *before* AI-image gen, and AI-image is skipped (hero/placeholder) when budget is tight.

**`crawl_job`** (interval 900s, `first=45`): iterate `news_sources_repo.enabled()`; per-source `try/except`
so one bad feed doesn't abort the batch; per-article `session.begin_nested()` catching `IntegrityError` on
`url_hash` → skip (dedup). **No Gemini in crawl** — cheap heuristic score only (recency + source weight +
keyword match vs active `Category.tag_slug`s). `crawler.fetch_articles(url, kind, limit)` is the Nabzar port
**with `trust_env=False` added** (memory `vpn-blocks-egress` — Nabzar omitted it).

**`render_job`** (interval 120s, `first=90`): drain `needing_render` (status in `approved|translating|rendering`),
capture ids up front (savepoint expires ORM attrs), per-item:
1. `status="translating"` → `translate_summarize_news()`. **fa-skip generalization**: if `lang_orig` already
   equals a target lang, fill that slot passthrough; Gemini fills only missing langs. Budget exhaustion →
   `None` → passthrough source-lang, item still ships.
2. `status="rendering"` → `rendering.render()` (hero → AI fallback → Pillow logo overlay, NO baked text).
3. `cta_market_id = await cta.best_market_id(item)` (best-effort).
4. `status="ready"`.

**`publish_job`** (interval 60s, `first=120`): selects `ready` items, per-item `begin_nested`:
(a) `post_to_channel` (always — one chat, no fan-out), (b) `enqueue_realtime` (per-user
`Command(action="NEWS")`), set `status="sent"`/`published_at`. (c) Once/day at the digest window,
`enqueue_daily_digest`. **Approval-miss policy** (hard guard `NEWS_AUTOSEND_REQUIRES_APPROVAL=True`): publish
selects only explicitly approved/ready items; a missed window = no post that cycle, **never** promote-by-score.

Per-user delivery reuses the existing `Command` queue: **extend `broadcast_job`** (or a sibling consumer) to
handle `action in ("NEWS","NEWS_DIGEST")` — load the `NewsItem`(s), resolve the recipient's `User.language`,
render caption from `item.translations[lang]` (EN fallback) + `t()` chrome, attach the CTA keyboard, send
through the existing rate-limited `Forbidden`/`TelegramError` pipe.

**Deleted from Nabzar**: `workers/scheduler.py`+`publisher.py` (APScheduler), `services/gemini.py` (SDK),
`services/claude.py` (local CLI), `image_compositor` text-baking (logo overlay survives).

## 5. Channel Post + CTA Deep-Links

**CTA resolution** (`cta.best_market_id`, run once per item at render, cached on the row — never per-user): if
`market_id` set use it; else if the item's `Category.tag_slug` exists, take the top
`markets.category_markets(tag_slug, limit=1)`; else `markets.search_markets(title)`; else generic. All
blocking Polymarket calls wrapped in `asyncio.to_thread`. No new Polymarket client.

**Deep-link form** (reuses the `?start=` convention from `start.py:38-40`):
```python
def news_deeplink(bot_username, item_id, market_id) -> str:
    payload = f"nm-{market_id}" if market_id else f"n-{item_id}"
    return f"https://t.me/{bot_username}?start={payload}"
```
`bot/handlers/start.py::start()` arg loop (currently parses `r-<code>` at line 164) gains: `nm-<cond>` → jump
to the discover.py market panel (Buy YES/NO funnel — one tap from a trade); `n-<item>` → `news.show_item`.

**Channel message layout** (HTML parse mode, escaped via `common.esc()`, sent to `app_config["news_channel_id"]`):
```
<b>{title}</b>

{summary}

🔗 <a href="{source_url}">{source_label}</a>
{nfa_footer}
```
Inline keyboard with an **HTTP `url=` button** (not `callback_data` — callbacks don't fire from a channel a
user hasn't started, so this sidesteps the `menu:` regex allowlist entirely): label `bot.news.cta_trade`
("📈 Trade this") if `cta_market_id` resolved, else `bot.news.cta_open` ("📰 Open in bot"). Photo
(`rendered_image_path`, caption ≤1024) with text fallback (≤4096). `channel_msg_id` stored.

## 6. Per-User Personalization & Delivery

**Bot UX** — new self-registering `bot/handlers/news.py` (`CommandHandler("news")`,
`CallbackQueryHandler(on_news, pattern=r"^news:")`); wired in `bot/main.py` (+`BotCommand("news")`). In
`start.py`: add the `b("news","news")` tile to `dashboard_keyboard`, an `elif action=="news"` in `on_menu`,
and **CRITICAL — add `news` to the callback regex allowlist** (`start.py:310` `^menu:(…|news)$`) or the tile
silently no-ops.

**Settings screen** (mirrors `rewards_screen`; all `news:<verb>:<arg>` callbacks, own pattern):
```
[ Off ] [ Daily ✓ ] [ Realtime ]        news:mode:…
[ 🕘 Digest hour: 09:00 ]                news:hour   (0–23 picker)
[ 🌙 Quiet hours: 22–07 ]                news:quiet
[ 🎯 Only my topics: On/Off ]            news:relevant:toggle
[ 📌 Topics (3 followed) ]               news:topics  (multi-select over Category, ✅/▫️ from user_topic_follows)
```

**Delivery jobs** (two more `run_repeating`, staggered): `news_realtime_job` (60s) and `news_digest_job`
(600s tick, fires per-user when `local_hour == digest_hour` and `last_digest_at` not already today-in-tz).
- **Relevance** (`relevant_items_for`): base `NewsItem.score` + `+0.5` followed topic, `+0.4` open
  `Bet.market_id` match, `+0.3` open `Position.market_id` match; if `only_relevant` and no match → drop.
  Excludes anything in `news_delivered`.
- **Quiet hours**: if `now_local ∈ [quiet_start, quiet_end)` skip the tick (defer, don't drop); wrap-around
  (22→7) via `start>end`.
- **Bundling**: ≥2 items in a realtime tick → one message, up to 3 stacked, single button.
- **Rate-limit discipline** (reuse `broadcast_job` shape): ~25 sends/tick cap, `asyncio.sleep(0.05)` between,
  `except Forbidden:` → set `delivery='off'`, `except RetryAfter:` → sleep + defer rest, per-user
  `begin_nested` + `except: continue`. CTA read from cached `cta_market_id` — **no `to_thread` Gamma calls in
  the fan-out loop**.

**i18n**: `bot.news.*` + `bot.tile.news` keys added to **all four** `locales/{en,fa,ru,zh}.json` (CI gate
`test_i18n.py` blocks otherwise; identical placeholder sets, balanced markdown). Article *body* is data from
`translations[lang]` (EN fallback + `bot.news.untranslated_note`), never a catalog key. Farsi captions
`dir=rtl`.

## 7. Admin Dashboard (Jinja2)

New `dashboard/routers/news.py` (`include_router` in `app.py` next to `pages`). All routes **sync `def`**
returning `deps.render(...)` (GET) or `RedirectResponse(303)` (POST/PRG). One nav tab `/news` in `base.html`
after `miniapp`, hosting four sub-sections reproducing Nabzar's four screens. Auth verbatim:
`Depends(require_admin)` (`require_superadmin` for send-now/integration-test), `Depends(get_db)`,
`Depends(verify_csrf)` on every POST.

**KEYLESS INVARIANT (governs the whole workflow)**: the dashboard never calls Gemini/Telegram — it only
writes status flags; the keyed bot/worker acts on them (the `curate_category(action="regen")` discipline,
repo.py:246-249). Module docstring states this explicitly.

- **A. Approval Queue** (`news_queue.html`) — `GET /news?status=&category=&sort=&page=`; POST `approve`
  (→`approved`, `approved_at`, `excluded=False`), `reject` (→`rejected`), `unapprove`, `send-now` (superadmin
  → `Command(action="NEWS_PUBLISH")`, **does not send**), `fetch-now` (→`Command(action="NEWS_HARVEST")`).
  Filter `<select onchange="this.form.submit()">`. Images are **plain `<img src="/cards/news/{id}.png">`**
  (keyless static under the mounted `/cards`) — eliminates Nabzar's auth'd blob fetch.
- **A'. Item detail** (`news_item.html`, modeled on `category_edit.html`) — `GET /news/{id}`; per-lang edit
  textareas (fa `dir="rtl"`), tone/revise/revise-image are **flag writes**
  (`retranslate_status="pending"` / `revise_status="pending"` / `image_status="none"`) the keyed worker
  drains. Zero JS.
- **B. Sources** (`news_sources.html`) — CRUD + toggle + delete (`data-confirm` via existing `ui.js`); `test`
  is a **flag write** (`last_status="pending"`) probed by the crawl worker.
- **C. Settings** (`news_settings.html`) — autosend toggle, frequency (minutes/top_n), delivery-default radio,
  image template, logo upload (reuse `pages.py:324` magic-byte + 8 MB guard). **Stored as JSON-serialized
  strings in `app_config`** under namespaced keys (`news.frequency`, `news.image`, `news.autosend`). **No
  `tones`** (D8). Categories shown read-only (managed under `/miniapp`).
- **D. Integrations** (`news_integrations.html`) — **read-only status only**: `configured: bool` from
  `settings.gemini_api_key`/`telegram_bot_token` + last-test result cached in `app_config`; **no key input
  fields**; copy "Credentials are configured via server environment." `POST /news/integrations/{name}/test`
  (superadmin) enqueues `Command(action="NEWS_TEST_INTEGRATION")`; the keyed worker runs `get_me()`/Gemini
  ping and writes the result back. This is the single biggest deliberate divergence — required by the keyless
  invariant (as are the dropped live `tone/preview` and `image/preview` Gemini routes).

`dashboard/repo.py` gains a `# ── News ──` section (~15 sync, public-columns-only helpers: `list_news_items`,
`approve_news_item`, `set_news_tone`, `request_news_image`, `list_sources`, `news_settings`,
`news_integration_status`, …). `core/audit.py` gains `NEWS_*` `AuditEvent` members recorded on each POST.

## 8. Dependencies

**ADD** (crawl/parse/render only):
```
feedparser==6.0.11          # RSS
trafilatura==1.12.2         # HTML article extraction (pulls lxml, courlan, htmldate, justext)
beautifulsoup4==4.12.3      # t.me/s/ scraping (reuse lxml parser, NOT html5lib)
lxml>=5.2                   # explicit; ensure cp313 manylinux/macos-arm64 wheel
Pillow==10.4.0              # news card LOGO overlay only
# rapidfuzz==3.10.0         # DEFERRED — url_hash dedup suffices for v1
```
**DROP**: `apscheduler`, `redis`, `google-generativeai`, `claude-agent-sdk`, `python-jose`, `passlib`, and the
entire Next.js/React/Tailwind/Node toolchain. Net **+4 / −7** plus Node.

**Version pitfalls**:
- **lxml/trafilatura source-build risk** (the single most likely image-build regression) — pin `lxml>=5.2`;
  verify `pip install` resolves wheels in the existing `Dockerfile` (no `gcc`/`libxml2-dev` needed).
- **asyncpg ≥0.30** already pinned (first release with py3.13 wheels) — keep the floor.
- **Gemini model rename** — `gemini-2.5-flash-image` / new `GEMINI_TEXT_MODEL` (default `gemini-2.5-flash`)
  are env-override-safe; add a startup log of the resolved name and a `record(ok=False)` on 404.
- **Pillow** scoped to news cards (logo overlay only) — never route through the text-banning generator prompt.

New `core/config.py` knobs: `gemini_text_model`, `gemini_text_cost_usd` (0.002); `news_crawl_interval_seconds`,
`news_render_interval_seconds`, `news_digest_hour_utc`, `news_crawl_per_source_limit`, `news_logo_path`,
`news_crawl_trust_env` (default `False`), `news_channel_lang` (default `en` — LOCKED), `news_per_tick_cap` (25). The
news **channel id** is the non-secret `app_config["news_channel_id"]`; the bot **token** stays in env.

## 9. Security, Egress & Policy

- **Keyless invariant** (hard): dashboard runs without `ENCRYPTION_KEY` and never calls Gemini/Telegram. News
  rows carry no wallet/secret material; every Nabzar inline-API action (translate, apply-tone, revise,
  generate-image, send-now, fetch-now, source-test, integration-test) is a flag-write the keyed worker
  consumes. Drop the `Integration` cred table.
- **Egress** (memory `vpn-blocks-egress`): VPN returns 403 for Gemini and disrupts Telegram. Crawler's
  `httpx.AsyncClient` MUST take `trust_env=settings.news_crawl_trust_env` (default `False`) — Nabzar omitted
  this. Egress matrix: crawl needs broad arbitrary-host + `t.me` egress (new surface); Gemini needs VPN-off;
  publish needs `api.telegram.org`.
- **SSRF hardening** (new — crawl fetches admin-supplied URLs): `http(s)`-scheme allowlist, reject
  private/link-local/loopback IPs before fetch, cap redirects + body size + per-request timeout (15s).
- **Channel-admin requirement**: the bot must be an admin of the news channel. Startup self-check via
  `bot.get_chat`/`get_chat_member`, surfaced as an OK/FAIL badge on the integrations page. Channel id from
  `app_config`, never hardcoded `@nabzarsocial`.
- **Approval-miss → skip** (guard `NEWS_AUTOSEND_REQUIRES_APPROVAL=True`): un-approved items at digest fire
  are left for the next window, never auto-promoted.
- **Not-financial-advice footer**: every CTA-bearing post/digest item appends a localized
  `bot.news.nfa_footer` (×4 via `t()`, never hardcoded); presence asserted in tests (legally load-bearing
  once a market CTA is attached).
- **Budget**: all text/image calls route through `core/gemini.py`'s pre-call gate; news + category-image share
  **one** weekly ledger; exhaustion → `None` → item skipped/unsent. `gemini_text_cost_usd` alongside
  `gemini_image_cost_usd=0.04`.

## 10. Phased Rollout

| Phase | Scope | Egress? | Deploy target | Ships behind flag? |
|---|---|---|---|---|
| **0** | Deps + config knobs + Dockerfile cp313-wheel verification | No | both | additive, always-on |
| **1** | `0001_baseline` + `0002_news_pipeline` migrations; `core/gemini.generate_text()` + `translate_summarize_news()` | No (mocked HTTP to test) | both | n/a |
| **2** | `crawl_job` + `render_job` (`trust_env=False`, savepoint isolation); items land in DB, no publish | **Yes (to run)** | bot | disabled-by-default |
| **3** | Jinja2 `/news` admin (queue/sources/settings/integrations); flag-writes only | No (keyless) | dashboard | independently shippable |
| **4** | `publish_job` → channel + market CTA + NFA footer + channel-admin self-check | **Yes** | bot | flag (`news_channel_enabled`) |
| **5** | Per-user prefs + `news_realtime_job`/`news_digest_job` + `Command(action="NEWS")` consumer + news tile/command/settings (regex allowlist!) | **Yes (realtime)** | bot | depends on 1–4 |

## 11. Verification / Test Plan

**Unit (mock all HTTP)**: `generate_text` budget gate (over → `None` + `record(kind="news_text", ok=False,
cost=0)`; under → `ok=True`); crawler `url_hash` dedup, paywall-drop, `trust_env=False` asserted on
constructor, SSRF reject of private-IP/non-http; caption formatter (NFA footer **always** present with CTA,
HTML-escaped, ≤1024/≤4096 truncation, no hardcoded `@nabzarsocial`); approval-miss (un-approved item not sent);
fault isolation (one source raising → batch continues).

**i18n (CI-blocking)**: all new `dash.news.*`/`bot.news.*`/`dash.nav.news`/`bot.tile.news` keys in all 4
locales; placeholder parity; balanced `*`/`_`.

**Dashboard auth/no-leak**: `/news` → `require_admin`; POST → `verify_csrf`; unauth → 303 `/login`; **no-key
assertion** — patch `core.gemini` to raise if invoked from the dashboard process and exercise approve/regen →
assert Gemini never called.

**Migration**: `upgrade head` + `downgrade` on **both** SQLite (aiosqlite, `PRAGMA foreign_keys=ON`) and
Postgres; assert `String(16)` status (no native enum), batch mode on SQLite, `0001 stamp` adopts existing
schemas.

**Staging dry-run (egress)**: VPN-off Gemini smoke (one fixture article) + VPN-on → graceful skip not crash;
crawl 1–2 fixture RSS + one `t.me/s/`; publish to a **private** test channel (photo/text fallback, NFA footer,
CTA deep-link opens the bot at the right market, channel-admin check passes); budget-exhaustion drill.

## 12. Open Questions for the Owner

1. **Article-content translation breadth (cost driver)**: translate every article into all 4 languages, or
   only the langs actually demanded by subscribed users / the channel lang? Plan assumes "all target langs in
   one Gemini call" — confirm, or switch to lazy per-demand translation to cap cost.
2. **Channel content language**: ✅ RESOLVED — owner chose **English** (`news_channel_lang="en"`); per-user
   DM translations are the multilingual layer.
3. **Post-revision tooling**: confirm cutting `claude.py` (local CLI) for v1. Admin "revise" becomes a Gemini
   re-tone (reset to `backlog`). If free-form revision is required, approve a budgeted Anthropic **API** call?
4. **Prod egress posture**: will production run behind the same OpenVPN? If so, crawl needs a routing-level
   carve-out (split-tunnel/allowlist) — `trust_env=False` bypasses proxy env vars, not a routing block.
5. **Source-set governance**: who curates `news_sources`, and do we keep Nabzar's fragile/ToS-gray `t.me/s/`
   Telegram scraping for v1, or restrict to RSS + single-article HTML?
6. **Budget split**: shared `GEMINI_WEEKLY_BUDGET` for news + category images (simplest — recommended), or a
   separate news sub-budget so a news burst can't starve category-card generation?

---

### Key files this plan creates/touches

- **Create**: `bot/news/{crawler,rendering,publisher,cta,jobs,__init__}.py`; `bot/handlers/news.py`;
  `db/repositories/{news_sources,news_items,news_prefs,topics,news}.py`; `dashboard/routers/news.py`;
  `dashboard/templates/{news_queue,news_item,news_sources,news_settings,news_integrations}.html`;
  `migrations/versions/{0001_baseline,0002_news_pipeline}.py`.
- **Edit**: `db/models.py` (6 new tables + `Category.kind`); `core/gemini.py`; `core/config.py`;
  `core/audit.py`; `bot/jobs.py`; `bot/handlers/start.py` (tile, `on_menu`, **regex allowlist at line 310**,
  `start()` arg parsing); `bot/main.py`; `dashboard/app.py`; `dashboard/repo.py`;
  `dashboard/templates/base.html`; `db/repositories/{appconfig,categories}.py`; `db/bootstrap.py`;
  `requirements.txt`; `Dockerfile`; `locales/{en,fa,ru,zh}.json`.
- **Reuse unchanged**: `dashboard/{deps,auth}.py`, `dashboard/static/*`, `dashboard/templates/_macros.html`,
  `db/repositories/gemini_usage.py`, `polymarket/markets.py`, `db/engine.async_session_scope`, `core/i18n`,
  `bot/handlers/common.py`, the `Command`/`broadcast_job` rate-limit+savepoint pattern, `UserSettings.timezone`.
