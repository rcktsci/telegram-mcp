"""Add user_id to chats, drop session_hash

Revision ID: 005
Revises: 004
Create Date: 2026-03-16

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = '005'
down_revision: Union[str, None] = '004'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_index('ix_chats_session_hash', 'chats')
    op.drop_column('chats', 'session_hash')
    op.add_column('chats', sa.Column('user_id', sa.BigInteger(), nullable=True))
    op.create_index('ix_chats_user_id', 'chats', ['user_id'])


def downgrade() -> None:
    op.drop_index('ix_chats_user_id', 'chats')
    op.drop_column('chats', 'user_id')
    op.add_column('chats', sa.Column('session_hash', sa.String(length=12), nullable=True))
    op.create_index('ix_chats_session_hash', 'chats', ['session_hash'])
