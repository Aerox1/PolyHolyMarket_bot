"""points ledger streak idempotency

Add ``points_ledger.day_bucket`` ('YYYY-MM-DD', UTC) plus a PARTIAL unique index
on (user_id, day_bucket) WHERE reason='streak'. This makes the once-per-day streak
bonus idempotent at the database level, closing the check-then-insert race where
two concurrent bet placements (e.g. bot + Mini App) could each pass the "already
awarded today?" check and double-award. NULL day_bucket (every non-streak reason)
is exempt from the unique index, so the ledger stays append-only for everything
else. Portable: partial indexes work on both PostgreSQL and SQLite.

Revision ID: 0006_ledger_streak_idempotency
Revises: 0005_news_cta_outcomes
Create Date: 2026-06-04 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0006_ledger_streak_idempotency'
down_revision: Union[str, Sequence[str], None] = '0005_news_cta_outcomes'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('points_ledger', sa.Column('day_bucket', sa.String(length=10), nullable=True))
    op.create_index(
        'uq_ledger_streak_day', 'points_ledger', ['user_id', 'day_bucket'], unique=True,
        postgresql_where=sa.text("reason = 'streak'"),
        sqlite_where=sa.text("reason = 'streak'"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('uq_ledger_streak_day', table_name='points_ledger')
    op.drop_column('points_ledger', 'day_bucket')
