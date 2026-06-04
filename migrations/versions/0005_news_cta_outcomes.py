"""news cta dynamic outcomes

Store the bet options for a news item as JSON (list of {label, market_id, side,
price}) so the channel post can offer an event's real choices (election
candidates, price buckets) instead of a flat Yes/No.

Revision ID: 0005_news_cta_outcomes
Revises: 0004_news_cta_question
Create Date: 2026-06-03 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0005_news_cta_outcomes'
down_revision: Union[str, Sequence[str], None] = '0004_news_cta_question'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('news_items', sa.Column('cta_outcomes', sa.JSON(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('news_items', 'cta_outcomes')
