# Technical Roadmap — Gamified Prediction-Market Telegram Mini App

> Engineering plan to build the product described in `GROWTH-STRATEGY.md`: a swipe-based, gamified prediction game inside Telegram, using **real Polymarket markets** but a **virtual-points economy** for Season 1.
>
> **Guiding principle:** Season 1 consumes **only Polymarket read APIs** — no wallets, no gas, no on-chain signing, no custody. That removes ~70% of the technical risk and ~100% of the regulatory risk, and lets us ship the game loop fast. Real money is milestone **M6**, fully deferred.

---

## 1. Scope & Non-Goals

**In scope (Season 1):**
- Telegram Mini App (TMA) + companion bot
- Pull live/trending Polymarket markets (read-only)
- Swipe-to-predict core loop on virtual **points**
- Settlement & scoring when markets resolve
- Gamification: XP, capped streaks, weekly leagues, multiple leaderboards, daily quests, seasons
- Viral: deep-link referrals (two-sided, descending, **conditional unlock**), waitlist
- Social share cards (server-rendered images)
- Bot notifications (the main retention lever)

**Explicit non-goals for Season 1 (deferred to M6):**
- Real money, USDC, on-chain orders, the CLOB order book
- User wallets / custody / gasless relayers
- KYC / on-ramps

---

## 2. System Architecture

```
                    ┌──────────────────────────────────────────┐
                    │              Telegram                      │
                    │   Bot (BotFather)  ──  Mini App (TMA)      │
                    └──────┬───────────────────────┬─────────────┘
                           │ /start ref_XXXX        │ initData (HMAC-signed)
                           │ notifications          │
                    ┌──────▼───────────────────────▼─────────────┐
                    │                API (Fastify/Nest)           │
                    │  auth · predictions · referrals · quests    │
                    │  leaderboards · share-cards · webhooks      │
                    └───┬────────────┬───────────┬───────────┬────┘
                        │            │           │           │
                 ┌──────▼───┐  ┌─────▼─────┐ ┌───▼────┐ ┌────▼──────┐
                 │ Postgres │  │  Redis    │ │ Worker │ │  Share-   │
                 │ (truth)  │  │ LB / cache│ │ (jobs) │ │  card svc │
                 │  Prisma  │  │ BullMQ    │ │ cron   │ │ satori→png│
                 └──────────┘  └───────────┘ └───┬────┘ └───────────┘
                                                 │ poll (1–5 min)
                                         ┌───────▼─────────┐
                                         │ Polymarket APIs │
                                         │ Gamma (markets) │
                                         │ Data (resolve)  │
                                         └─────────────────┘
```

**Components**
- **API** — stateless HTTP service; all game actions, validated with Zod, authorized via Telegram session JWT.
- **Worker** — background jobs (market sync, settlement, streak/league rollover, quest assignment, referral unlocks). Uses BullMQ on Redis.
- **Postgres** — source of truth. Append-only ledgers for points/XP (see §6).
- **Redis** — leaderboards (sorted sets), hot caches (current market prices), rate-limiting, BullMQ queues.
- **Share-card service** — renders dynamic PNGs; can be a route in the API or a separate function; output cached on CDN.
- **Bot** — receives `/start` deep links (referral attribution) and sends notifications.

---

## 3. Recommended Stack

| Layer | Choice | Why |
|---|---|---|
| **Monorepo** | pnpm workspaces + Turborepo | Share types between web/api in `packages/shared` |
| **Frontend (TMA)** | React + Vite + TypeScript | Fast, static-hostable |
| **Telegram SDK** | `@telegram-apps/sdk-react` (over raw `window.Telegram.WebApp`) | MainButton, haptics, theme, CloudStorage, viewport |
| **Swipe UX** | `framer-motion` drag (or `react-tinder-card`) | Tinder-style gesture + spring physics |
| **Client state/data** | Zustand + TanStack Query | Optimistic swipes, cache, retries |
| **Styling** | Tailwind + Telegram theme params | Auto light/dark to match client |
| **Backend** | Node + **Fastify** (or NestJS if you want structure) | High throughput, simple |
| **ORM/DB** | **Prisma** + **Postgres** | Type-safe, migrations |
| **Cache/RT/queue** | **Redis** + **BullMQ** | Leaderboards, jobs, rate limits |
| **Validation** | **Zod** (shared client+server) | One schema, both sides |
| **Auth** | `@telegram-apps/init-data-node` → JWT | Validates `initData` HMAC correctly (don't hand-roll) |
| **Share images** | `satori` (JSX→SVG) + `@resvg/resvg-js` (SVG→PNG) | Dynamic OG-style cards, no headless browser |
| **Analytics** | PostHog | Funnels, retention, **viral coefficient** (ties to strategy metrics) |
| **Errors/observability** | Sentry + structured logs (pino) + OTel | Trace settlement/job failures |
| **Hosting** | Web → Netlify/Vercel/Cloudflare; API+Worker → Railway/Fly/Render; Postgres → Neon/Supabase; Redis → Upstash | Stateful services need a real host; static TMA on CDN |

> The folder is `Polymarket-BOT` and Netlify is already connected — Netlify is fine for the **static TMA frontend**; put the **API + Worker** on a stateful host (Railway/Fly), not serverless-only, because of the cron/queue worker and WebSocket needs.

---

## 4. Polymarket Data Integration (the crux of Season 1)

You only need the **read** side. Three endpoints matter:

| API | Base | Use |
|---|---|---|
| **Gamma Markets** | `gamma-api.polymarket.com` | List events/markets, categories, images, end dates, current outcome prices (implied probability), liquidity/volume for "trending" |
| **CLOB (read-only)** | `clob.polymarket.com` | Optional: more precise live prices / midpoints per token; WebSocket `wss://ws-subscriptions-clob.polymarket.com` for live price ticks |
| **Data API** | `data-api.polymarket.com` | Resolution status & outcomes for settlement |

**Market sync job (every 1–5 min):**
1. Pull active markets from Gamma (filter by `active=true`, `closed=false`, sort by volume/liquidity for "trending"; pick categories that read well as a yes/no swipe).
2. Normalize → `markets` table: `polymarket_id`, `question`, `category`, `image_url`, `end_date`, `yes_token_id`, `no_token_id`, `current_yes_price`.
3. Snapshot `current_yes_price` (0–1 implied probability) — needed for fair scoring.

**Curation matters:** not every Polymarket market is a clean binary swipe. Build a **curation filter** (binary YES/NO, not-yet-resolved, end_date in a sensible window, has image, min liquidity) and optionally a manual allow/deny list for the daily card deck. This is product-critical: the deck quality *is* the game.

**Settlement job (every few min):**
1. For markets your users predicted on, check resolution (Gamma `closed/resolved` + final outcome, confirmed via Data API).
2. For each open prediction on a resolved market → compute payout/score (§7), write to the **points ledger**, mark settled, enqueue a notification.
3. Idempotency: settlement keyed on `(market_id, resolution_version)` so re-runs never double-pay.

**Probability snapshot rule (anti-abuse):** when a user swipes, the server re-reads the current price and **locks `prob_at_prediction` server-side** — never trust a client-supplied price. This prevents gaming stale odds and is the basis of the accuracy score.

---

## 5. Telegram Integration

**Two surfaces, one bot:**
- **Bot (BotFather):** owns the token, hosts the Mini App button, handles `/start` deep links, and **sends notifications**.
- **Mini App (TMA):** the React app, launched from the bot / a button / a link.

**Auth flow (do not hand-roll):**
1. TMA boots → reads `initData` from the Telegram SDK.
2. POST `initData` to `/auth/telegram`.
3. Server validates the **HMAC signature** (key derived from bot token) and checks `auth_date` freshness (reject replays) — use `@telegram-apps/init-data-node`.
4. Upsert user, issue a short-lived **session JWT** (+ refresh). Never trust a client-sent `telegram_id` again.

**Referral via deep link (canonical Telegram pattern):**
- Each user's link: `https://t.me/YourBot?startapp=ref_ABC123` (Mini App) or `?start=ref_ABC123` (bot).
- On `/start`, the bot reads the start param → records a **pending** referral edge `(inviter, invitee)`.
- The reward stays **locked** until the invitee completes real activity (e.g., 5 settled predictions over ≥2 days) — enforced server-side. This is the anti-fraud gate from the strategy doc.

**Notifications = your #1 retention lever (and ~free):** the worker sends bot messages for:
- Streak reminder ("predict today to keep your 6-day streak 🔥")
- Settlement result ("Your YES on X won — +420 pts")
- League status ("2h left, you're rank 4 in Gold — top 3 promote")
- Referral unlocked ("Sara just hit 5 predictions — claim your 500 pts")

Respect rate limits and a per-user quiet-hours / notification-preference setting.

**CloudStorage** (Telegram per-user KV) is fine for trivial client state (onboarding seen, etc.), but **the backend is the source of truth** for anything point-bearing.

---

## 6. Core Data Model (Postgres / Prisma)

```
users(id, telegram_id, username, is_premium, created_at,
      referred_by, status[active/shadowbanned], fingerprint)

markets(id, polymarket_id, question, category, image_url, end_date,
        yes_token_id, no_token_id, current_yes_price,
        status[open/resolved], resolved_outcome, resolution_version)

predictions(id, user_id, market_id, side[YES/NO],
            prob_at_prediction, stake_points,
            status[open/won/lost/void], payout_points,
            created_at, settled_at)   -- UNIQUE(user_id, market_id)

points_ledger(id, user_id, delta, reason[swipe_reward/settlement/
              referral/quest/league/season/sink_spend],
              ref_id, created_at)     -- APPEND-ONLY, balance = SUM(delta)

xp_ledger(id, user_id, delta, reason, ref_id, created_at)  -- append-only

streaks(user_id, current, longest, last_active_date, multiplier)

referrals(id, inviter_id, invitee_id, status[pending/unlocked],
          unlocked_at)               -- UNIQUE(invitee_id)

referral_earnings(id, user_id, from_user_id, layer[1/2], amount, created_at)

quests(id, code, description, target, reward_points, period[daily])
quest_progress(id, user_id, quest_id, progress, completed_at, period_key)

seasons(id, name, starts_at, ends_at, prize_pool_points, prize_pool_cash)
leagues(id, season_id, tier[bronze..diamond], week_key)
league_memberships(id, league_id, user_id, points, rank)

share_events(id, user_id, prediction_id, channel, created_at)
audit_flags(id, user_id, reason, severity, created_at)
```

**Why append-only ledgers:** balances are `SUM(delta)`, so there's no read-modify-write race, no way to "lose" or "double" points, and you get a full audit trail. This is what technically **enforces the economic rule** "never pay out more than you took in" — referral/cash deltas are written *from* fee/pool deltas, so the books always balance. Materialize a `balances` cache for fast reads; reconcile against the ledger nightly.

---

## 7. Scoring & Points Economy (technical)

Two parallel systems mapping to **Status vs. Value** from the strategy:

**A) Value economy (staking) — drives the leaderboard & sinks**
- On swipe, user stakes points; lock `prob_at_prediction`.
- Win payout = `round(stake / prob_at_prediction)` (fixed-odds at entry → longshots pay more); loss = stake forfeited.
- Daily **faucet** (free points / streak bonus) keeps non-spenders in the game; **sinks** (contest entry, boosts, skins) pull points out so the currency doesn't inflate.
- Guardrails: daily faucet cap, max stake, max daily earn — all server-enforced.

**B) Accuracy score (status) — the "best predictor" leaderboard, zero cash cost**
- Use **Brier score**: `(prob_assigned_to_actual_outcome − 1)²` averaged over settled predictions (lower = better). Rewards calibration, not just luck.
- Pure status: rank, badge, frame. Infinite to give, $0 cost.

Recording `prob_at_prediction` server-side at swipe time is what makes both systems fair and tamper-resistant.

---

## 8. The Roadmap — Milestones

> Sizing assumes a small team; treat as relative effort, not commitments. **MVP cut line is end of M2** (a playable, scoreable game). M3 makes it viral. M4 makes it sticky & shareable.

### M0 — Foundations *(~1 wk)*
- Monorepo, CI, lint/format, env management, error tracking (Sentry).
- Provision Postgres + Redis; Prisma baseline migration.
- Bot created in BotFather; TMA shell loads inside Telegram; theme + viewport wired.
- **Auth end-to-end:** `initData` validated → session JWT. Health checks.
- **Exit:** a logged-in empty TMA renders for a real Telegram user.

### M1 — Core game loop (read-only) *(~2 wks)*
- Market sync job + curation filter → daily card deck.
- Swipe UI (YES/NO/skip) with optimistic updates + haptics.
- `predictions` recorded with locked probability; stake from a starting points grant.
- Settlement job + idempotent payout to points ledger.
- Basic balance & history screens. Bot notification on settlement.
- **Exit:** a user can swipe real markets, and when one resolves they correctly win/lose points.

### M2 — Gamification (MVP cut line) *(~2–3 wks)*
- XP from activity; **capped streaks** (≤7 days, ≤2×) with daily rollover job.
- **Leaderboards** in Redis sorted sets: accuracy, activity, invites (invites stubbed until M3).
- **Weekly leagues** (Bronze→Diamond) with promote/relegate rollover job — zero cash cost.
- **Daily quests** + assignment/expiry job.
- **Seasons** scaffolding (fixed pre-announced prize pool).
- **Exit:** daily-return loop works; ranks update live; a week rolls over cleanly.

### M3 — Viral engine *(~2 wks)*
- Deep-link referral attribution via `/start ref_XXXX`.
- **Two-sided** rewards; **descending multi-level** earnings (10% L1 / 3% L2, capped 2 layers).
- **Conditional unlock** (invitee must hit real-activity threshold) — server-enforced.
- **Anti-fraud v1:** fingerprinting, rate limits, referral-graph cycle/cluster detection, shadow-ban path (act but excluded from payouts/boards).
- **Waitlist** with queue position by invites (can gate the whole launch).
- **Exit:** a referred user who completes activity unlocks rewards for both sides; fake-account farming is blocked in testing.

### M4 — Social sharing & polish *(~1.5 wks)*
- **Share-card service:** satori→resvg PNG, dynamic per win/bet, CDN-cached.
- One-tap share to Story / X / Telegram (`switchInlineQuery` / share deep link).
- Notification suite complete (streak / settlement / league / referral) + preferences & quiet hours.
- Onboarding flow, empty states, copy pass.
- **Exit:** every win is one tap from a shareable card; a share drives an attributable install.

### M5 — Scale, hardening & observability *(~1 wk, ongoing)*
- Load-test settlement + leaderboards at target concurrency.
- Cache strategy for hot market prices (Redis + short TTL; optional CLOB WebSocket for live ticks).
- PostHog dashboards for the **strategy metrics**: viral coefficient (>1), D1/D7, predictions/user, % from referral.
- Backups, runbooks, alerting on job failures & settlement lag.
- Security review (rate limits, authz on every endpoint, ledger reconciliation).

### M6 — Real-money layer *(DEFERRED — scoped, not built)*
Only after legal sign-off (§2 of strategy). Adds, roughly:
- **Wallets:** per-user proxy/Safe or external wallet via WalletConnect; **non-custodial** (keys with user).
- **Gasless:** relayer (Gelato/Biconomy) or Polymarket relayer; sponsor Polygon gas.
- **Funding:** USDC on Polygon; on-ramp (Moonpay/Transak) / bridge.
- **Order routing:** `@polymarket/clob-client`, EIP-712 signed orders → CLOB; manage L2 API creds.
- **Fees → referral funding:** platform fee on trades funds payouts (keeps the "self-funding" rule literally true).
- **Compliance:** geofencing, KYC if required, terms, **security audit (CertiK-style)**.
- This is a large jump in complexity + risk; the points game must prove the loop first.

---

## 9. Cross-Cutting Concerns

**Leaderboards at scale** — Redis sorted sets, one key per board/period (`lb:accuracy:s3`, `lb:invites:w2026-22`). `ZINCRBY` on events, `ZREVRANGE` for top-N, `ZREVRANK` for "your rank." Snapshot to Postgres on rollover for history. O(log n), scales to millions.

**Jobs/cron (BullMQ)** — market sync (1–5 min), settlement (few min), streak rollover (daily UTC cutoff), league rollover (weekly), quest assignment (daily), referral-unlock checks, notification fan-out. All **idempotent** and safe to retry.

**Anti-fraud** — initData validation (no fake users) · device fingerprint (multi-account detection) · conditional referral unlock · per-action rate limits · daily earn caps · referral-graph analysis (cycles, shared fingerprint/IP clusters) · shadow-ban (excluded from payouts/boards, doesn't know it). Tie suspicious accounts to `audit_flags`.

**Security** — authz check on every endpoint (a user can only act as themselves) · server-side price locking · ledger as the only way to move points (no ad-hoc balance writes) · secrets in a vault, never in the TMA bundle · validate all webhook/job inputs.

**Observability** — Sentry for errors; structured logs; alert on settlement lag, job-queue depth, and ledger-vs-balance drift (a drift alarm catches economy bugs early).

---

## 10. Key Risks & Mitigations

| Risk | Mitigation |
|---|---|
| Polymarket API shape/limits change | Thin adapter layer over Gamma/Data; cache; alert on sync failures |
| Bad/ambiguous markets ruin the deck | Curation filter + manual allow/deny; binary-only; min liquidity |
| Settlement double-pay / economy bug | Idempotent settlement keyed on resolution_version; append-only ledger; nightly reconciliation + drift alarm |
| Referral farming | Conditional unlock + fingerprint + graph analysis + shadow-ban |
| Notification spam → blocks/mutes | Frequency caps, preferences, quiet hours |
| Scope creep into real money too early | M6 is gated on legal sign-off and a proven loop |

---

## 11. Immediate Next 2 Weeks (concrete)
1. **M0**: scaffold monorepo, stand up Postgres/Redis, create the bot, get the TMA loading with validated `initData` → JWT.
2. **Spike the Polymarket adapter**: hit Gamma, list trending binary markets, prove you can read current prices and detect a resolution end-to-end.
3. **Vertical slice of M1**: one screen, swipe one real market, record a prediction with a locked probability, manually trigger settlement, watch points land in the ledger.
4. Stand up **PostHog** early so you measure the funnel from day one.

> Build order rationale: prove the **data integration** (riskiest external dependency) and the **append-only points loop** (riskiest internal invariant) first; everything else is additive on top of those two.

---

### Appendix — Stack at a glance
`React + Vite + @telegram-apps/sdk-react + framer-motion + Tailwind`  ·  `Fastify + Prisma + Postgres + Redis + BullMQ + Zod`  ·  `satori + resvg-js` for cards  ·  `PostHog + Sentry`  ·  Polymarket **Gamma/Data** (read-only) for Season 1; **CLOB client** only at M6.
