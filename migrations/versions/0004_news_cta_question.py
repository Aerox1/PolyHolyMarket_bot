"""news cta market question

Cache the resolved market's question on the news item so the channel post (and
the digest) can show the actual wager next to the Bet YES/NO CTA — the buttons
alone ("Bet YES?") were ambiguous.

Revision ID: 0004_news_cta_question
Revises: 0003_pending_intents
Create Date: 2026-06-03 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0004_news_cta_question'
down_revision: Union[str, Sequence[str], None] = '0003_pending_intents'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('news_items', sa.Column('cta_market_question', sa.String(length=300), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('news_items', 'cta_market_question')
