"""Add topic_id and topic_name to messages

Revision ID: 008
Revises: 007
"""
from alembic import op
import sqlalchemy as sa


revision = '008'
down_revision = '007'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('messages', sa.Column('topic_id', sa.BigInteger(), nullable=True))
    op.add_column('messages', sa.Column('topic_name', sa.String(255), nullable=True))
    op.create_index('idx_messages_topic_id', 'messages', ['topic_id'])


def downgrade() -> None:
    op.drop_index('idx_messages_topic_id', table_name='messages')
    op.drop_column('messages', 'topic_name')
    op.drop_column('messages', 'topic_id')
