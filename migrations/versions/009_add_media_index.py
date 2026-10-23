"""Add media index to messages

Revision ID: 009
Revises: 008
"""
from alembic import op
import sqlalchemy as sa


revision = '009'
down_revision = '008'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute('CREATE INDEX idx_messages_media ON messages USING gin ((raw_json->\'media\'));')


def downgrade() -> None:
    op.execute('DROP INDEX idx_messages_media;')
