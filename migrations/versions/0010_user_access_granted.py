"""user access_granted (invite-code gate)

Adds users.access_granted for the new-user access-code gate. Existing users are
grandfathered (backfilled to true) so the gate never locks out current users; new
rows default false (gated until they enter a valid access/referral code).

Revision ID: 0010_user_access_granted
Revises: 0009_drop_news_poll_votes
Create Date: 2026-06-07 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic. (Keep id <= 32 chars — alembic_version is varchar(32).)
revision: str = '0010_user_access_granted'
down_revision: Union[str, Sequence[str], None] = '0009_drop_news_poll_votes'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('users', sa.Column('access_granted', sa.Boolean(), nullable=False,
                                     server_default=sa.false()))
    # grandfather every existing user so nobody currently using the bot is locked out
    op.execute("UPDATE users SET access_granted = true")


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('users', 'access_granted')
