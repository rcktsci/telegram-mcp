"""Add attachments_downloads (media links cache index)

Revision ID: 016
Revises: 015
"""
from alembic import op
import sqlalchemy as sa


revision = '016'
down_revision = '015'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'attachments_downloads',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('download_code', sa.Text(), nullable=False),
        sa.Column('chat_id', sa.BigInteger(), nullable=False),
        sa.Column('message_id', sa.BigInteger(), nullable=False),
        sa.Column('media_kind', sa.Text(), nullable=False),
        sa.Column('file_name', sa.Text(), nullable=True),
        sa.Column('mime_type', sa.Text(), nullable=True),
        sa.Column('size_bytes', sa.BigInteger(), nullable=True),
        sa.Column('cached_file', sa.Text(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('download_code'),
    )
    op.create_index('idx_attachments_downloads_chat_message', 'attachments_downloads', ['chat_id', 'message_id'])
    op.create_index('idx_attachments_downloads_expires_at', 'attachments_downloads', ['expires_at'])


def downgrade() -> None:
    op.drop_index('idx_attachments_downloads_expires_at', table_name='attachments_downloads')
    op.drop_index('idx_attachments_downloads_chat_message', table_name='attachments_downloads')
    op.drop_table('attachments_downloads')
