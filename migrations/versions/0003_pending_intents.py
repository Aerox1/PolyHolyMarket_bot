"""pending_intents

Deferred bet intents: store the (market, outcome) a non-connected user wanted to
bet from a news-channel CTA so it can resume after onboarding.

Revision ID: 0003_pending_intents
Revises: 0002_news_pipeline
Create Date: 2026-06-03 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0003_pending_intents'
down_revision: Union[str, Sequence[str], None] = '0002_news_pipeline'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('pending_intents',
    sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), autoincrement=True, nullable=False),
    sa.Column('user_id', sa.BigInteger(), nullable=False),
    sa.Column('news_item_id', sa.BigInteger(), nullable=True),
    sa.Column('market_id', sa.String(length=128), nullable=False),
    sa.Column('outcome', sa.String(length=8), nullable=False),
    sa.Column('question', sa.Text(), nullable=True),
    sa.Column('source', sa.String(length=16), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('idempotency_key', sa.String(length=64), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=True),
    sa.CheckConstraint("outcome in ('YES','NO')", name='ck_pending_intent_outcome'),
    sa.CheckConstraint("status in ('pending','resumed','fulfilled','expired','cancelled')", name='ck_pending_intent_status'),
    sa.ForeignKeyConstraint(['news_item_id'], ['news_items.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('idempotency_key')
    )
    with op.batch_alter_table('pending_intents', schema=None) as batch_op:
        batch_op.create_index('ix_pending_intents_user_status', ['user_id', 'status'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('pending_intents', schema=None) as batch_op:
        batch_op.drop_index('ix_pending_intents_user_status')
    op.drop_table('pending_intents')
