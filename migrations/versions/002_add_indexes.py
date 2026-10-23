"""Add indexes for search optimization

Revision ID: 002
Revises: 001
Create Date: 2024-01-02

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = '002'
down_revision: Union[str, None] = '001'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    from sqlalchemy import text
    op.execute(text("DROP INDEX IF EXISTS idx_messages_chat_date"))
    op.execute(text("DROP INDEX IF EXISTS idx_messages_sender"))
    op.execute(text("DROP INDEX IF EXISTS idx_messages_chat_id"))
    
    op.create_index('idx_messages_chat_id_date', 'messages', ['chat_id', 'date'])
    op.create_index('idx_messages_sender_id', 'messages', ['sender_id'])
    op.create_index('idx_messages_chat_id_message_id', 'messages', ['chat_id', 'message_id'])
    op.create_index(
        'idx_messages_not_deleted',
        'messages',
        ['chat_id', 'message_id'],
        postgresql_where=sa.text('NOT is_deleted')
    )


def downgrade() -> None:
    op.drop_index('idx_messages_not_deleted', 'messages')
    op.drop_index('idx_messages_chat_id_message_id', 'messages')
    op.drop_index('idx_messages_sender_id', 'messages')
    op.drop_index('idx_messages_chat_id_date', 'messages')
