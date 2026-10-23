"""Initial migration

Revision ID: 001
Revises: 
Create Date: 2024-01-01

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = '001'
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'chats',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('chat_id', sa.BigInteger(), nullable=False),
        sa.Column('title', sa.String(length=255), nullable=True),
        sa.Column('is_private', sa.Boolean(), nullable=False, server_default='false'),
        sa.Column('is_group', sa.Boolean(), nullable=False, server_default='false'),
        sa.Column('watching', sa.Boolean(), nullable=False, server_default='false'),
        sa.Column('min_message_id', sa.BigInteger(), nullable=True),
        sa.Column('min_message_date', sa.DateTime(timezone=True), nullable=True),
        sa.Column('max_message_id', sa.BigInteger(), nullable=True),
        sa.Column('max_message_date', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('chat_id')
    )

    op.create_table(
        'messages',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('chat_id', sa.BigInteger(), nullable=False),
        sa.Column('message_id', sa.BigInteger(), nullable=False),
        sa.Column('sender_id', sa.BigInteger(), nullable=True),
        sa.Column('sender_username', sa.String(length=255), nullable=True),
        sa.Column('sender_name', sa.String(length=255), nullable=True),
        sa.Column('text', sa.Text(), nullable=True),
        sa.Column('date', sa.DateTime(timezone=True), nullable=True),
        sa.Column('raw_json', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('is_deleted', sa.Boolean(), nullable=False, server_default='false'),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('chat_id', 'message_id')
    )

    op.create_index('idx_messages_chat_date', 'messages', ['chat_id', 'date'])
    op.create_index('idx_messages_chat_id', 'messages', ['chat_id'])
    op.create_index('idx_messages_sender', 'messages', ['sender_id'])


def downgrade() -> None:
    op.drop_index('idx_messages_sender', 'messages')
    op.drop_index('idx_messages_chat_id', 'messages')
    op.drop_index('idx_messages_chat_date', 'messages')
    op.drop_table('messages')
    op.drop_table('chats')
