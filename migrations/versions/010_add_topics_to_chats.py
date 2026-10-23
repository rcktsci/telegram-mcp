"""Add topics column to chats

Revision ID: 010
Revises: 009
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = '010'
down_revision = '009'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('chats', sa.Column('topics', postgresql.JSONB(), nullable=True), if_not_exists=True)


def downgrade() -> None:
    op.drop_column('chats', 'topics')
