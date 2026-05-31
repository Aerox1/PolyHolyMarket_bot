# Prediction-Market Web App — Growth, Referral & Gamification Strategy

> A consolidated playbook for a Tinder-style, gamified prediction-market web app (Telegram-first), adapted from the **Trojan** growth model and the current Polymarket-tooling landscape.
>
> _Source: strategy conversation with Sami Ganji (30 May 2026). Not legal advice — see §2._

---

## 0. TL;DR — The Whole Thing in One Page

**Your product:** a swipe-based, gamified prediction game for *ordinary people* — built as a Telegram web app. Not a trader terminal.

**The three pillars (copied from Trojan):**
1. **Ride a hot wave** — launch timed to a big event (election, World Cup, major crypto moment).
2. **Zero friction** — the entire experience lives inside Telegram; no app-switching.
3. **A self-propagating referral loop** — users spread it for their *own* financial/status benefit, so you barely pay for ads.

**Your unfair advantage over every competitor:** they all build *trader terminals* (charts, whale-tracking, order books). You build a **game** for the masses. That's the open gap in the market.

**The one decision that changes everything (make it first):**
> Launch as a **points/season game** (virtual points on real Polymarket trends + leaderboards), **not** real money — at least for Season 1. This zeroes legal risk, lets you test the game mechanics, and keeps the option to add a real-money layer later.

**Sustainability in one line:** give **status** rewards infinitely & free; give **value** rewards only when self-funded (from fees you actually collected, or from a fixed pre-announced prize pool); make referral rewards conditional on *real activity*; and always give points a way to be *spent* (sinks) so they don't inflate to zero.

---

## 1. Strategic Context

### 1.1 The Trojan Playbook (what actually worked)

| Lever | What Trojan did | Lesson for you |
|---|---|---|
| **Origin** | Was *Unibot* on Solana, rebranded to **Trojan** with a focus on speed | You don't need to invent everything from zero — sharpen one angle |
| **Timing** | Launched **4 Jan 2024**, right as the Solana memecoin / pump.fun wave exploded | Launch *into* an existing frenzy, don't create demand from scratch |
| **Friction** | "Trade where you talk" — full experience **inside Telegram** | Keep everything in one surface; every app-switch loses users |
| **Growth engine** | **Multi-level referral**, not paid ads. 1% fee → **0.9%** with a referral code; referrers earn a cut of their recruits' fees | A viral loop that advertises itself |
| **Scale reached** | ~**$23.4B** volume, ~**1.7M** users — biggest bot in the space | Proof the model works |
| **Channels** | Organic & community: crypto-Twitter (X), Telegram groups, KOLs — each KOL with their own referral code | Community-led, not billboards |
| **Retention** | Cashback + **Arena** rewards; referral boost pushed effective fees *negative* for regulars | Pay people to **stay** and **recruit** |
| **Trust** | Non-custodial, **CertiK** audit, **JITO** private routing (anti-sandwich) | Critical when you touch people's wallets |

### 1.2 The Market Gap — Your Wedge

Every existing Polymarket tool targets **professional traders**: terminals, charts, whale-tracking, AI, OSINT. Their language is "volume, order book, position."

**Your product is the inverse:** a frictionless, fun, swipe-style guessing game for people who find those tools cold and intimidating.

- ✅ **Strengths:** zero-friction entry (swipe, not an order form), genuine game/competition layer (none of them have it), viral referral + leaderboard loop.
- ⚠️ **Likely weakness:** serious traders will call it "shallow" — but **they aren't your audience**, so it doesn't matter.

> Strategy: don't build "yet another trader terminal." Find the niche (casual mass-market) and make it *simple*.

### 1.3 Competitive Landscape

| Product | Platform | Focus | Distinctive strength | Audience |
|---|---|---|---|---|
| **Betmoar** | Discord + web | Community trade terminal | Official Polymarket bot; most 3rd-party volume (~$110M cumulative, ~5% of Polymarket's monthly activity); news terminal with text-to-speech alerts | Discord communities |
| **PolyBot** | Telegram | Fast non-custodial trading | Per-user **Gnosis Safe** wallet, **gasless** tx, **Paste-to-Trade**; referral **25% / 20% / 12%** paid instantly in USDC | Fast mobile traders |
| **Polymtrade** | iOS / Android | Mobile terminal + AI | First dedicated mobile app; AI trained on 55k+ resolved markets; non-custodial | Mobile traders |
| **Glint** | Web | Intelligence / OSINT dashboard | "Bloomberg of Polymarket"; whale tracking >$10k positions, NEW-account flagging; 3D signal globe | Geopolitical traders |
| **➡️ Yours** | **Telegram web app** | **Swipe prediction game** | **Tinder-like UX, gamification, viral loop — for the masses** | **Mainstream / casual** |

> Note on **Hyperliquid**: it is *not* a prediction market — it's a perpetuals DEX. It's only relevant as an **analogy**: third-party "builder" apps grew to ~**40%** of Hyperliquid's volume, vs. just ~**2.5–3%** on Polymarket today. That gap implies large headroom for new builders like you.

---

## 2. The Critical Decision: Model & Geography (do this before anything else)

**Regulatory reality:** Polymarket is effectively two products — a global crypto platform (not available to US users) and a separate CFTC-regulated US structure. Real-money event contracts hit **gambling/derivatives regulation** in many places; Polymarket faces outright bans in parts of **Europe and Southeast Asia**. (You're in Frankfurt — this is directly relevant.)

**Two paths:**
1. **Real money** → you must pick allowed geographies precisely, and budget for legal/compliance.
2. **Points / in-app token game** → legal risk drops enormously; you predict on the same real trends but compete for points & leaderboard glory.

**Recommendation:** start with **Path 2 (points/seasonal game)**. It (a) zeroes legal risk, (b) lets you test the core game loop, (c) preserves the option to add a real-money layer later.

> ⚖️ **Not legal advice.** Before any real-money launch, consult a crypto/gambling lawyer for your target markets.

**Decision checklist:**
- [ ] Money model chosen: **Points** ▢ / **Real-money** ▢ / **Points→Money phased** ▢
- [ ] Target geographies shortlisted (and legality confirmed per market)
- [ ] If real-money: lawyer engaged before launch
- [ ] KYC/age-gating requirements understood for chosen path

---

## 3. The Growth Plan (Phases 0–5)

### Phase 0 — Choose the model *(see §2)*
Points vs. real money. Recommended: **points / seasonal** for Season 1.

### Phase 1 — Build the growth engine from day one
The core Trojan lesson: little direct advertising; users spread it for self-interest. Your v1 **must** ship with:
- **A unique referral link per user.**
- **Two-sided reward** — both inviter *and* invitee get something (points + a boost).
- **A leaderboard** showing both *prediction rank* and *most-invites rank*.

### Phase 2 — Build anticipation before launch
- A **waitlist** where queue position depends on **number of invites** (invite more → get access sooner).
- A **Telegram channel + group**; assemble a core of a few hundred prediction/crypto enthusiasts. These people are your launch-day ignition.

### Phase 3 — Launch on the right channels
- Same places Trojan lived: **crypto-Twitter (X)** and **Telegram groups**.
- Instead of ads, work with several **KOLs / micro-influencers**, each given a **unique referral code** so they're financially motivated to keep promoting.
- **Time the launch to a hot wave** — a major election, the World Cup, or a big crypto event everyone's watching.

### Phase 4 — Stickiness & daily return (your winning edge — Trojan had none of this)
- **Daily streaks** — predict every day, rewards grow.
- **Seasons** with end-of-season prizes.
- **Social sharing** — when a user bets or wins, generate a beautiful **share card** (the SVG posters) with a one-tap share to Story / X / group. Every share is a free ad.

### Phase 5 — Build trust
- Be explicit you're **non-custodial** (keys stay with users).
- Write **terms & risks** clearly.
- If budget allows, get a **security audit**. In prediction markets, trust is everything.

---

## 4. Referral System Design

Multi-level like Trojan, but **controlled**:

| Rule | Spec | Why |
|---|---|---|
| **Two-sided** | Both inviter & invitee get rewarded (e.g. **500 pts + a boost** each) | One-sided referrals convert poorly |
| **Multi-level but descending** | **10%** of direct recruit's activity → you; **3%** from their second layer. **Max 2 layers.** | More than 2 layers = looks pyramid-y → reputational + economic blow-up |
| **Conditional unlock (key anti-fraud)** | Reward releases **only after the invitee does something real** — e.g. **5 predictions**, or (paid model) a **minimum deposit** | Reward on signup alone = 1,000 fake accounts by morning |
| **Paid-model share** | Referral cut = a **fixed % of the fee that user generates** (e.g. platform fee 2% → **0.2%** to referrer) | Self-funding; can never run you a loss |

---

## 5. Gamification System (where you beat Trojan)

Trojan was only a trading tool. You're building a **game**.

| Mechanic | Spec | Notes |
|---|---|---|
| **XP / points from activity** | Daily prediction → XP; **correct** prediction → more XP | Brings users back daily |
| **Capped streaks** | Consecutive days raise your multiplier — **but cap it** (e.g. up to 7 days, max **2×**) | Uncapped streaks let veterans drain the economy |
| **Weekly leagues (Duolingo model)** | Bronze → Diamond. Top of each league promotes weekly; bottom relegates | Strongest daily-return engine, **zero cash cost** (reward = rank only) |
| **Multiple leaderboards** | **Accuracy** (best predictor), **Activity**, **Invites** | Every user type gets a place to shine |
| **Daily quests** | "Predict in 3 categories today," "Hit a 5-day streak" | Small missions that direct & entertain |
| **Social sharing** | Put wins on the share-card → one-tap to Story / X | Every share is a free ad |

---

## 6. Economic Sustainability — The Golden Rule

**Split every reward into two strictly separate buckets:**

- **🏅 Status rewards** — rank, badges, card skins, profile frames, titles. **Zero real cost; give infinitely.** People fight for status more than you'd expect. **~80% of your reward system should live here.**
- **💰 Value rewards** — spendable points, boosts, and (if you ever go real-money) cash share. These **cost money**, so they must be **limited & self-funding**.

**Two rules you must never break:**
1. **Cash rewards paid only from fees you actually collected** — never from principal. (Exactly like Trojan: take a 1% fee, pay part to the referrer. You only spend after you've earned.) Never pay out more than you took in.
2. **Seasonal prizes come from a fixed, pre-announced pool** (e.g. "$1,000 prize this season") — never an open-ended "more for whoever does more." This caps your cost and you know it in advance.

**Anti-inflation — build sinks:** do **not** convert points 1:1 to money, or they become a currency and rapidly go worthless. Instead give points a **way to be spent** — entry to special contests, buying boosts, buying skins. Points constantly leave circulation, so their value holds. (This is the exact mistake that blew up many token projects.)

---

## 7. Metrics to Track

| Metric | Target | If it's off… |
|---|---|---|
| **Viral coefficient** (new users per user) | **> 1** | If < 1, the referral loop isn't working → strengthen rewards |
| **D1 / D7 retention** | as high as possible | Weak → improve streaks/quests/leagues |
| **Predictions per user** | trending up | Low → improve core loop & daily quests |
| **% of users from referrals** | majority over time | Low → referral incentives too weak or too hidden |

---

## 8. Trust & Security Checklist
- [ ] Non-custodial; clearly communicate keys stay with users
- [ ] Clear, honest terms & risk disclosure
- [ ] Security audit (budget-permitting) — CertiK-style signal
- [ ] Private/safe transaction routing if handling real on-chain trades
- [ ] Transparent fee display **before** each action (Betmoar does this well)

---

## 9. Open Questions / Next Steps
- [ ] **Lock the model decision** (§2) — everything downstream depends on it.
- [ ] Draft the **referral economics** in detail (reward tiers, point values, unlock thresholds) so it's attractive *and* doesn't drain the treasury.
- [ ] Produce a **differentiation map** — the exact 3 features only your product has, to nail identity from day one.
- [ ] Design the **share-card** system (SVG poster generator + one-tap share).
- [ ] Identify the **launch wave** (which event, which date) and line up KOLs.

---

### Appendix — Quick reference: the numbers cited
- Trojan: launched **4 Jan 2024**; ~**$23.4B** volume; ~**1.7M** users; fee **1% → 0.9%** with referral.
- Betmoar: ~**$110M** cumulative volume; ~**5%** of Polymarket monthly activity; **Discord**-based.
- PolyBot referral: **25% / 20% / 12%** (3 layers), paid in USDC; gasless; Gnosis Safe per user.
- Polymtrade: AI trained on **55k+** resolved markets.
- Glint: tracks positions **>$10k**; covers both Hyperliquid (perps) & Polymarket.
- Builder-code share of volume: **~40%** on Hyperliquid vs. **~2.5–3%** on Polymarket (headroom signal).
- Suggested referral split: **10% / 3%** (max 2 layers); paid model **0.2%** of a 2% fee.
- Suggested streak cap: **≤7 days, max 2×**.
