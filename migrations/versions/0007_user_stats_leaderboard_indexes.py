"""user_stats leaderboard indexes

Index the two leaderboard metrics that were missing one — ``realized_pnl_usd``
(the "pnl" board) and ``wins`` (the "wins" board) — so all four leaderboard
queries (METRICS in db.repositories.stats) are index-backed and don't fall back
to a full-table sort as the user base grows. bets/volume were already indexed.

Revision ID: 0007_user_stats_leaderboard_indexes
Revises: 0006_ledger_streak_idempotency
Create Date: 2026-06-04 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = '0007_user_stats_leaderboard_indexes'
down_revision: Union[str, Sequence[str], None] = '0006_ledger_streak_idempotency'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_index('ix_user_stats_pnl', 'user_stats', ['realized_pnl_usd'])
    op.create_index('ix_user_stats_wins', 'user_stats', ['wins'])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_user_stats_wins', table_name='user_stats')
    op.drop_index('ix_user_stats_pnl', table_name='user_stats')
