# Migrating Polymarket-BOT to MongoDB — Full Analysis & Migration Plan

## Executive Summary & Bottom-Line Recommendation

**Bottom line, stated honestly up front: MongoDB is the wrong target for this system, and a complete, executable Mongo plan is provided below regardless.** This codebase is an integrity-dense, real-money/real-points workload that is *already designed for PostgreSQL* (`BigInteger().with_variant`, dual psycopg/asyncpg drivers, `PRAGMA foreign_keys=ON` explicitly to "emulate Postgres on SQLite"). Its correctness rests on exactly the relational primitives Mongo does not provide natively: 30 foreign keys with `ON DELETE CASCADE`/`SET NULL`, 13 `CHECK`-enforced enums/state machines, 13 `UNIQUE` idempotency guards, and a cross-document SQL transaction wrapping the rewards/referral money fan-out.

**Recommended path:** finish the move you already started — **SQLite → PostgreSQL** (change a connection string, run `alembic upgrade head`; ~1 day; every FK, CHECK, UNIQUE, Alembic revision, and all 879 tests survive unchanged), then harden the three genuine concurrency bugs *in Postgres* (`SELECT … FOR UPDATE` on `UserStats`, `INSERT … ON CONFLICT` + a `UNIQUE(user_id,reason,utc_day)` partial index for ledger idempotency, a guarded atomic counter or advisory lock for the Gemini budget) and stop the `float()` coercion so `NUMERIC` stays exact.

**Cost contrast:**

| Option | Effort | Preserves FK/CHECK/UNIQUE/ACID? | New ops dependency |
|---|---|---|---|
| **SQLite → Postgres** (recommended) | ~1 day | Yes — all of it, unchanged | None (you already target it) |
| **→ MongoDB** (this plan) | **~42–56 dev-days (≈9–12 wks solo)** | No — must be rebuilt by hand | Replica set in dev/CI/prod *if* transactions used |

**If the decision to adopt MongoDB is already made** (platform mandate, polyglot strategy, operational preference), the rest of this document is a complete, phased, executable plan: a 24-collection design with the table→collection mapping, `Decimal128` money handling, the unique indexes that *are* the idempotency guarantees, JSON-Schema validators replacing CHECK constraints, a Beanie+PyMongo driver split that preserves the keyless-dashboard invariant, an ETL with an ID strategy, per-module rewrite effort, a transaction/replica-set strategy that minimizes the ops tax, a test strategy, and a strangler rollout. Where Mongo *improves* on the current SQLite design (atomic counters, atomic command-claim, free per-document insert isolation), that is called out — but those same fixes are cheaper in Postgres.

**One nuance that cuts both ways:** the money columns are typed `Mapped[float]` and `float()`-coerced in 15 repo reads today, so there is a latent precision smell *regardless of database*. Mongo `Decimal128` would fix it — but so would Postgres `NUMERIC` + a `Decimal` discipline, and the natural Python `float`→BSON `double` path actively *risks* reintroducing the drift on Mongo unless every money field is validated as `decimal` at write time.

---

## 2. Current Data Layer and Why It Matters for Mongo

### 2.1 What exists today

| Layer | Detail | Confirmed |
|---|---|---|
| ORM / driver | SQLAlchemy 2, dual engines: sync (psycopg / aiosqlite) + async (asyncpg / aiosqlite), one shared `Base.metadata` | `db/engine.py` (106 ln) |
| Schema | 18 tables, **30** ForeignKey, **13** CheckConstraint, **13** unique declarations, **26** `Numeric(...)` columns | `db/models.py` (641 ln) |
| Repositories | 17 async/sync repos | `db/repositories/*.py` (accounts, appconfig, bets, categories, commands, gemini_usage, news_delivery, news_items, news_prefs, news_sources, orders, pending_intents, rewards, stats, users) |
| Migrations | 5-revision linear Alembic chain, `BigInteger().with_variant(Integer,'sqlite')`, `render_as_batch`, `compare_type` | `migrations/versions/0001…0005` |
| Tests | **879** test functions across **46** files; pristine SQLite DB rebuilt per test | `tests/conftest.py` |
| Concurrency control | SQLite WAL + `busy_timeout=5000` + `foreign_keys=ON`; **zero** `with_for_update`/`FOR UPDATE`/`version_id` anywhere; safety rests on append-only inserts + UNIQUE indexes + single-writer jobs (`max_instances=1`) | verified |
| Keyless dashboard | Dashboard runs *sync*, never holds `ENCRYPTION_KEY`, never decrypts; stores ciphertext only (`encrypted_private_key`, `encrypted_api_creds`) | `dashboard/`, `core.crypto` |

### 2.2 The four consistency mechanisms that define correctness

1. **Claim-commit-send-finalize (at-most-once channel delivery)** — `bot/news/jobs.py:149-188` `_publish_one`: (1) in a committed tx, INSERT the `news_channel_posts` claim row guarded by `UNIQUE(news_item_id,chat_id,lang)`, catching `IntegrityError` if a concurrent run won; (2) the irreversible Telegram send happens **outside any DB transaction**; (3) a fresh tx writes `message_id`+`status=sent`, or `DELETE`s the claim to release it on transient failure. Reconcile-not-resend on an existing claim.
2. **Idempotency-key upsert (pending bet intents)** — `pending_intents.upsert_intent`: `UNIQUE` on `sha256(user:item:market:outcome)`, read-then-insert-or-update.
3. **Crawl dedup race tolerance** — per-article insert inside `session.begin_nested()` (SAVEPOINT), `UNIQUE url_hash` + swallowed `IntegrityError`; plus a *non-unique* `dedup_hash` soft check with a known TOCTOU window.
4. **Cross-document money transaction** — `reward_for_bet` fans out into `award(bet)` + maybe-streak award + `_maybe_unlock` (two awards + a `Referral` status flip) + `_propagate` (up to 5 awards across *different users*), **all in one enclosing `async_session_scope`**.

### 2.3 Why each matters for Mongo (the crux)

- Mechanisms (1)–(3) **port cleanly** to single-document atomicity (unique index + `E11000` catch; per-document insert isolation is free). These are the low-stakes parts.
- Mechanism (4) is the **one place multi-document ACID is load-bearing** — and it adjudicates real money/points. On a standalone `mongod` it splits in half on a crash.
- The 30 FK cascades, 13 CHECK enums, and the SUM-derived balances (`points_ledger`, `gemini_usage`) have **no Mongo equivalent** and become application responsibility, JSON-Schema validators, or aggregation pipelines.
- The status state machines are advanced by **non-atomic** `WHERE status='x'` UPDATEs, correct today only via single-writer jobs — they must become atomic `findOneAndUpdate` CAS per callsite.

---

## 3. Risk / Impact Analysis (severity-ranked, candid on money/ACID/constraints)

| # | Risk | Severity | Why |
|---|---|---|---|
| **C1** | **Cross-document ACID lost for the rewards/referral fan-out** → partial-applied money/points (inviter paid, invitee bonus lost; `Referral` flipped to `unlocked` but signup bonus never inserted). Forces a **replica set** in dev/CI/prod to restore atomicity, *or* a hand-rolled idempotency-keyed saga (more code, more failure modes). | **CRITICAL** | `reward_for_bet` commits a 5+-write cross-user fan-out in ONE SQL tx today. |
| **C2** | **30 FK cascades unenforced** → deleting a user/news_item leaves orphaned bets/ledger/delivery rows that silently corrupt `SUM`-based balances and leaderboards. Referential integrity becomes ~30 hand-maintained relationships. | **CRITICAL** | Mongo enforces no FKs; `ON DELETE CASCADE`/`SET NULL` vanish. |
| **C3** | **13 CHECK enums + non-atomic status machine** → invalid states become representable; each state transition needs a manual per-callsite CAS rewrite, and any missed callsite reintroduces the double-process race. | **CRITICAL** | Status machine advanced by non-atomic filtered UPDATEs today. |
| **H1** | **Decimal128 is lateral, and the float habit makes it dangerous.** The natural Python `float`→BSON path is `double`; with 15 existing `float()` coercions producing floats, the easy path silently reintroduces binary-float drift in `realized_pnl_usd` / summed `cost_usd`. | **HIGH** | 26 `Numeric` cols, typed `Mapped[float]`. |
| **H2** | **Lost-update counters** (`UserStats.record_bet/record_settlement`, Gemini budget). Mongo `$inc`/`$max` *fixes* them — but Postgres `FOR UPDATE`/`ON CONFLICT` fixes the same bugs in an afternoon without abandoning a single guarantee. | **HIGH** | No locks/version columns today. |
| **H3** | **~880-test harness full rewrite.** `mongomock` *lies* about transactions, `$graphLookup`, `$expr`, and index/`E11000` semantics — i.e. it cannot validate the C1/idempotency paths. Real ephemeral `mongod` (replica set) needed. | **HIGH** | `conftest.py` is deeply SQLite/SQLAlchemy-specific. |
| **H4** | **Dual PyMongo + Motor wiring**, mandatory replica-set connection string, loss of the shared `Base.metadata`. More moving parts, not fewer. | **HIGH** | Two clients to wire; lazy-async-singleton + event-loop hack re-solved for Motor. |
| **M1** | Every aggregation/join rewritten (`leaderboard`, rank, `weekly_spend`, `balance`, `referral_stats`, `_propagate`, the `candidates_for` NOT-IN anti-join) → pipelines or denormalized counters; each a re-test surface. | **MED** | |
| **M2** | Permanent replica-set operational burden (if transactions are used) — step-change from "one file / one Postgres process." | **MED** | |
| **M3** | Alembic + portability machinery gone; weaker, unversioned-by-default migration story (`mongock`/`migrate-mongo`). | **MED** | |
| **M4** | `dedup_hash` TOCTOU window unchanged (cannot be made unique without rejecting legitimately distinct same-title stories). | **MED** | |

**What ports cleanly (credit where due):** `pending_intents.upsert_intent` → `updateOne({idempotency_key},…,{upsert:true})` (the cleanest map in the system); `news_channel_posts` claim → `insertOne` catching `E11000` (mirrors the current `IntegrityError` catch) *provided it is not embedded into NewsItem*; append-only `gemini_usage`/`points_ledger` → immutable collections; the crawl SAVEPOINT → free per-document `insertOne` isolation. These are genuine — and they are the low-stakes parts.

---

## 4. Target MongoDB Design

### 4.1 Table → Collection mapping (24 collections)

| # | Table | Decision | Rationale |
|---|---|---|---|
| 1 | `users` | **Own collection** (aggregate root) | Top of FK graph; hosts 1:1 embeds. |
| 2 | `accounts` | **Embedded** in `users.accounts[]` | Bounded per-user; co-accessed. Holds **ciphertext only** (§4.6). |
| 3 | `bets` | **Own**, ref `user_id`/`account_id` | Unbounded; hot query by `market_id+status` without the user. |
| 4 | `orders` | **Own**, ref | Unbounded, lifecycle-mutated. |
| 5 | `trades` | **Own**, ref | Append-only fills; unbounded. |
| 6 | `positions` | **Own**, ref | Mutable per-(account,market). |
| 7 | `markets` | **Own (reference)** | Shared dimension. |
| 8 | `categories` | **Own (reference)** | Shared taxonomy (markets+news+sources+follows); `UNIQUE slug`. |
| 9 | `news_items` | **Own** | Pipeline spine; `translations`/`cta_outcomes` embedded sub-docs. |
| 10 | `news_channel_posts` | **Own (claim ledger)** | MUST stay separate — unique-claim race needs a collection-level unique index. |
| 11 | `news_delivered` | **Own (presence ledger)** | Unique `{user_id,news_item_id}`; optionally denormalize ids onto user. |
| 12 | `pending_intents` | **Own** | Idempotency-key upsert; queried by `user_id+status`. |
| 13 | `news_sources` | **Own** | Small admin CRUD; `UNIQUE url_hash`. |
| 14 | `user_news_prefs` | **Embedded** `users.news_prefs` | Strict 1:1 (PK=user_id). |
| 15 | `user_topic_follows` | **Embedded** `users.followed_category_ids[]` | Small M2M; `$addToSet`/`$pull`. |
| 16 | `app_config` | **Own**, `_id=key` | Tiny KV; upsert via `$set`. |
| 17 | `commands` | **Own (work queue)** | Needs **atomic claim** via `findOneAndUpdate`. |
| 18 | `gemini_usage` | **Own (append-only)** | Window-summed spend. |
| 19 | `points_ledger` | **Own (append-only)** | Unbounded; balance = `$sum(delta)`. |
| 20 | `referrals` | **Own (graph edge)** | Queried both sides; `UNIQUE invitee_id`. |
| 21 | `user_stats` | **Embedded** `users.stats` | 1:1; makes leaderboard a single-collection sort. |
| 22 | `referral_code` | **Field on `users`** + unique index | Replaces the check-then-set loop (now race-free). |
| 23 | `audit_log` | **Own (append-only)** | Immutable, unbounded; never embed. |
| 24 | `_migrations` / `counters` | **Own (bookkeeping)** | Replaces Alembic version table; mints int ids. |

**Embedding rule:** 1:1-and-co-accessed → embed on `users` (`news_prefs`, `stats`, `settings`, `followed_category_ids[]`, bounded `accounts[]`). Unbounded or concurrent-unique-write → own collection (`bets/orders/trades/positions`, all ledgers, `news_channel_posts`, `news_delivered`, `referrals`). **Never embed `news_channel_posts`** — its whole purpose is a cross-process unique claim token.

### 4.2 ID strategy — keep integer PKs, do **not** switch to ObjectId

Forced by the code: FKs are `int`, and `pending_intents` bakes the integer user/item/market ids into `sha256(...)`; composite presence rows key on int pairs. Switching to ObjectId would force recomputing every idempotency key and rewriting every reference.

- `_id` = existing integer PK for single-int-PK collections.
- `_id` = `user_id` for the 1:1 tables that remain standalone.
- Composite presence rows → `_id = f"{user_id}:{news_item_id}"` (string `_id` *is* the uniqueness guarantee, cheaper than a separate unique index).
- New inserts mint ids via a `counters` collection: `findOneAndUpdate({_id:"news_items"},{$inc:{seq:1}},{returnDocument:"after",upsert:true})`; seed each counter at `MAX(id)+1` during ETL.

### 4.3 Money / Decimal → `Decimal128` (never `double`)

| Collection.field | Source | Target |
|---|---|---|
| `categories.volume` | `Numeric(20,2)` | `Decimal128` |
| `news_items.score` | `Numeric(6,4)` | `Decimal128` |
| `gemini_usage.cost_usd` | `Numeric(10,4)` | `Decimal128` |
| `users.stats.total_volume_usd` | `Numeric(20,2)` | `Decimal128` |
| `users.stats.realized_pnl_usd` | `Numeric(20,6)` | `Decimal128` |
| `users.stats.brier_sum` | `Numeric(20,6)` | `Decimal128` |
| `bets`/`orders`/`trades`/`positions` price/size/amount/pnl/payout | `Numeric(...)` | `Decimal128` |
| `app_config.value` (`gemini_weekly_budget_usd`) | Text/string | parse to `Decimal128` |

`points_ledger.delta` and all integer counters stay `Int64`. **Enforce `bsonType:"decimal"` in the JSON-Schema validator on every money field** so a stray `float` write is rejected rather than silently stored as `double`. Read via `Decimal128.to_decimal()` → Python `Decimal`; **stop the `float()` coercion**. `$sum`/`$inc` over `Decimal128` is exact; one `double` value silently promotes the whole pipeline to binary float. Quantize on write to the old scales (pnl 6dp, volume/`volume` 2dp, cost 4dp).

### 4.4 Unique indexes (the 13 UNIQUE guarantees) + JSON-Schema validators (the 13 CHECKs)

**Unique indexes — these *are* the idempotency/at-most-once guarantees:**

| Index (unique) | Collection | Guarantee |
|---|---|---|
| `{news_item_id, chat_id, lang}` | `news_channel_posts` | **At-most-once channel claim** — insert-only, catch `E11000`. |
| `{idempotency_key}` | `pending_intents` | **Bet-intent idempotency** — `updateOne(upsert)`. |
| `{user_id, news_item_id}` | `news_delivered` | Per-user at-most-once DM. |
| `{user_id, category_id}` | follows (or `$addToSet` if embedded) | M2M presence. |
| `{url_hash}` | `news_items` | Exact-URL crawl dedup — catch `E11000`. |
| `{url_hash}` | `news_sources` | Source URL uniqueness. |
| `{slug}` | `categories` | Taxonomy slug. |
| `{invitee_id}` | `referrals` | One inviter per invitee. |
| `{referral_code}` | `users` | Race-free code (replaces check-then-set loop). |
| `{user_id, reason, day_bucket}` **(partial, `reason:"streak"`)** | `points_ledger` | **NEW** — closes the streak-once-per-UTC-day race. |
| `{earner_id, source_id, layer}` | `points_ledger` | **NEW** — referral-payout idempotency (no double-pay on retry). |

Non-unique supporting indexes carry over: `news_items {status,score}`, `{category_id,status}`, `{published_at}`, **`dedup_hash` (non-unique — TOCTOU persists by design)**; `news_sources {enabled,category_id}`; `pending_intents {user_id,status}`; `commands {status,requested_at}`; `gemini_usage {ts}`; `points_ledger {user_id}`; `referrals {inviter_id}`; `users {stats.total_bets:-1}` / `{stats.total_volume_usd:-1}` for leaderboards.

**JSON-Schema validators** (`$jsonSchema`, `validationLevel:"strict"`, `validationAction:"error"`) replace the CHECK enums and are mirrored by app-level Pydantic validation:
- `news_items.status ∈ [backlog,approved,translating,rendering,ready,sent,rejected]`; `image_status ∈ [none,generating,ready,failed]`
- `news_sources.kind ∈ [auto,rss,html,telegram]`; `categories.kind ∈ [market,news,both]`
- `pending_intents.outcome ∈ [YES,NO]`; `status ∈ [pending,resumed,fulfilled,expired,cancelled]`
- `news_prefs.delivery ∈ [off,daily,realtime]`; `digest_hour` int 0–23; `news_delivered.channel ∈ [digest,realtime]`
- Previously-unenforced enums (author from scratch): `commands.status ∈ [pending,processing,done,failed]`; `referrals.status ∈ [pending,unlocked]`; `gemini_usage.kind ∈ [image,…]`; `points_ledger.reason ∈ [bet,win,streak,referral,referral_signup,referral_welcome]`
- NOT-NULL → `required:[…]` + `bsonType`; money fields → `bsonType:"decimal"`.

### 4.5 Driver / ODM choice — **Beanie (async) + PyMongo (sync)**

The sync/async split is structural: the bot + webapp are PTB 22.x asyncio; the **dashboard + worker + tests** are sync. So "Beanie everywhere" fails (Beanie is async-only).

- **Beanie on the async side** (bot/webapp): Pydantic `Document` models double as *both* the JSON-Schema validator source and the runtime app-level validation §4.4 demands — one model emits both the `$jsonSchema` and the check, and natively maps `Decimal128↔Decimal`. It wraps Motor, giving the `AsyncIOMotorClient` singleton that replaces the lazy `_async_engine` global (the NullPool/event-loop hack is re-solved by Motor's one-client-per-loop binding).
- **PyMongo on the sync side** (dashboard, keyless): keeps the dashboard synchronous and simple; it imports the same Pydantic models for shape but talks raw PyMongo.
- **One schema source, two drivers** — mirrors today's "two SQLAlchemy engines, one `Base.metadata`."

Fallback if a single driver is mandated: Motor + PyMongo over hand-written `$jsonSchema`. Beanie wins because §4.4 needs ~17 validators authored and Beanie generates them from models you need anyway.

### 4.6 The keyless-dashboard invariant under Mongo

The invariant is a *secret-boundary* property; Mongo preserves it the same way SQLite does (ciphertext at rest, key only in bot processes) and can *strengthen* it:

1. **Store ciphertext only** in `users.accounts[].encrypted_key` + non-secret metadata. No plaintext, no derived key material — identical to today.
2. **Dashboard process never imports crypto and never holds `ENCRYPTION_KEY`** (PyMongo, no key in env). Keep `core.crypto` out of the dashboard import graph. Project ciphertext out of reads (`{encrypted_private_key:0, encrypted_api_creds:0}`).
3. **RBAC hardening Mongo enables (SQLite could not):** split secrets into an `account_secrets` collection and grant the dashboard's Mongo role **no read** on it — a compromised dashboard query returns zero ciphertext.
4. **Optional CSFLE:** the bot's Motor client configured with a KMS provider + key vault auto-encrypts `encrypted_key`; the dashboard's PyMongo client is configured *without* the auto-decrypt options/master key, so it can only ever retrieve ciphertext — "dashboard never decrypts" enforced by the driver, not convention.
5. **Privileged writes stay bot-side** via the `commands` queue (dashboard *enqueues*, bot *executes* the decrypt). The atomic `findOneAndUpdate` claim keeps that boundary clean.

---

## 5. The Migration Plan

### 5.1 ETL (SQLite/Postgres → MongoDB)

One-shot **sync PyMongo** script. SQLAlchemy Core `select()` to stream rows (no ORM hydration), transform to dicts, `insert_many(ordered=False)` in ~1000-row batches. **ETL runs with `ENCRYPTION_KEY` unset** — copy ciphertext verbatim; key material never touches the migration host.

**Type transforms:** `Numeric → Decimal128(str(v))`; integer points → `int64`; `DateTime(tz) → tz-aware UTC BSON Date`; **`last_active_date String(10)` stays a string** (the streak logic compares strings — do not "improve" it); `JSON → native sub-doc/array`; ciphertext Text → string unchanged.

**Load order (FK graph, parents first):** `users` → `admins`/`categories`/`app_config` → `accounts` → embedded `user_settings`/`user_stats`/`user_news_prefs`/`bot_states` → `news_sources` → `news_items` → `positions`/`orders`/`trades` → `bets` → `points_ledger`/`referrals`/`gemini_usage`/`audit_log`/`commands` → `pending_intents` → presence rows (`user_topic_follows`/`news_delivered`/`news_channel_posts`) → **seed `counters` at MAX(id)+1** → **build indexes + validators last** (bulk-load then index is faster).

**Dry-run verifier:** assert per-collection `countDocuments == SELECT COUNT(*)`; spot-check `SUM(delta)` per user and `SUM(cost_usd)` over 7d match the SQL aggregates (catches Decimal128 bugs).

### 5.2 Per-module rewrite + effort (S ≤0.5d, M 1–2d, L 3–5d)

| Module | Effort | Key changes |
|---|---|---|
| `db/engine.py` (106) | M | Drop both engines + PRAGMA listener; `MongoClient` (sync) + lazy `AsyncIOMotorClient` (async) singletons; scopes become a `db` handle (or real Motor `start_session()`+`start_transaction()` only on §5.3 paths). |
| `db/models.py` (641) | M | Replace ORM with Pydantic/Beanie `Document` models; collection-name registry; `counters` id helper; embedding shape; Decimal128 coercion. `BigIntPK`/`MutableDict`/CHECK go away; keep str enums. |
| `db/bootstrap.py` (55) | S | `create_all()` → `ensure_indexes()` (indexes + validators, idempotent); admin seed unchanged in logic. |
| `pending_intents.py` (94) | **S** | `upsert_intent` → `update_one(upsert)` on unique key — cleanest map. `expire_stale` → atomic `update_many` (improvement). |
| `news_delivery.py` (60) | M | `candidates_for` NOT-IN → fetch delivered-id set + `$nin` (or embedded `delivered_ids`); `users_for` → single `users.find({"news_prefs.delivery":mode,status:"active"})`; `user_market_ids` → `bets.distinct(...)`. |
| `news_items.py` (116) | M | `create` → `insert_one` + counter, **catch `DuplicateKeyError`** on `url_hash`; `approve_ids` → `update_many({_id:{$in},status:"backlog"},{$set:{status:"approved"}})` (filter stays → idempotent). |
| `rewards.py` (191) | **L** | `award`→insert; `balance`→`$sum` pipeline; `_propagate`→iterative `find_one` or `$graphLookup`; `attribute_referral`→unique `invitee_id`+`E11000`; streak guard → **new unique `(user_id,reason,day_bucket)`**; `ensure_referral_code`→unique index+retry. **Main transaction candidate (§5.3).** |
| `stats.py` (127) | **L** | `record_bet`/`record_settlement` → atomic `$inc`/`$max`; conditional streak → aggregation-pipeline update (`$cond`); leaderboard → single-collection sort; rank → `count_documents({"stats.total_bets":{$gt:me}})`. **Fixes current lost-update bug.** |
| `gemini_usage.py` (43) | M | `record`→insert; `weekly_spend`→`$match+$group $sum` (Decimal128); hard cap → guarded `$inc` counter (§5.3). |
| `appconfig.py` (60) | S | `_id=key`; `set_`→`update_one(upsert)` (closes dashboard/worker race). |
| `commands.py` (30) | M | Consume → **atomic `find_one_and_update({status:"pending"},sort=[("requested_at",1)],{$set:{status:"processing"}})`** — behavioral improvement, flag for sign-off. |
| `categories.py` (74) | S | `upsert_from_tag`→`update_one(upsert)` on `slug`; `volume` Decimal128. |
| `news_prefs.py` (72) | S–M | Embedded on user → `update_one({_id:user_id},{$setOnInsert},upsert=True)`. |
| `news_sources.py` (29) | S | Unique `url_hash`; `mark_checked`→`update_one`. |
| `users.py` (73) | M | `get_or_create` by unique `telegram_id`; owns embeds; **CASCADE deletes become app code (§5.3)**. |
| `accounts.py` (189) | M | **Encryption boundary unchanged** — only persistence calls swap; `uq_account_user_label`→compound unique index. |
| `bets.py` (80) | S–M | CRUD swap; `ix_bets_status_market`→index; Decimal128 on money. |
| `orders.py` (101) | S–M | CRUD swap; indexes; Decimal128. |
| `dashboard/repo.py` (687) | **L** | Largest single rewrite: every `func.count`/`func.sum`/`case`/JOIN → aggregation pipeline; keyless invariant preserved by projecting ciphertext out. |
| `webapp/` (api 217, sync, deps, initdata, app) | M–L | Swap async session dep → Motor `db`; endpoints call rewritten repos. |
| `dashboard/routers/` (pages 414, news 198) + app/auth/deps | M | Swap `Session` dep → PyMongo `Database`; churn concentrates in `repo.py`. |
| `bot/jobs.py` settlement SAVEPOINT | **L** | Per-bet atomic CAS `bets.update_one({_id,status:"OPEN"},{$set:…})`; only on match do stats `$inc` + idempotent reward (§5.3). "Capture ids before try" hack disappears. |
| `bot/news/jobs.py` crawl + `_publish_one` + render | **L** | Crawl→per-doc `insert_one`+`E11000` (free isolation); `_publish_one`→claim `insert_one`+`E11000`, send outside session, finalize `update_one`/`delete_one`. **Keep `{news_item_id,chat_id,lang}` unique index.** |
| Alembic (`migrations/`, 5 rev) | M | Retired; replace with `ensure_indexes()` at boot + a versioned-backfill convention (`_migrations` collection / `mongock`). |

### 5.3 Transaction / replica-set strategy (minimize the ops tax)

**Operational fact, plainly:** MongoDB multi-document transactions require a **replica set** — they do **not** work on a standalone `mongod`. Adopting transactions forces a replica set in prod, dev, **and** CI (a single-node `rs.initiate()` suffices). The design goal is therefore to need transactions in **as few paths as possible**.

- **Single-document atomic — no transaction:** Gemini budget (guarded `find_one_and_update $inc` for a hard cap), `UserStats` (`$inc`/`$max`/pipeline — *fixes* the lost-update bug), `pending_intents` upsert, `app_config` upsert, `points_ledger.award` (insert-only).
- **At-most-once delivery (`_publish_one`) — no transaction:** send is deliberately outside any tx; claim and finalize are separate single-doc writes; uniqueness via the compound index. **Do not "upgrade" to a transaction** (it would force holding a session across the network send — the very thing the design forbids). Crash-safe by reconciliation. Same for the crawl SAVEPOINT (free per-doc isolation).
- **Referral fan-out (`reward_for_bet`) — the ONE real multi-doc path.** Two strategies:
  - **(A) Replica-set transaction** around the fan-out — faithful, smallest code change, but **forces the replica-set tax everywhere.**
  - **(B) Idempotency-keyed saga (no transaction, recommended)** — each award is `update_one(upsert)` on a deterministic key (the new `(earner_id,source_id,layer)` / `(user_id,reason,day_bucket)` unique indexes); the `Referral` flip is an atomic CAS `update_one({_id,status:"pending"},{$set:{status:"unlocked"}})`. Partial application self-heals on retry. Keeps a standalone `mongod` viable. This aligns with the codebase's "existence-is-the-token" philosophy.
- **Settlement batch — drop the outer commit, per-bet CAS.** The outer transaction is not load-bearing (failed bet stays OPEN, retried next tick). Atomic CAS `bets.update_one({_id,status:"OPEN"},…)`; only on match do the stats `$inc` and the bet-id-idempotent reward. At-most-once settlement without any transaction.
- **Cascade deletes — app responsibility, best-effort.** `cascade_delete_user(id)` / `cascade_delete_news_item(id)` helpers; deletes are rare/admin-driven and tolerate non-atomic cleanup (orphan sweep reconciles).

**Recommendation: strategy (B)** — keeps a standalone `mongod` viable everywhere and avoids the replica-set tax. Reserve (A) only if reviewers insist on strict all-or-nothing for the signup bonus.

### 5.4 Test strategy (replacing the 879-test harness)

`conftest.py` (SQLite/SQLAlchemy-specific: env-before-import, `drop_all`/`create_all` per test, `NullPool`+`SQLITE_WAL=0`, per-file `StaticPool` in-memory engines) is deleted wholesale.

| Option | Verdict |
|---|---|
| **`mongomock`** | **Reject as primary** — does not faithfully implement transactions, `$graphLookup`, `$expr`, full aggregation, or `E11000` index semantics; would silently pass the exact correctness-critical paths (claim race, unique-index idempotency, `$cond` streak). OK only for a few pure-CRUD unit tests. |
| **Real ephemeral `mongod` via testcontainers, single-node replica set** (**primary**) | Only option that validates §5.3. One container session-wide; function-scoped autouse `drop_database()` = direct analog of `_clean_db`; per-test Motor client bound to the loop *replaces* the NullPool hack. Needs Docker in CI; ~1–3s startup amortized. |
| **Persistent local `mongod` + per-test DB name** (fallback) | No Docker; clean `pytest-xdist` parallelism via unique DB names; good for inner-loop. CI still uses testcontainers for hermeticity. |

The ~879 test *bodies* mostly change at the seed/assert boundary (insert dicts, assert via `find_one`); the structural rewrite is in the fixtures. Integration-test the claim race and idempotency paths against the real container; keep a dual-read verifier in CI during the strangler.

### 5.5 Rollout — strangler, not big-bang

A big-bang is high-risk (money Decimal128, at-most-once ledgers, 879 tests, two driver families across every process all change at once). Strangle it; per-process boundaries make this natural — the **dashboard** (sync, read-mostly, keyless) cuts over first/safest; the **bot** (async, money + idempotency) last.

| Phase | Scope | Effort |
|---|---|---|
| **0 — Foundations** | Mongo client layer *alongside* SQLAlchemy; collection shapes; `counters`; `ensure_indexes()`+validators; single-node replica-set dev/CI containers; ETL + verifier dry-run on a prod snapshot. | M |
| **1 — Leaf/low-risk repos** | `appconfig`, `news_sources`, `categories`, `pending_intents`, `news_prefs`. Dual-read CI verification (query both stores, assert equal). | M |
| **2 — Ledgers & stats** | `gemini_usage`, `points_ledger`/`rewards` (saga §5.3-B), `stats` (atomic `$inc`/pipeline). Decimal128 + SUM→aggregation land here; verify balances/leaderboard vs SQL. | L |
| **3 — News pipeline & idempotency** | `news_items`, `news_delivery`, `_publish_one`, crawl dedup, `commands` atomic claim. Integration-test the claim race on the real container. | L |
| **4 — Accounts/bets/orders + jobs** | Encryption-boundary repos, settlement per-bet CAS, savepoint removal in `bot/jobs.py` + `bot/news/jobs.py`. | L |
| **5 — Surfaces** | `dashboard/repo.py` (687 ln of aggregations), `webapp`, routers, auth/deps; `bootstrap.py`; retire Alembic. | L |
| **6 — Cutover & decommission** | Final ETL in a brief read-only maintenance window; flip config; remove SQLAlchemy/aiosqlite/asyncpg/Alembic deps; delete old harness. | M |

**Rough total effort:** infra+engine+models+bootstrap+ETL+harness ≈ 8–10d; 17 repos ≈ 14–18d; `dashboard/repo.py`+webapp+routers ≈ 8–11d; `bot/jobs.py`+`bot/news/jobs.py` ≈ 6–8d; test bodies + race/idempotency integration + dual-read ≈ 6–9d. **Total ≈ 42–56 dev-days (~9–12 weeks solo, ~6–7 weeks for two)**, plus a Decimal128/at-most-once hardening tail. The dominant cost is not query translation — it is **re-establishing the consistency guarantees** that UNIQUE constraints + per-row isolation + a single enclosing transaction give for free today.

---

## 6. Decision Checkpoints — What I Need From You Before Starting

1. **Reconsider the target (most important).** Confirm you have weighed SQLite→Postgres (≈1 day, preserves everything) against MongoDB (≈9–12 weeks, rebuilds the guarantees by hand). If there is a hard external reason for Mongo (platform mandate, polyglot strategy), state it so I stop re-litigating and execute. **Otherwise my recommendation is Postgres.**
2. **Replica set: yes or no?** This is the pivotal ops decision. Strategy **(B) idempotency-keyed saga** keeps a standalone `mongod` viable in dev/CI/prod; choosing **(A) transactions** forces a replica set everywhere (including CI). Default proposal: (B).
3. **Sign-off on the two behavioral changes that get *stronger* guarantees than today** — the `commands` consumer gains an atomic claim/lease (no more double-processing), and settlement gains per-bet CAS at-most-once. These are improvements, not faithful ports. Confirm that is acceptable.
4. **Money precision policy.** Approve `Decimal128` end-to-end *and* removing the existing `float()` coercion (current precision debt). Confirm the per-field scales (pnl 6dp, volume 2dp, cost 4dp) and that money writes must be rejected if not `decimal`.
5. **Keyless-dashboard hardening level.** Pick: (a) parity (ciphertext-only + key absent from dashboard env, as today), (b) +RBAC (dashboard role has no read on `account_secrets`), or (c) +CSFLE (driver-enforced). Higher options need a Mongo KMS/key-vault decision.
6. **Driver/ODM approval.** Confirm **Beanie (async) + PyMongo (sync)**, or veto in favor of raw Motor+PyMongo.
7. **Rollout shape & downtime window.** Approve the strangler with dual-read verification, and confirm a brief **read-only maintenance window** for the final ETL is acceptable (the dataset is small enough).
8. **Cascade-delete tolerance.** Confirm best-effort app-side cascade cleanup + orphan sweep is acceptable (vs. wrapping deletes in transactions), since FK cascades disappear.
9. **Authoritative inputs to hand over:** a **production data snapshot** for the ETL dry-run and `SUM`/count parity checks; confirmation of the prod topology (standalone vs replica set) and CI's ability to run Docker/testcontainers; and the canonical test count for tracking (code shows **879** functions across 46 files; the brief said 893 — reconcile before we gate "all tests green").

**Relevant files:** `/Users/arashfarahani/Desktop/Work.nosync/Polymarket-BOT/db/engine.py`, `/Users/arashfarahani/Desktop/Work.nosync/Polymarket-BOT/db/models.py`, `/Users/arashfarahani/Desktop/Work.nosync/Polymarket-BOT/db/bootstrap.py`, `/Users/arashfarahani/Desktop/Work.nosync/Polymarket-BOT/db/repositories/` (17 files), `/Users/arashfarahani/Desktop/Work.nosync/Polymarket-BOT/dashboard/repo.py`, `/Users/arashfarahani/Desktop/Work.nosync/Polymarket-BOT/webapp/`, `/Users/arashfarahani/Desktop/Work.nosync/Polymarket-BOT/bot/jobs.py`, `/Users/arashfarahani/Desktop/Work.nosync/Polymarket-BOT/bot/news/jobs.py`, `/Users/arashfarahani/Desktop/Work.nosync/Polymarket-BOT/tests/conftest.py`, `/Users/arashfarahani/Desktop/Work.nosync/Polymarket-BOT/migrations/versions/` (0001–0005).
