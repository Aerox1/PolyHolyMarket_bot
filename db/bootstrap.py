"""Database bootstrap: create the schema and the first admin.

Usage:
    python -m db.bootstrap

For local/dev (and CI) this is the quickest way to stand up the schema —
``Base.metadata.create_all`` renders the correct types on both Postgres
(BIGINT) and SQLite (INTEGER) thanks to the ``BigIntPK`` variant.

For production migrations going forward, use Alembic on the target Postgres:
    alembic revision --autogenerate -m "describe change"
    alembic upgrade head
"""

from __future__ import annotations

import logging

from sqlalchemy import select

from core.config import settings
from db.engine import create_all, session_scope
from db.models import Admin

logger = logging.getLogger(__name__)


def bootstrap_admin() -> None:
    """Create the bootstrap admin from env if the admins table is empty."""
    if not settings.admin_bootstrap_password_hash:
        logger.info("No ADMIN_BOOTSTRAP_PASSWORD_HASH set — skipping admin bootstrap.")
        return
    with session_scope() as s:
        if s.scalar(select(Admin).limit(1)) is not None:
            return
        s.add(
            Admin(
                username=settings.admin_bootstrap_user,
                password_hash=settings.admin_bootstrap_password_hash,
                is_superadmin=True,
            )
        )
        logger.info("Bootstrapped first admin: %s", settings.admin_bootstrap_user)


def main() -> None:
    logging.basicConfig(level=settings.log_level)
    create_all()
    logger.info("Schema created (create_all).")
    bootstrap_admin()
    logger.info("Bootstrap complete.")


if __name__ == "__main__":
    main()
