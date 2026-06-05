"""news poll votes

Inline engagement-poll votes on channel news cards. Each row is one sentiment vote
(the real bet stays on the card's deep-link buttons). Composite PK
(news_item_id, tg_user_id) gives one-vote-per-Telegram-account-per-item; a re-tap
updates the chosen outcome. Replaces the old separate native-poll message — the
poll now lives as callback buttons on the card itself.

Revision ID: 0008_news_poll_votes
Revises: 0007_user_stats_lb_indexes
Create Date: 2026-06-05 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0008_news_poll_votes'
down_revision: Union[str, Sequence[str], None] = '0007_user_stats_lb_indexes'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('news_poll_votes',
    sa.Column('news_item_id', sa.BigInteger(), nullable=False),
    sa.Column('tg_user_id', sa.BigInteger(), nullable=False),
    sa.Column('outcome_index', sa.SmallInteger(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.ForeignKeyConstraint(['news_item_id'], ['news_items.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('news_item_id', 'tg_user_id')
    )
    with op.batch_alter_table('news_poll_votes', schema=None) as batch_op:
        batch_op.create_index('ix_news_poll_votes_item', ['news_item_id'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('news_poll_votes', schema=None) as batch_op:
        batch_op.drop_index('ix_news_poll_votes_item')
    op.drop_table('news_poll_votes')
