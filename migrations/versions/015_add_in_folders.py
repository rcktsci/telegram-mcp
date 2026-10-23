"""Add in_folders column to chats

Revision ID: 015
Revises: 014
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = '015'
down_revision = '014'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('chats', sa.Column('in_folders', postgresql.JSONB(), nullable=False, server_default='[]'), if_not_exists=True)


def downgrade() -> None:
    op.drop_column('chats', 'in_folders')
