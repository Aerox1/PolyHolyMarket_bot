# Bet-on-This CTA — Feature Analysis & Build Plan

> Status: **BUILT (2026-06-03).** All three phases implemented, reviewed (3-lens adversarial pass +
> per-finding verification), and shipping behind the news pipeline. 210 tests pass; migration `0003` round-trips.
> The §"Corrections" call-outs below were the pre-build findings; the "As-built" notes record where the
> implementation diverged from / hardened the naïve plan.

## As-built notes (what shipped, and how it differs from the plan)

- **No snapshot columns.** The plan proposed caching `cta_yes_token/…/price` on `NewsItem`. Dropped: a bet tap is
  deliberate + low-frequency, so the outcome→token is resolved **fresh at click** via `markets.get_market_state`
  (the same pattern as `/market`). This eliminates stale-token drift and the whole "snapshot vs live" reconciliation
  problem, and `publisher.snapshot()` / `render_item` need no change. Channel buttons are plain **Bet YES / Bet NO**.
- **`get_market_state(condition_id) → ('open'|'closed'|'error', market)`** distinguishes a resolved/closed market from a
  transient Gamma/VPN blip, so a network hiccup shows "try again" (retryable) instead of permanently killing an intent.
- **Slippage guard is mandatory for news bets.** `client.place_capped_buy` = FOK *limit* at `entry×(1+NEWS_BET_SLIPPAGE)`
  (default 5%, clamped 0.01–0.99). If a news market can't be priced, the funnel **refuses** rather than fall back to an
  uncapped market order.
- **Force-confirm:** every `source='news'` bet always shows ✅/❌ regardless of the user's `confirm_trades` pref
  (Decision 8 → resolved as force-confirm), so "never auto-place" is literally true end-to-end. Resume lands on the
  picker only; the intent is marked `resumed` **only after** the picker renders (else left `pending` for the TTL reaper).
- **`Bet.source='news'`** (4 chars, fits `String(8)` — no schema change to `Bet`). `create_bet` is wired news-only via
  `confirm._record_news_bet`; `market_id`+`entry_price` are threaded through the intent so the bet is settleable.
- **Intent reaper runs unconditionally** (not gated to `NEWS_PIPELINE_ENABLED`) — an `nb-` link can create an intent
  even with the pipeline off, so the table must always stay bounded.
- **Known v1 limitation:** the picker→confirm last hop still reads `user_data`; a bot restart between resume and the
  amount tap drops to "outdated" (the user re-taps; the intent is reaped at TTL).

---

> Status: **analysis / not started.** Produced by a multi-agent mapping + design + adversarial-review pass.
> The §"Corrections" call-outs below are findings from the adversarial review that **change the naïve plan** — read them before building.

## 1. What the feature is, and what we already have

**The feature.** Under each news-channel item, surface a direct "bet on this" CTA. A tap takes the user from the
channel straight toward placing a market BUY on the item's resolved market — for already-connected users it lands
them on the amount picker in one hop; for new users it onboards them through the existing connect flow and then
resumes the same bet they intended.

**What already exists and is reused (no rewrite needed):**

| Capability | Real name / location | What it gives us |
|---|---|---|
| News deep-link | `news_deeplink()` → `?start=n-<item_id>` (`bot/news/cta.py`) | Channel→bot entry under the 64-char cap; encodes a small `item_id`, not the 66-char conditionId |
| Render-time market resolve | `best_market_id()` (`cta.py`), cached as `cta_market_id` on the row (`render.py`, `db/models.py`) | The market is already resolved once at publish time |
| `/start` payload routing | `bot/handlers/start.py`, branches for `r-`/`n-` | Where a new `nb-` branch slots in |
| Click-time market panel | `show_market_by_id()` (`bot/handlers/discover.py`) | Fetches market fresh, new generation id, stashes in `user_data`, renders buy panel |
| Amount picker + buy callbacks | `_AMOUNTS=(5,10,25,50)`, `on_buy`/`on_buy_amount` (`discover.py`) | Preset amounts and `buyamt:<gen>:<idx>:<side>:<amt>` callbacks |
| Confirm + execution spine | `confirm.make_intent` → `confirm.request` → `_execute` (`bot/handlers/confirm.py`) | Confirm-gating, signing via `get_trading_client`, `place_market_order` (FOK), order logging |
| Outcome→token resolution | `markets.get_market(condition_id)` → `{yes_token,no_token,…}`, token order `[YES,NO]` (`polymarket/markets.py`) | Maps a chosen outcome to a token_id **server-side** |
| Connect flow | `connect.register()` ConversationHandler, entry `menu:connect`, success in `enter_key()` (`bot/handlers/connect.py`) | Existing onboarding we hook resume into |

Two real gaps to close: (a) channel posts have only one generic button today; (b) `confirm._execute` logs orders but
**never calls `create_bet`** for Telegram trades — so news bets wouldn't settle or count toward rewards unless wired.

---

## 2. The core technical crux — carrying an intended bet through onboarding

A new user taps "Bet NO," but has no wallet. The intent (which item, which outcome) must survive a **multi-message
connect conversation** and then resume.

**Why the obvious store fails.** `context.user_data` is the wrong home: the connect ConversationHandler wipes
`user_data['connect']` (`_clear_connect`), and `confirm` pops `pending_orders` with a 120s TTL. Both are gone long
before a user finishes pasting a key, and neither survives a restart.

**Mechanism: a persisted `pending_intents` row, keyed by `user_id`.**
- Written **only when the user is not connected** (connected users skip straight to the picker — no row needed).
- Stores non-secret essentials: `user_id`, `news_item_id`, `market_id`, `token_id`, `outcome`, `entry_price`,
  `question`, `status='pending'`, `expires_at = now+24h`, `idempotency_key = sha256(user_id:item_id:outcome)`.
- Consumed by `resume_after_connect(update, context, user_id)` called from the **success branch of
  `connect.enter_key()`** (after key zeroization). It picks the newest non-expired pending row, **re-fetches the
  market live**, flips the row to `resumed`, and drops the user on the amount picker.

**Critical safety choice:** resume **never auto-places** the bet. It lands on the amount picker, requiring an explicit
amount tap and the user's normal confirm preference. Connecting a wallet must never silently spend money.

> **⚠ Correction (review):** durability is only solved **up to the picker**. The final picker→confirm hop
> (`buyamt:<gen>:…` → `_resolve`) still reads a `user_data` stash (`disc_markets`/`disc_gen`) written by
> `show_market_for_bet`. If the process restarts *between* resume and the amount tap, the stash + generation are gone
> and the tap yields "outdated" — the bet silently dies while the intent row is already `resumed`. So we also need:
> (a) a reaper for `resumed`-but-never-`fulfilled` rows, and ideally (b) the picker to be able to re-hydrate its stash
> from the intent row on a cold tap rather than relying solely on `user_data`.

---

## 3. The two user journeys

### Existing (connected) user — tap → amount picker, no YES/NO step
1. Taps **🟢 Bet YES** in the channel → `t.me/<bot>?start=nb-1234-y`.
2. Middleware (group −1): `get_or_create_user`, caches `db_user_id` + lang.
3. `start()` parses `nb-1234-y` → new `_open_news_bet(item_id=1234, outcome="YES")`.
4. Load `NewsItem 1234`; read `cta_market_id` + token snapshot; pick `yes_token`/`yes_price`.
5. `accounts_repo.resolve_account()` returns an account → connected path.
6. Freshness/closed guard: if snapshot stale (>30 min) or closed, re-fetch `markets.get_market(cta_market_id)` in a
   thread; if now closed/resolved → `bot.news.bet_closed` and stop.
7. New `discover.show_market_for_bet(…, preselect_outcome="YES")`: `_new_gen` + `common.stash` like
   `show_market_by_id`, then renders the **amount row directly** (skips the YES/NO panel), buttons
   `buyamt:<gen>:0:yes:<amt>`.
8. User taps **$25** → existing `on_buy_amount` builds `make_intent("market", side="buy", token_id, amount=25,
   outcome="YES")` → `confirm.request`.
9. `confirm.request` shows ✅/❌ per `confirm_trades` pref (or executes) → `_execute` → `place_market_order` → logged
   (+ new `create_bet`).

### New (not-connected) user — intent survives onboarding
1. Taps **🔴 Bet NO** → `/start nb-1234-n`; middleware creates/loads user.
2. `_open_news_bet` resolves outcome=NO → token from snapshot; `resolve_account()` → `None`.
3. **Persist intent before onboarding:** upsert `pending_intents` (status `pending`, `expires_at=now+24h`,
   `idempotency_key=sha256(user:item:NO)`). On-conflict updates the same row (double-tap safe).
4. Show `bot.news.bet_connect_prompt` ("To bet NO on '<headline>', connect your wallet first") → existing
   `menu:connect` entry (no new connect entry point).
5. User completes connect (`CHOOSE_TYPE → [ENTER_FUNDER] → ENTER_KEY`).
6. **Resume hook** at end of `enter_key()` success (after `set_active_account` + invalidate, after key zeroized):
   `resume_after_connect` finds the newest pending row, re-fetches market live, re-resolves the NO token, flips row to
   `resumed`, re-enters `show_market_for_bet(preselect_outcome="NO")`. No pending row → behaves exactly as today.
7. If the market closed during onboarding: mark intent `expired`, show `bet_closed`; the connect success still stands.

> **⚠ Correction (review):** the resume render runs inside the security-critical `enter_key` where
> `update.callback_query is None` **and** `update.message was already deleted** (key-message delete-first). Helpers
> that reply via `update.effective_message` will fail — the resume render path **must pass an explicit `chat_id`** and
> use `context.bot.send_message`, like the rest of `enter_key`. The new import/call must be wrapped best-effort so it
> can never raise into the connect success path. Also: resume currently fires from `enter_key` **regardless of how
> connect was started** — a user who typed `/connect` for unrelated reasons gets yanked into a stale bet picker. Decide
> whether to gate resume on "arrived via `bet_connect_prompt`" (see Decision 4).

---

## 4. Key design decisions (recommended defaults)

| Decision | Recommendation | Why |
|---|---|---|
| **Channel UI** | Two `url` buttons **Bet YES / Bet NO** when `cta_market_id` is set; else today's single "Open in bot" | Channel buttons must be `url` (callback_data is dead in public channels), so deep-links are mandatory; pre-choosing outcome removes a tap |
| **Deep-link payload** | `nb-<item_id>-<y\|n>[-<amt>]`; `nb-` distinguishes from `n-`/`r-` | `nb-1234-y` ≈ 17 chars vs 64-char cap; charset `[a-z0-9-]`; conditionId never encoded |
| **Outcome→token** | Render-time snapshot + click-time refresh when stale (>30 min)/missing/closed | Keeps the hot path off Gamma (helps ~1.6s TTFB) while staying correct; outcome is the user's explicit tap, never NLP-inferred |
| **Amount selection** | Existing presets `(5,10,25,50)`; no typed entry in v1; add "Switch outcome" to fix mis-taps | Funnel has no free-text amount entry today |
| **Deferred-intent storage** | Persisted `pending_intents` table, not `user_data` | `user_data` is wiped by connect / expires in 120s in confirm |

> **⚠ Correction (review) — show prices on the channel button?** A frozen "Bet YES (78%)" printed on a channel
> message is **never refreshed** and can be arbitrarily wrong by tap time (hours/days later). The 30-min refresh only
> fixes the in-bot picker *after* the tap. Recommend **plain "Bet YES / Bet NO"** on the channel (no percentages), and
> show live odds only in-bot post-refresh. This is a compliance/NFA point, not cosmetic (Decision 3).

---

## 5. Phased plan — ship minimal, evolve to durable

| Phase | Scope | Effort | Exit criteria |
|---|---|---|---|
| **A — Channel buttons + payload + snapshot** | 4 new `NewsItem` columns + migration; snapshot fetch in `render_item`; two-button `build_keyboard`; `bet_outcome_deeplinks()` in `cta.py`; `nb-` parsing in `start.py` (keep `n-` intact) | **M** | A published item shows Bet YES/NO; `nb-` links parse and route; malformed payloads fall through to dashboard like a bad `n-` |
| **B — Fast funnel for connected users** | `_open_news_bet` + `discover.show_market_for_bet` (thin wrapper over existing stash + amount row); reuses `on_buy_amount`/`confirm` unchanged | **S** | Connected user taps Bet YES → amount picker (no YES/NO step) → places via existing confirm path; closed/stale → `bet_closed` |
| **C — Deferred intent + resume + bet recording** | `pending_intents` table/repo/migration; `resume_after_connect` hook; `create_bet` wiring + intent linkage in `_execute`; expiry/`resumed`-reaper tick; i18n | **L** | New user taps Bet NO → connect → resumes to picker for the same outcome; restart-safe; bets recorded; stale intents reaped |

> **⚠ Correction (review) — Phase A is under-scoped as written.** Three concrete edits are missing from its scope:
> 1. `best_market_id()` returns **only a condition_id** and discards the normalized dict — to snapshot yes/no
>    tokens+prices, `render_item` needs a **second `markets.get_market()` round-trip** (or refactor `best_market_id` to
>    return the dict).
> 2. The channel button is built from `item.cta_url`, a **static `n-<id>` link set once** in `render_item`. Two
>    `nb-<id>-y`/`nb-<id>-n` buttons **cannot reuse `cta_url`** — `build_keyboard` needs new inputs.
> 3. `publisher.snapshot()` is the detached object the publisher renders from and it **does not carry** the new
>    `cta_yes_token/cta_no_token/…price` columns — `snapshot()` must be edited too, or the buttons can't read them.
>
> Phase A is still independently shippable, but plan for these three edits. Phases A+B are low-regression (additive
> wiring on the trading spine); Phase C touches the security-critical connect path and lands last, carefully.

---

## 6. Data model changes

House style: `String`+`CheckConstraint` enums (never native enum), `Numeric` for money, `BigIntPK` variant,
`MutableDict.as_mutable(JSON)`.

**`NewsItem` — add 4 nullable columns** (`cta_market_id`/`cta_url`/`cta_resolved_at` already exist):
`cta_yes_token String(128)`, `cta_no_token String(128)`, `cta_yes_price Numeric(10,6)`, `cta_no_price Numeric(10,6)`.

**New table `pending_intents`** (Phase C):
```
id              BigIntPK
user_id         BigInteger  FK users.id ON DELETE CASCADE
account_id      BigInteger  NULL FK accounts.id ON DELETE SET NULL
news_item_id    BigInteger  NULL FK news_items.id ON DELETE SET NULL
market_id       String(128)
token_id        String(128)
outcome         String(8)     CHECK in ('YES','NO')
amount_usd      Numeric(20,6) NULL
entry_price     Numeric(10,6) NULL
question        Text NULL
source          String(16)  default 'news_channel'
status          String(16)  default 'pending'  CHECK in ('pending','resumed','fulfilled','expired','cancelled')
idempotency_key String(64)  UNIQUE
created_at      timestamptz default now()
expires_at      timestamptz
```
Index `ix_pending_intents_user_status (user_id, status)`. **Hand-edit the Alembic revision** — autogenerate does not
reliably emit CHECK constraints or composite indexes (matches the `0002` experience).

New repo `db/repositories/pending_intents.py`: `upsert_intent`, `latest_pending`, `mark`, `expire_stale` — same
flush-before-return / pure-status-transition pattern as `bets.py`/`commands.py`.

> **⚠ Correction (review) — `Bet` DOES need attention (the "no Bet schema change" claim is wrong):**
> - **`Bet.source` is `String(8)`.** `'news_channel'` is 12 chars → won't fit (raises on Postgres, truncates on
>   SQLite). The webapp already uses `'miniapp'` (7) to fit. **Either widen `Bet.source` or use a ≤8-char tag like
>   `'news'`** (Decision 1).
> - **`create_bet` requires `market_id`, `question`, `outcome`, AND `entry_price`** (entry_price drives
>   `shares = amount/entry_price` and all settlement math). But today's intent dict from `on_buy_amount` carries only
>   `token_id, amount, side, title, outcome` — **no `market_id`, no `entry_price`.** Wiring `create_bet` into
>   `_execute` is therefore **not one line**: `market_id` + `entry_price` must be threaded into `make_intent` at every
>   call site (discover, trading, positions_ui), or news bets record un-settleable rows. Because Phase C wires
>   `create_bet` for *all* Telegram trades, every existing `/buy` and discover buy must also start carrying these
>   fields (Decision 2).

---

## 7. Edge cases & failure modes

| Case | Handling |
|---|---|
| Render never snapshotted tokens (Gamma error) | Channel still shows YES/NO (`cta_market_id` set); click-time refresh repopulates; if that fails → `bet_closed`/unavailable, nav fallback |
| Market closed/resolved before click | `get_market` → `None` → `bot.news.bet_closed`; no stash, no dangling intent |
| Market closes during onboarding | Resume re-fetches; on `None` marks intent `expired`, shows `bet_closed`; connect success still shown |
| Snapshot stale but open | `cta_resolved_at` >30 min → live refresh so picker uses current price/token |
| Insufficient balance | FOK; funding errors surface the existing message; no bet recorded |
| Market unresolved / no CTA | `cta_market_id is None` → keep single "Open in bot" → legacy `_open_news_item` dashboard fallback |
| Deep-link expiry | `expires_at = created_at+24h`; cleanup tick marks past-due `expired`; resume ignores them |
| Stale callback after arrival | Existing discover generation guard invalidates old `(gen,idx)` |
| Double-tap same outcome | `idempotency_key=sha256(user:item:outcome)` → one row upserted |
| Multi-account | Token/market are user-independent; `get_trading_client(user_id)` resolves the **active** account at `_execute`; intent stores `user_id` only |
| Malformed/unknown payload | `start.py` validation fails closed → dashboard, like a bad `n-` |

> **⚠ Corrections (review) — additional edge cases the naïve plan misses:**
> - **`get_market()` conflates many `None` cases.** It returns `None` for *resolved* OR *inactive* OR *any non-200
>   (timeout/500/VPN-egress)*. A transient Gamma blip at click time → user sees "bet closed"; at resume it marks the
>   intent **permanently `expired`**. Given the documented VPN/egress fragility, this is likely in practice —
>   **distinguish transient failures from genuine closure** (retry / "try again later" vs `bet_closed`).
> - **Snapshot-token vs live-token reconciliation.** Click-time refresh re-fetches by the *same* condition_id, so YES
>   still maps to `tokens[0]` — but nothing verifies the refreshed `tokens[0]` equals the snapshot's `cta_yes_token`.
>   If Polymarket re-issued tokens, the user taps "Bet YES" against a different token. Add a check.
> - **Double-tap across *different* outcomes** (YES then NO) creates two rows; "newest wins" is a silent product
>   decision (Decision 5).
> - **In-bot `nb-<id>-y-<amt>` amount must be clamped** to `_AMOUNTS=(5,10,25,50)`. An attacker-supplied `-<amt>`
>   deep-link could otherwise skip the picker with an arbitrary amount. Validate `amt ∈ presets` or ignore it.
> - **Void-at-settlement** for a news bet only works if `create_bet` recorded `entry_price`/`market_id` correctly —
>   ties back to the `create_bet` data-flow gap.

---

## 8. Security & compliance

- **Tamper-resistance:** the deep-link encodes only `item_id` + `y|n` (public, non-sensitive). An edited payload can at
  most pick a different valid item or flip the outcome — never inject a token. `token_id` is **always** resolved
  server-side from the trusted `NewsItem` snapshot / live Gamma. (Verified: `nb-`/`n-`/`r-` prefixes are
  collision-free in `start.py`.)
- **Replay:** `idempotency_key` collapses repeated clicks into one pending row; the bet is created only after explicit
  confirm + a real CLOB order.
- **Confirm-gating reused:** betting flows through `confirm.request`/`_execute`; we deliberately **do not** auto-place
  on connect.
- **NFA:** keep the disclaimer; the bot never recommends — YES/NO is always the user's explicit tap.
- **Audit:** log `intent_created`/`intent_fulfilled`/`intent_expired` to `AuditLog`.
- **Key safety during resume:** `resume_after_connect` runs **after** the key is zeroized; it only reads
  `pending_intents` + public market data. (See §3 correction on `chat_id`/best-effort.)

> **⚠ Correction (review) — slippage protection is overstated.** A market BUY is FOK, but **FOK caps *fill size*, not
> *price*.** It rejects only if the full notional can't fill *at all* — it will still fill at a materially worse average
> than the snapshot if the book has depth at worse levels. There is **no limit price** anywhere in the market-buy path.
> "Fails rather than fills badly" is **not** real slippage protection. Decide whether news bets (originating from a
> public channel) need a **max-slippage / limit-style guard** (Decision 6).

---

## 9. Decisions for the owner

1. **`Bet.source` is `String(8)`** — `'news_channel'` (12) won't fit. Widen the column (schema change) or use `'news'`?
2. **`create_bet` scope** — wiring it into `_execute` retroactively makes *all* Telegram trades settleable and forces
   threading `market_id`+`entry_price` through every intent builder. Do that globally now, or a news-only path first
   (accepting inconsistency)?
3. **Channel button odds** — plain "Bet YES / Bet NO" (recommended, avoids stale-odds/NFA risk) or print `(78%)/(22%)`?
4. **Resume trigger** — only when the user arrived via `bet_connect_prompt`, or auto-resume after *any* connect
   (risk: surprising a user who typed `/connect` for other reasons)?
5. **Two pending intents (YES then NO)** — last-tap-wins, or make the user re-choose at resume?
6. **Slippage** — accept FOK-only (no price cap), or add a max-slippage guard on news bets?
7. **TTLs** — intent 24h vs 72h; snapshot freshness 30 min (tighter = fresher odds but more Gamma egress under VPN).
8. **Force-confirm news bets** regardless of `confirm_trades` pref? **Multi-wallet** — bet from the active account, or
   force a wallet picker first?

---

## Verdict (review)

**Directionally sound, not yet implementation-ready.** Architecture is good: reuse the confirm/`_execute` spine,
resolve `token_id` server-side from a trusted snapshot/live Gamma (never the deep-link), persist intent in a DB row,
never auto-place on connect; deep-link tampering is genuinely contained and the `nb-`/`n-` split is collision-free.
**Before building, resolve the blocking gaps:** (1) `Bet.source` width, (2) `create_bet` needs `market_id`+`entry_price`
threaded through intents, (3) Phase A's three missing edits (`best_market_id` dict, `cta_url`→two-button keyboard,
`publisher.snapshot()` columns). Also fix: overstated slippage claim, resume's last-hop `user_data` dependency, and
`get_market()`'s collapsed `None` (transient failure → permanent dead intent). Phases B and the security posture are
otherwise solid.
