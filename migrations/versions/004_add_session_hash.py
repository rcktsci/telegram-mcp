"""Add session_hash to chats

Revision ID: 004
Revises: 003
Create Date: 2026-03-16

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = '004'
down_revision: Union[str, None] = '003'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('chats', sa.Column('session_hash', sa.String(length=12), nullable=True))
    op.create_index('ix_chats_session_hash', 'chats', ['session_hash'])


def downgrade() -> None:
    op.drop_index('ix_chats_session_hash', 'chats')
    op.drop_column('chats', 'session_hash')
