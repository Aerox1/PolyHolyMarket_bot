# ── Stage 1: build the Mini App frontend (React + Vite) ──────────────────────
FROM node:20-slim AS frontend
WORKDIR /fe
COPY webapp/frontend/package*.json ./
RUN npm install
COPY webapp/frontend/ ./
RUN npm run build

# ── Stage 2: Python app (bot / dashboard / webapp share this image) ──────────
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential libpq-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

COPY . .
# Built SPA from stage 1 (so the webapp service can serve it at /).
COPY --from=frontend /fe/dist ./webapp/frontend/dist

# Default command is the bot; compose overrides per service.
CMD ["python", "-m", "bot.main"]
