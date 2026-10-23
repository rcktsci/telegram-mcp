"""Add is_edited to messages

Revision ID: 006
Revises: 005
Create Date: 2026-03-17

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = '006'
down_revision: Union[str, None] = '005'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('messages', sa.Column('is_edited', sa.Boolean(), nullable=False, server_default='false'))


def downgrade() -> None:
    op.drop_column('messages', 'is_edited')
