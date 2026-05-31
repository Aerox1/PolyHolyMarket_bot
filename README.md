# Polymarket Trading Bot (Telegram) — Multi-User

A multi-user, multi-language Telegram bot for trading and managing **real Polymarket positions**, with an authenticated admin dashboard. Built in Python, reusing the trading core from the original single-user `Polygen` bot.

- **Connect account** → link your existing Polymarket wallet (private key encrypted at rest; server signs orders).
- **Create account** → guided link to Polymarket's own signup (done externally), then connect.
- **Trade & manage positions** → buy/sell/market/close/cancel with inline confirmation.
- **Multi-language** → English, فارسی (RTL), Русский, 中文.
- **Admin dashboard** → manage users, view accounts/positions, suspend/ban, broadcast — **never exposes keys**.

> ⚠️ **Security & legal.** This bot stores users' wallet private keys *encrypted at rest* and signs orders server-side (custodial-encrypted, like Trojan/PolyBot). The encryption key (`ENCRYPTION_KEY`) lives only in the bot/worker processes — **never** the dashboard. Real-money trading carries legal/regulatory weight depending on jurisdiction; consult a lawyer before a public launch.

---

## Architecture

```
Telegram ──► bot (python-telegram-bot, async)  ──┐
                                                  ├──► PostgreSQL  ◄── dashboard (FastAPI, NO encryption key)
Polymarket APIs ◄── per-user ClobClient (signs) ─┘
```

- **bot/** — Telegram bot: onboarding (connect), monitoring, trading. Holds `ENCRYPTION_KEY`.
- **dashboard/** — FastAPI admin UI. Reads DB + public Data API. **No `ENCRYPTION_KEY`** → cannot decrypt keys.
- **polymarket/** — per-user Polymarket client + `AccountManager` (decrypt → build `ClobClient` → cache).
- **core/** — config, `crypto` (Fernet encryption + argon2 admin hashing), `i18n`, `audit`, `logging`.
- **db/** — SQLAlchemy 2 models (shared spine) + engines + repositories.
- **locales/** — `en/fa/ru/zh.json` (en is the source of truth; others fall back to en).

### Credential levels (why design is two-tier)
Polymarket is a DEX — orders are EIP-712 signed:
- **Positions / portfolio / market data** → public, need only the **wallet address**.
- **Balance / open orders / place / cancel** → need the **decrypted private key** (signer) + API creds. *API keys alone cannot trade.*

So monitoring uses a read-only client (no decryption); trading uses a signing client (decrypts the key in memory only).

---

## Quick start (local, SQLite — no Docker)

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env

# Generate secrets:
python -c "from cryptography.fernet import Fernet; print('ENCRYPTION_KEY=' + Fernet.generate_key().decode())"
python -c "import secrets; print('SESSION_SECRET=' + secrets.token_urlsafe(32))"
python -m core.crypto hash-password 'your-admin-password'   # -> ADMIN_BOOTSTRAP_PASSWORD_HASH

# Edit .env: set TELEGRAM_BOT_TOKEN, ENCRYPTION_KEY, SESSION_SECRET,
#            ADMIN_BOOTSTRAP_PASSWORD_HASH, and DATABASE_URL=sqlite:///./dev.db

python -m db.bootstrap        # create schema + first admin
python -m bot.main            # run the bot  (Phase 1+)
uvicorn dashboard.app:app --port 8877   # run the dashboard  (Phase 3+)
```

## Production (Docker + Postgres)

```bash
cp .env.example .env          # fill secrets; use the postgres DATABASE_URL
docker compose up -d --build  # postgres + migrate + bot + dashboard
```

The `migrate` service runs `alembic upgrade head` (falling back to `db.bootstrap`).
To generate the first Alembic migration against your Postgres:

```bash
alembic revision --autogenerate -m "initial schema"
alembic upgrade head
```

---

## Tests

```bash
python -m pytest -q
```

Foundation tests cover encryption round-trip/rotation, i18n fallback + RTL + no-orphan-keys, and DB model constraints/cascades.

---

## Security checklist

- [x] Private keys encrypted at rest (Fernet); decrypted only in memory at signing time.
- [x] Dashboard process runs **without** `ENCRYPTION_KEY` (cannot decrypt keys).
- [x] Log filter redacts hex keys / Fernet tokens (defence-in-depth).
- [x] The Telegram message containing a pasted key is auto-deleted on receipt.
- [ ] **Rotate the bot token** if it was ever shared in plaintext (`@BotFather → /revoke`).
- [x] Audit log records key access, orders, and admin actions (never secrets).

---

## Bot commands

| Command | What it does |
|---|---|
| `/start` | Welcome + language picker + main menu (Connect / Create) |
| `/connect` | Connect a wallet (type → address → key); key message auto-deleted, encrypted |
| `/disconnect` | Remove a connected wallet (deletes the stored key) |
| `/language` | Change language (EN / FA / RU / ZH) |
| `/portfolio` `/positions` `/balance` `/orders` `/trades` `/activity` | Account monitoring |
| `/search` `/market` `/price` `/book` | Market data |
| `/buy` `/sell` | Limit order: `<token> <price> <size>` (inline confirm) |
| `/marketbuy <token> <usd>` `/marketsell <token> <shares>` | Market orders (inline confirm) |
| `/manage` | Positions with **[Sell 50%] [Close]** buttons |
| `/close <token>` · `/cancel <id>` · `/cancelall` | Close position / cancel order(s) |

## Status — all phases complete ✅

- **Phase 0 — Foundations:** config, Fernet crypto + argon2, i18n (4 langs, RTL), DB schema, Alembic, Docker.
- **Phase 1 — Connect + monitoring:** per-user encrypted-key client, AccountManager, connect flow, monitoring.
- **Phase 2 — Trading + position management:** buy/sell/market/close/cancel with inline confirm + audit + order logging.
- **Phase 3 — Admin dashboard:** FastAPI, auth, users/metrics/broadcast/audit, dark theme + RTL, never exposes keys.
- **Phase 4 — Hardening:** full FA/RU/ZH translations, broadcast delivery worker, per-user rate limit, dashboard CSRF, prod cookie flag.

**37 tests passing** (crypto, i18n, models, AccountManager, repo round-trip, trading logic, dashboard e2e + no-key-leak).
