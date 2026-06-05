"""drop news poll votes

The inline engagement-poll feature was removed entirely, so the news_poll_votes
table is dropped. (Downgrade recreates it exactly as 0008 did.)

Revision ID: 0009_drop_news_poll_votes
Revises: 0008_news_poll_votes
Create Date: 2026-06-05 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0009_drop_news_poll_votes'
down_revision: Union[str, Sequence[str], None] = '0008_news_poll_votes'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('news_poll_votes', schema=None) as batch_op:
        batch_op.drop_index('ix_news_poll_votes_item')
    op.drop_table('news_poll_votes')


def downgrade() -> None:
    """Downgrade schema."""
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
