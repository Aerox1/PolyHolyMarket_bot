"""One-off ETL: copy all data from the live SQLite DB into PostgreSQL.

Run AFTER ``alembic upgrade head`` has created the schema on Postgres and AFTER the
bot/dashboard are stopped (so SQLite is frozen — no lost writes). Both engines bind
the SAME ``db.models.Base.metadata``, so column types adapt values on both ends
(JSON ↔ dict, Numeric ↔ Decimal, Boolean ↔ bool, DateTime ↔ datetime).

Safety / correctness:
* Integer PKs are copied VERBATIM (foreign keys + the pending_intents sha256 key
  depend on them) — we do NOT renumber.
* Naive datetimes from SQLite (stored UTC) get UTC tzinfo re-attached so Postgres
  TIMESTAMPTZ doesn't shift them by the session timezone.
* Encrypted-key ciphertext (Text) is copied as-is — the migration host never needs
  ENCRYPTION_KEY.
* Sequences are reset to MAX(id) so new inserts don't collide.
* Idempotent: destination tables are cleared (children-first) before load, so a
  re-run reproduces the same result.

Usage:
    ETL_DST='postgresql+psycopg://pmbot:pmbot@localhost:5432/pmbot' \
        .venv/bin/python -m scripts.migrate_sqlite_to_pg
"""

from __future__ import annotations

import datetime as dt
import os

from sqlalchemy import create_engine, insert, select, text

from db.models import Base

SRC = os.environ.get("ETL_SRC", "sqlite:///./pmbot.db")
DST = os.environ.get("ETL_DST", "postgresql+psycopg://pmbot:pmbot@localhost:5432/pmbot")


def _utc(v):
    """SQLite returns naive datetimes (stored UTC); make them tz-aware for TIMESTAMPTZ."""
    if isinstance(v, dt.datetime) and v.tzinfo is None:
        return v.replace(tzinfo=dt.timezone.utc)
    return v


def main() -> None:
    src = create_engine(SRC)
    dst = create_engine(DST, future=True)
    tables = list(Base.metadata.sorted_tables)  # FK parents first

    counts: dict[str, int] = {}
    with src.connect() as s, dst.begin() as d:
        # clear destination children-first so a re-run starts clean (no FK violation)
        for t in reversed(tables):
            d.execute(t.delete())
        for t in tables:
            rows = [dict(r._mapping) for r in s.execute(select(t))]
            if rows:
                rows = [{k: _utc(v) for k, v in row.items()} for row in rows]
                d.execute(insert(t), rows)
            counts[t.name] = len(rows)

    # reset sequences to MAX(id) so the next insert doesn't collide with copied ids
    with dst.begin() as d:
        for t in tables:
            if "id" not in t.c:  # composite-PK / keyed tables have no serial 'id'
                continue
            seq = d.execute(text("SELECT pg_get_serial_sequence(:tn, 'id')"), {"tn": t.name}).scalar()
            if seq:
                d.execute(text(
                    f"SELECT setval('{seq}', "
                    f"GREATEST((SELECT COALESCE(MAX(id), 0) FROM {t.name}), 1), "
                    f"(SELECT COUNT(*) FROM {t.name}) > 0)"
                ))

    for name, c in sorted(counts.items()):
        print(f"{name:24} {c}")
    print(f"{'TOTAL':24} {sum(counts.values())}  across {len(counts)} tables")


if __name__ == "__main__":
    main()
