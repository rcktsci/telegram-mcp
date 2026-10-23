from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import BigInteger, Boolean, DateTime, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Chat(Base):
    __tablename__ = "chats"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False)
    title: Mapped[Optional[str]] = mapped_column(String(255))
    is_private: Mapped[bool] = mapped_column(Boolean, default=False)
    is_group: Mapped[bool] = mapped_column(Boolean, default=False)
    watching: Mapped[bool] = mapped_column(Boolean, default=False)
    user_id: Mapped[Optional[int]] = mapped_column(BigInteger, default=None, index=True)
    min_message_id: Mapped[Optional[int]] = mapped_column(BigInteger, default=None)
    max_message_id: Mapped[Optional[int]] = mapped_column(BigInteger, default=None)
    min_message_date: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), default=None)
    max_message_date: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), default=None)
    last_webhook_message_id: Mapped[Optional[int]] = mapped_column(BigInteger, default=None)
    # Resume checkpoint of the bottom-up history sweep: every id <= this value
    # is either stored or absent on the server. Written only by the sweep.
    history_sweep_upto: Mapped[Optional[int]] = mapped_column(BigInteger, default=None)
    # Telegram folder_id (DialogFilter) membership; refreshed hourly by the
    # sweep via src/telegram/folders.py — token scope resolves against it.
    in_folders: Mapped[list] = mapped_column(JSONB, default=list)
    topics: Mapped[Optional[dict]] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))


class Message(Base):
    __tablename__ = "messages"
    __table_args__ = (
        UniqueConstraint("chat_id", "message_id", name="uidx_messages_chat_id_message_id"),
        Index("idx_messages_chat_id_date", "chat_id", "date"),
        Index("idx_messages_topic_id", "topic_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    message_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sender_id: Mapped[Optional[int]] = mapped_column(BigInteger)
    sender_username: Mapped[Optional[str]] = mapped_column(String(255))
    sender_name: Mapped[Optional[str]] = mapped_column(String(255))
    text: Mapped[Optional[str]] = mapped_column(Text)
    date: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    raw_json: Mapped[Optional[dict]] = mapped_column(JSONB)
    is_deleted: Mapped[bool] = mapped_column(Boolean, default=False)
    is_edited: Mapped[bool] = mapped_column(Boolean, default=False)
    topic_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    topic_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))


class AuthToken(Base):
    __tablename__ = "auth_tokens"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # UUID4, plaintext by owner decision (spec: hash adds nothing here)
    token: Mapped[str] = mapped_column(String(36), unique=True)
    comment: Mapped[str] = mapped_column(Text)
    # Telegram folder_id (DialogFilter) / chat_id allow-lists; both empty = no access
    folder_ids: Mapped[list] = mapped_column(JSONB, default=list)
    chat_ids: Mapped[list] = mapped_column(JSONB, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class AttachmentDownload(Base):
    __tablename__ = "attachments_downloads"
    __table_args__ = (
        Index("idx_attachments_downloads_chat_message", "chat_id", "message_id"),
        Index("idx_attachments_downloads_expires_at", "expires_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    download_code: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    # BigInteger, not int4: marked channel ids (-100…) overflow int4 —
    # same as messages.chat_id, which stores these very values.
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    message_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    media_kind: Mapped[str] = mapped_column(Text, nullable=False)
    file_name: Mapped[Optional[str]] = mapped_column(Text)
    mime_type: Mapped[Optional[str]] = mapped_column(Text)
    size_bytes: Mapped[Optional[int]] = mapped_column(BigInteger)
    cached_file: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
