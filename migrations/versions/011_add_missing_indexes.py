"""Add missing indexes for search

Revision ID: 011
Revises: 010
"""
from alembic import op
import sqlalchemy as sa


revision = '011'
down_revision = '010'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index('idx_chats_watching', 'chats', ['watching'])
    op.create_index('idx_messages_chat_topic', 'messages', ['chat_id', 'topic_id'])
    op.drop_index('idx_messages_topic_id', 'messages')


def downgrade() -> None:
    op.create_index('idx_messages_topic_id', 'messages', ['topic_id'])
    op.drop_index('idx_messages_chat_topic', 'messages')
    op.drop_index('idx_chats_watching', 'chats')
