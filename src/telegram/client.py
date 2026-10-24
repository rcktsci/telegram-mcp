import asyncio
import logging
import mimetypes
import os
from datetime import datetime
from typing import Optional, AsyncGenerator
from dataclasses import dataclass
from uuid import uuid4

from telethon import TelegramClient
from telethon.network import ConnectionTcpMTProxyAbridged

from src.db import get_session_factory, ChatRepository, MessageRepository
from telethon.sessions import StringSession
from telethon.errors import SessionPasswordNeededError, PhoneCodeInvalidError
from telethon.events import NewMessage, MessageEdited, MessageDeleted
from telethon import types, functions, utils

logger = logging.getLogger(__name__)


def _convert_datetime(obj):
    """Recursively convert datetime objects and bytes to JSON-serializable format."""
    import base64
    if isinstance(obj, datetime):
        return obj.isoformat()
    elif isinstance(obj, bytes):
        return base64.b64encode(obj).decode('utf-8')
    elif isinstance(obj, dict):
        return {k: _convert_datetime(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [_convert_datetime(item) for item in obj]
    return obj


def marked_chat_id(peer) -> int:
    """Normalize a Telethon peer/entity to the MARKED ID form.

    Telethon exposes marked IDs in most places (Dialog.id, event.chat_id:
    -100… for channels, negative for basic groups, raw for users), while
    entity objects carry bare IDs. Mixing bare IDs (event.get_chat().id)
    with marked ones (chats.chat_id from Dialog.id) silently breaks
    lookups for channels and groups: the realtime path would store
    messages under an id no chats row uses.
    """
    return utils.get_peer_id(peer, add_mark=True)


# Deterministic cache dir for get_media_link. NO volume mount (owner's call):
# recreating the container resets the cache, DB rows then 404 (no 500s).
MEDIA_CACHE_DIR = "/app/files"

# Specific kinds first: video_note/voice/audio/sticker/animation are all
# documents with extra attributes, and a video_note also matches .video.
_MEDIA_KINDS = (
    ("voice", "voice"),
    ("video_note", "video_note"),
    ("audio", "audio"),
    ("sticker", "sticker"),
    ("animation", "gif"),
    ("video", "video"),
    ("photo", "photo"),
    ("document", "document"),
)

_KIND_EXT = {
    "photo": ".jpg",
    "voice": ".ogg",
    "video": ".mp4",
    "video_note": ".mp4",
    "animation": ".mp4",
    "sticker": ".webp",
}


def cleanup_media_tmp_files(cache_dir: Optional[str] = None) -> int:
    """Startup cleanup: remove *.tmp left by interrupted downloads.

    cache_dir resolves at call time, so tests can monkeypatch the constant.
    Only the tmp suffix matches — cached files never end with .tmp. Any
    OSError (missing dir/file, unreadable directory, permissions) is
    swallowed: cleanup returns 0 and must not take the application start down.
    """
    removed = 0
    directory = cache_dir or MEDIA_CACHE_DIR
    try:
        for name in os.listdir(directory):
            if name.endswith(".tmp"):
                os.remove(os.path.join(directory, name))
                removed += 1
    except OSError:
        pass
    return removed


def _media_of(message) -> tuple[Optional[str], object]:
    """(kind, media object) of a Telethon message; (None, None) without media."""
    for kind, prop in _MEDIA_KINDS:
        media = getattr(message, prop, None)
        if media:  # truthiness: props return None or the media object
            return kind, media
    return None, None


def media_summary(message) -> Optional[dict]:
    """Compact attachment summary stored in raw_json["media"].

    One shape for all three write paths: the realtime handler (whose raw used
    to lack "media" entirely, breaking has_media=true), the history pages
    (which stored the whole telethon to_dict()["media"] blob) and the
    MessageEdited handler (same to_dict() blob — without this its stored row
    would lose kind, and an equal edit_date never re-refreshes it via the
    sweep). Drives has_media filtering and tells the agent the attachment
    type.
    """
    kind, _ = _media_of(message)
    if kind is None:
        return None
    f = getattr(message, "file", None)
    return {
        "kind": kind,
        "mime_type": getattr(f, "mime_type", None),
        "size": getattr(f, "size", None),
        "file_name": getattr(f, "name", None),
        "duration": getattr(f, "duration", None),
    }


# Read-side priority, derived so "specific beats generic" stays defined in
# _MEDIA_KINDS only.
_KIND_ORDER = tuple(kind for kind, _ in _MEDIA_KINDS)


def _document_kind(document: dict) -> Optional[str]:
    """Kind of a legacy document blob by its attributes; None without markers."""
    markers = set()
    for attr in document.get("attributes") or []:
        name = attr.get("_")
        if name == "DocumentAttributeAudio":
            markers.add("voice" if attr.get("voice") else "audio")
        elif name == "DocumentAttributeVideo":
            markers.add("video_note" if attr.get("round_message") else "video")
        elif name == "DocumentAttributeSticker":
            markers.add("sticker")
        elif name == "DocumentAttributeAnimated":
            markers.add("animation")
    return next((k for k in _KIND_ORDER if k in markers), None)


def media_kind_of(media) -> Optional[str]:
    """Kind of an already-stored raw_json["media"] value.

    Reads both shapes found in the DB: the compact summary written by
    media_summary ({"kind": ...}) and the legacy full telethon to_dict()
    blob saved before the summary existed (no "kind", a "_" ctor instead).
    Empty document/photo shells (DocumentEmpty, PhotoEmpty) count as no
    media, mirroring the Message.document / Message.photo properties.
    Unknown or missing shapes yield None — no guessing.
    """
    if not isinstance(media, dict):
        return None
    if media.get("kind"):
        return media["kind"]
    ctor = media.get("_")
    if ctor == "MessageMediaPhoto":
        photo = media.get("photo") or {}
        return "photo" if photo.get("_") == "Photo" else None
    if ctor == "MessageMediaWebPage":
        # Link previews can carry an attachment: a document (video, gif,
        # sticker, audio...) inside the webpage — same markers as usual.
        webpage = media.get("webpage") or {}
        document = webpage.get("document") or {}
        has_document = document.get("_") == "Document"
        kind = _document_kind(document) if has_document else None
        if kind:
            return kind
        if (webpage.get("photo") or {}).get("_") == "Photo":
            return "photo"
        return "document" if has_document else None
    if ctor == "MessageMediaDocument":
        document = media.get("document") or {}
        if document.get("_") != "Document":
            return None
        return _document_kind(document) or "document"
    return None


@dataclass
class TelegramMessage:
    chat_id: int
    message_id: int
    sender_id: Optional[int]
    sender_username: Optional[str]
    sender_name: Optional[str]
    text: Optional[str]
    date: datetime
    raw: dict
    topic_id: Optional[int] = None
    topic_name: Optional[str] = None


def message_to_row(msg: TelegramMessage) -> dict:
    """Shape a TelegramMessage the way MessageRepository.create_batch expects."""
    return {
        "chat_id": msg.chat_id,
        "message_id": msg.message_id,
        "sender_id": msg.sender_id,
        "sender_username": msg.sender_username,
        "sender_name": msg.sender_name,
        "text": msg.text,
        "date": msg.date,
        "raw_json": msg.raw,
        "topic_id": msg.topic_id,
        "topic_name": msg.topic_name,
    }


class TelegramListener:
    def __init__(
        self,
        session_string: str,
        api_id: int,
        api_hash: str,
        mtproxy_host: Optional[str] = None,
        mtproxy_port: Optional[int] = None,
        mtproxy_secret: str = "",
    ):
        self.session_string = session_string
        self.api_id = api_id
        self.api_hash = api_hash
        self.mtproxy = None
        if not (mtproxy_host and mtproxy_secret):
            # MCP-путь (_build_listeners) прокси явно не передаёт: без этого
            # фолбэка на VM за MTProxy он уходит в Telegram напрямую и падает
            # (воркер и статус-чекер передают прокси сами). Явные аргументы
            # главнее настроек; настроек нет — подключаемся напрямую.
            try:
                from src.config import get_settings

                telegram = get_settings().telegram
                mtproxy_host = mtproxy_host or telegram.mtproxy_host
                mtproxy_port = mtproxy_port or telegram.mtproxy_port
                mtproxy_secret = mtproxy_secret or telegram.mtproxy_secret
            except Exception:
                logger.warning("Failed to read MTProxy settings; connecting directly", exc_info=True)
        if mtproxy_host and mtproxy_secret:
            self.mtproxy = (mtproxy_host, mtproxy_port or 443, mtproxy_secret)
            logger.info(f"MTProxy configured: {self.mtproxy[0]}:{self.mtproxy[1]}")
        else:
            logger.info("MTProxy not configured, connecting directly")
        self._client: Optional[TelegramClient] = None
        self._running = False
        self._queue: asyncio.Queue = asyncio.Queue()

    async def _handle_topic_created(self, chat_id: int, topic_id: int, topic_name: str) -> None:
        """Persist a newly created forum topic.

        Topic events must not go through the message queue: consumers expect
        TelegramMessage there, and a dict crashes the worker loop. Update the
        in-memory cache and the DB row directly instead.
        """
        if getattr(self, "_topic_names", None) is None:
            self._topic_names = {}
        self._topic_names[topic_id] = topic_name

        try:
            session_factory = get_session_factory()
            async with session_factory() as session:
                repo = ChatRepository(session)
                await repo.add_topic(chat_id, topic_id, topic_name)
        except Exception as e:
            logger.warning(f"Failed to persist topic {topic_id} for chat {chat_id}: {e}")
            return
        logger.info(f"Topic created: id={topic_id}, name={topic_name} (chat_id={chat_id})")

    async def start(self):
        session = StringSession(self.session_string)
        client_kwargs = dict(
            session=session,
            api_id=self.api_id,
            api_hash=self.api_hash,
            device_model="telegram-mcp",
            system_version="1.0",
            app_version="1.1.0",
            proxy=self.mtproxy,
        )
        if self.mtproxy:
            client_kwargs["connection"] = ConnectionTcpMTProxyAbridged
        self._client = TelegramClient(**client_kwargs)
        
        self._queue = asyncio.Queue()
        self._ready = asyncio.Event()
        self._history_loaded = asyncio.Event()
        self._running = True
        self._topic_names: dict[int, str] = {}
        
        @self._client.on(NewMessage(incoming=True))
        async def handler(event):
            if not self._running:
                return
            
            message = event.message
            chat = await event.get_chat()
            sender = await message.get_sender()
            
            sender_name = None
            sender_username = None
            sender_id = None
            if sender:
                sender_id = getattr(sender, 'id', None)
                sender_username = getattr(sender, 'username', None)
                if hasattr(sender, 'first_name'):
                    parts = [sender.first_name, sender.last_name]
                    sender_name = " ".join(p for p in parts if p)
                elif hasattr(sender, 'title'):
                    sender_name = sender.title
            
            topic_id = getattr(message, 'topic_id', None)
            if message.reply_to and getattr(chat, 'forum', False):
                topic_id = message.reply_to.reply_to_top_id or message.reply_to.reply_to_msg_id or topic_id
            
            topic_name = self._topic_names.get(topic_id) if topic_id else None

            msg = TelegramMessage(
                chat_id=event.chat_id,
                message_id=message.id,
                sender_id=sender_id,
                sender_username=sender_username,
                sender_name=sender_name,
                text=message.text,
                date=message.date,
                raw={
                    "chat_id": event.chat_id,
                    "message_id": message.id,
                    "sender_id": sender_id,
                    "sender_username": sender_username,
                    "sender_name": sender_name,
                    "text": message.text,
                    "date": message.date.isoformat() if message.date else None,
                    "topic_id": topic_id,
                    "topic_name": topic_name,
                    "media": media_summary(message),
                },
                topic_id=topic_id,
                topic_name=topic_name,
            )
            await self._queue.put(msg)
        
        @self._client.on(NewMessage(incoming=True))
        async def service_handler(event):
            if not self._running:
                return
            if hasattr(event.message, 'action') and event.message.action:
                if isinstance(event.message.action, types.MessageActionForumTopicCreate):
                    await self._handle_topic_created(
                        event.chat_id,
                        event.message.id,
                        event.message.action.title,
                    )
        
        @self._client.on(MessageEdited(incoming=True))
        async def edit_handler(event):
            if not self._running:
                return
            
            message = event.message
            
            session_factory = get_session_factory()
            async with session_factory() as session:
                from sqlalchemy import select, and_
                from src.db.models import Chat
                result = await session.execute(
                    select(Chat).where(
                        Chat.chat_id == event.chat_id,
                        Chat.watching == True,
                    )
                )
                watch_chat = result.scalar_one_or_none()
                if not watch_chat:
                    return
                
                repo = MessageRepository(session)
                raw = _convert_datetime(message.to_dict())
                raw["media"] = media_summary(message)
                updated = await repo.update_text(event.chat_id, message.id, message.text, raw)
                if updated:
                    logger.info(f"Message edited: chat={event.chat_id}, msg_id={message.id}")
        
        @self._client.on(MessageDeleted())
        async def delete_handler(event):
            if not self._running:
                return
            
            for msg_id in event.deleted_ids:
                session_factory = get_session_factory()
                async with session_factory() as session:
                    from sqlalchemy import select
                    from src.db.models import Chat
                    
                    result = await session.execute(
                        select(Chat).where(
                            Chat.chat_id == event.chat_id,
                            Chat.watching == True,
                        )
                    )
                    watch_chat = result.scalar_one_or_none()
                    if not watch_chat:
                        continue
                    
                    repo = MessageRepository(session)
                    marked = await repo.mark_deleted(event.chat_id, msg_id)
                    if marked:
                        logger.info(f"Message deleted: chat={event.chat_id}, msg_id={msg_id}")
        
        async def run_telegram_client():
            try:
                await self._client.start()
                me = await self._client.get_me()
                logger.info(f"Telegram client started for user {me.username or me.id} (user_id: {me.id})")
                
                self._ready.set()
                
                logger.info("Fetching dialogs...")
                dialogs = []
                try:
                    dialogs = await self._client.get_dialogs()
                    logger.info(f"Found {len(dialogs)} dialogs")
                    
                    from src.db import get_session_factory, ChatRepository
                    session_factory = get_session_factory()
                    async with session_factory() as session:
                        chat_repo = ChatRepository(session)
                        logger.info("Upserting dialogs...")
                        await chat_repo.upsert_chats([{
                            "chat_id": d.id,
                            "title": d.name,
                            "is_group": hasattr(d.entity, 'megagroup') and d.entity.megagroup,
                            "is_private": d.entity is not None and isinstance(d.entity, (types.User, types.PeerUser))
                        } for d in dialogs], user_id=me.id)
                        logger.info(f"Synced {len(dialogs)} dialogs to database")
                except Exception as e:
                    logger.error(f"Failed to sync dialogs: {e}")
                    import traceback
                    logger.error(traceback.format_exc())
                
                try:
                    from src.db import get_session_factory, ChatRepository, MessageRepository
                    session_factory = get_session_factory()
                    
                    async with session_factory() as session:
                        chat_repo = ChatRepository(session)
                        logger.info("About to call get_watching_chats")
                        watching_chats = await chat_repo.get_watching_chats(me.id)
                        logger.info(f"Found {len(watching_chats)} watching chats to load history")
                    
                    for chat in watching_chats:
                        try:
                            async with session_factory() as session:
                                chat_repo = ChatRepository(session)
                                msg_repo = MessageRepository(session)
                                bounds = await msg_repo.get_message_id_bounds(chat.chat_id)
                            _, max_existing_id = bounds

                            if max_existing_id:
                                logger.info(f"Loading NEW messages for chat {chat.chat_id} (last sync had up to msg_id={max_existing_id})")
                            else:
                                logger.info(f"Loading FULL history for watching chat {chat.chat_id} ({chat.title})...")

                            saved = 0

                            async def save_batch(batch, _chat_id=chat.chat_id):
                                nonlocal saved
                                saved += await self.save_history_batch(_chat_id, batch)

                            loaded = await self._load_history_with_pagination(
                                chat.chat_id,
                                min_message_id=(max_existing_id + 1) if max_existing_id else None,
                                on_batch=save_batch,
                            )
                            logger.info(f"Loaded {loaded} messages from chat {chat.chat_id} (saved {saved} new)")

                            async with session_factory() as session:
                                chat_repo = ChatRepository(session)
                                msg_repo = MessageRepository(session)

                                current_id_bounds = await msg_repo.get_message_id_bounds(chat.chat_id)
                                curr_min_id, curr_max_id = current_id_bounds
                                current_date_bounds = await msg_repo.get_message_date_bounds(chat.chat_id)
                                curr_min_date, curr_max_date = current_date_bounds
                                if curr_min_id is not None and curr_max_id is not None:
                                    await chat_repo.update_message_bounds(chat.chat_id, curr_min_id, curr_max_id, curr_min_date, curr_max_date)
                                    logger.info(f"Updated bounds for chat {chat.chat_id}: min_id={curr_min_id}, max_id={curr_max_id}, min_date={curr_min_date}, max_date={curr_max_date}")
                                
                                all_topic_ids = await msg_repo.get_topic_ids_for_chat(chat.chat_id)
                                topic_names = await self.get_forum_topics(chat.chat_id)
                                if topic_names or all_topic_ids:
                                    topics_map = {}
                                    if topic_names:
                                        for tid, name in topic_names.items():
                                            topics_map[str(tid)] = name
                                    if all_topic_ids:
                                        for tid in sorted(all_topic_ids):
                                            if not tid or str(tid) in topics_map:
                                                continue
                                            name = topic_names.get(tid) if topic_names else None
                                            if not name:
                                                name = await self.get_topic_name_from_message(chat.chat_id, tid)
                                            topics_map[str(tid)] = name
                                    await chat_repo.update_topics(chat.chat_id, topics_map)
                                    
                                    from sqlalchemy import text
                                    await session.execute(text("""
                                        UPDATE messages m
                                        SET topic_name = c.topics->>m.topic_id::text
                                        FROM chats c
                                        WHERE m.chat_id = c.chat_id 
                                        AND m.chat_id = :chat_id
                                        AND m.topic_id IS NOT NULL 
                                        AND m.topic_name IS NULL
                                        AND c.topics IS NOT NULL
                                    """), {"chat_id": chat.chat_id})
                                    await session.commit()
                                    
                                    logger.info(f"Updated topics for chat {chat.chat_id}: {len(topics_map)} topics")
                        except Exception as e:
                            logger.error(f"Failed to load history for chat {chat.chat_id}: {e}")
                            import traceback
                            logger.error(traceback.format_exc())
                            continue
                except Exception as e:
                    logger.error(f"Failed to load history: {e}")
                    import traceback
                    logger.error(traceback.format_exc())
                
                # Topic metadata sync is slow (an RPC per forum dialog plus one
                # UPDATE per dialog), so it runs AFTER history catch-up: new
                # and previously-watched chats must not wait for it.
                if dialogs:
                    from src.db import get_session_factory, ChatRepository
                    session_factory = get_session_factory()
                    async with session_factory() as session:
                        chat_repo = ChatRepository(session)
                        for d in dialogs:
                            if hasattr(d.entity, 'forum') and d.entity.forum:
                                try:
                                    topics = await self.get_forum_topics(d.id)
                                    if topics:
                                        topics_map = {str(tid): name for tid, name in topics.items()}
                                        await chat_repo.update_topics(d.id, topics_map)
                                        logger.info(f"Updated {len(topics_map)} forum topics for chat {d.id}")
                                except Exception as e2:
                                    logger.warning(f"Failed to fetch topics for chat {d.id}: {e2}")
                            else:
                                await chat_repo.update_topics(d.id, {})
                
                self._history_loaded.set()
                await self._client.disconnected
            except Exception as e:
                logger.error(f"Telegram client error: {e}")
        
        asyncio.create_task(run_telegram_client())
    
    async def _load_history_with_pagination(
        self,
        chat_id: int,
        batch_size: int = 1000,
        min_message_id: int = None,
        on_batch=None,
    ) -> int:
        """Load history for a chat using server-side pagination.

        Every page is handed to ``on_batch`` immediately, so a crash keeps all
        pages already fetched instead of throwing the whole run away.

        Args:
            chat_id: Chat ID
            batch_size: Batch size for loading
            min_message_id: If specified, only load messages with ID > this (for incremental sync).
                           If None, load ALL history (for full sync).
            on_batch: Optional async callable invoked with each fetched page.
        Returns:
            Total number of messages fetched.
        """
        total = 0
        offset_id = min_message_id

        while True:
            if offset_id is not None:
                batch = await self.get_history(chat_id, limit=batch_size, min_id=offset_id)
            else:
                batch = await self.get_history(chat_id, limit=batch_size)

            if not batch:
                break

            total += len(batch)
            if on_batch is not None:
                await on_batch(batch)
            logger.info(f"Loaded batch of {len(batch)} messages (total: {total})")

            offset_id = batch[-1].message_id
            await asyncio.sleep(0.5)

        return total

    async def save_history_batch(self, chat_id: int, messages: list[TelegramMessage]) -> int:
        """Upsert one history page and widen the chat bounds right away.

        A re-walked page is a no-op unless an incoming copy carries a
        strictly newer edit_date, which refreshes the stored row (counted
        as saved by the edit-freshness policy in MessageRepository).
        """
        rows = [message_to_row(msg) for msg in messages]
        if not rows:
            return 0

        dates = [row["date"] for row in rows if row["date"] is not None]
        session_factory = get_session_factory()
        async with session_factory() as session:
            saved = await MessageRepository(session).create_batch(rows, session=session)
            await ChatRepository(session).update_message_bounds(
                chat_id,
                min(row["message_id"] for row in rows),
                max(row["message_id"] for row in rows),
                min(dates) if dates else None,
                max(dates) if dates else None,
            )
        return saved

    async def sweep_history(self, chat_id: int) -> None:
        """Run one resumable bottom-up sweep for a chat.

        Returns normally when the sweep reached the top (clean empty page).
        Page errors propagate, keeping the checkpoint where it was.
        """
        from src.telegram.sweep import sweep_chat

        await sweep_chat(
            chat_id,
            fetch_page=lambda offset: self.get_history_page(chat_id, offset_id=offset),
            save_batch=self.save_history_batch,
        )

    async def load_history_for_chat(self, chat_id: int, user_id: int, full_history: bool = False):
        """Load history for a specific chat when watching is enabled.
        
        Args:
            chat_id: Telegram chat ID
            user_id: User ID to verify ownership
            full_history: If True, load all history. If False, only new messages since last sync.
        """
        if not self._client or not self._running:
            logger.warning(f"Cannot load history: client not connected")
            return
        
        try:
            session_factory = get_session_factory()
            async with session_factory() as session:
                chat_repo = ChatRepository(session)
                msg_repo = MessageRepository(session)
                
                from sqlalchemy import select
                from src.db.models import Chat
                result = await session.execute(
                    select(Chat).where(Chat.chat_id == chat_id, Chat.watching == True)
                )
                chat = result.scalar_one_or_none()
                
                if not chat:
                    logger.warning(f"Chat {chat_id} not found or not watching")
                    return
                
                bounds = await msg_repo.get_message_id_bounds(chat.chat_id)
                _, max_existing_id = bounds

                if full_history or not max_existing_id:
                    logger.info(f"Loading FULL history for chat {chat.chat_id} ({chat.title})...")
                else:
                    logger.info(f"Loading NEW messages for chat {chat.chat_id} (last sync had up to msg_id={max_existing_id})")

                saved = 0

                async def save_batch(batch):
                    nonlocal saved
                    saved += await self.save_history_batch(chat.chat_id, batch)

                loaded = await self._load_history_with_pagination(
                    chat.chat_id,
                    min_message_id=None if (full_history or not max_existing_id) else max_existing_id + 1,
                    on_batch=save_batch,
                )
                logger.info(f"Loaded {loaded} messages from chat {chat.chat_id} (saved {saved} new)")

                current_id_bounds = await msg_repo.get_message_id_bounds(chat.chat_id)
                curr_min_id, curr_max_id = current_id_bounds
                current_date_bounds = await msg_repo.get_message_date_bounds(chat.chat_id)
                curr_min_date, curr_max_date = current_date_bounds
                if curr_min_id is not None and curr_max_id is not None:
                    await chat_repo.update_message_bounds(chat.chat_id, curr_min_id, curr_max_id, curr_min_date, curr_max_date)
                    logger.info(f"Updated bounds for chat {chat.chat_id}: min_id={curr_min_id}, max_id={curr_max_id}, min_date={curr_min_date}, max_date={curr_max_date}")
                
                all_topic_ids = await msg_repo.get_topic_ids_for_chat(chat.chat_id)
                topic_names = await self.get_forum_topics(chat.chat_id)
                if topic_names or all_topic_ids:
                    topics_map = {}
                    if topic_names:
                        for tid, name in topic_names.items():
                            topics_map[str(tid)] = name
                    if all_topic_ids:
                        for tid in sorted(all_topic_ids):
                            if not tid or str(tid) in topics_map:
                                continue
                            name = topic_names.get(tid) if topic_names else None
                            if not name:
                                name = await self.get_topic_name_from_message(chat.chat_id, tid)
                            topics_map[str(tid)] = name
                    await chat_repo.update_topics(chat.chat_id, topics_map)
                    
                    from sqlalchemy import text
                    await session.execute(text("""
                        UPDATE messages m
                        SET topic_name = c.topics->>m.topic_id::text
                        FROM chats c
                        WHERE m.chat_id = c.chat_id 
                        AND m.chat_id = :chat_id
                        AND m.topic_id IS NOT NULL 
                        AND m.topic_name IS NULL
                        AND c.topics IS NOT NULL
                    """), {"chat_id": chat.chat_id})
                    await session.commit()
                    
                    logger.info(f"Updated topics for chat {chat.chat_id}: {len(topics_map)} topics")
                    
        except Exception as e:
            logger.error(f"Failed to load history for chat {chat_id}: {e}")
            import traceback
            logger.error(traceback.format_exc())
    
    async def reconnect(self):
        """Reconnect the listener."""
        saved_topics = getattr(self, "_topic_names", {})
        old_client = self._client
        if old_client is not None:
            try:
                await old_client.disconnect()
            except Exception as e:
                logger.warning(f"Error disconnecting old client during reconnect: {e}")
        self._client = None
        if getattr(self, "_ready", None) is not None:
            self._ready.clear()
        if getattr(self, "_history_loaded", None) is not None:
            self._history_loaded.clear()
        self._queue = asyncio.Queue()
        await self.start()
        self._topic_names = saved_topics
        ready = await self.wait_ready()
        if not ready:
            raise RuntimeError("Failed to reconnect: wait_ready() timed out")

    async def wait_ready(self, timeout: float = 120.0):
        """Wait for the client to be ready (connected and authenticated)."""
        try:
            await asyncio.wait_for(self._ready.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            logger.error(f"Timeout waiting for Telegram client to be ready")
            return False
    
    async def wait_history_loaded(self, timeout: float = 300.0):
        """Wait for initial history to be loaded."""
        try:
            await asyncio.wait_for(self._history_loaded.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            logger.warning(f"Timeout waiting for history to load (this is OK if there are many messages)")
            return True
        except Exception as e:
            logger.error(f"Error waiting for history: {e}")
            return False

    async def stop(self):
        self._running = False
        if self._client:
            await self._client.disconnect()
            logger.info("Telegram client stopped")

    async def get_chat_members(self, chat_id: int) -> list[dict]:
        if not self._client:
            return []
        
        try:
            await self.wait_ready(timeout=30)
            entity = await self._client.get_entity(chat_id)
            participants = await self._client.get_participants(entity)
            
            members = []
            for p in participants:
                members.append({
                    "user_id": p.id,
                    "username": p.username or "",
                    "name": f"{p.first_name or ''} {p.last_name or ''}".strip()
                })
            return members
        except Exception as e:
            logger.error(f"Failed to get chat members: {e}")
            return []

    async def get_forum_topics(self, chat_id: int) -> dict[int, str]:
        if not self._client:
            return {}
        
        try:
            entity = await self._client.get_entity(chat_id)
            has_forum = hasattr(entity, 'forum') and entity.forum
            logger.info(f"get_forum_topics: chat_id={chat_id}, has_forum={has_forum}")
            if not has_forum:
                return {}
            try:
                result = await self._client(functions.messages.GetForumTopicsRequest(
                    peer=entity,
                    offset_date=None,
                    offset_id=0,
                    offset_topic=0,
                    limit=100
                ))
                topics_map = {}
                for topic in result.topics:
                    topics_map[topic.id] = topic.title
                logger.info(f"Got {len(topics_map)} forum topics for chat {chat_id}")
                return topics_map
            except AttributeError:
                return {}
        except Exception as e:
            logger.warning(f"Failed to get forum topics for {chat_id}: {e}")
            return {}

    async def get_topic_name_from_message(self, chat_id: int, topic_id: int) -> Optional[str]:
        if not self._client:
            return None
        
        try:
            entity = await self._client.get_entity(chat_id)
            msg = await self._client.get_messages(entity, ids=topic_id)
            if msg:
                if isinstance(msg, list):
                    msg = msg[0] if msg else None
                if msg:
                    if hasattr(msg, 'action') and hasattr(msg.action, 'title') and msg.action.title:
                        return msg.action.title
                    if hasattr(msg, 'text') and msg.text:
                        return msg.text.split('\n')[0][:255]
        except Exception as e:
            logger.debug(f"Failed to get topic name for {topic_id}: {e}")
        return None

    async def _build_message(self, chat_id: int, entity, message) -> TelegramMessage:
        """Convert one Telethon message into a TelegramMessage."""
        raw_dict = {}
        try:
            raw_dict = message.to_dict()
            raw_dict = _convert_datetime(raw_dict)
        except Exception as e:
            logger.warning(f"Failed to get raw message: {e}")
            raw_dict = {
                "chat_id": chat_id,
                "message_id": message.id,
                "text": message.text,
            }

        sender = await message.get_sender()

        sender_name = None
        sender_username = None
        sender_id = None
        if sender:
            sender_id = getattr(sender, 'id', None)
            sender_username = getattr(sender, 'username', None)
            if hasattr(sender, 'first_name'):
                parts = [sender.first_name, sender.last_name]
                sender_name = " ".join(p for p in parts if p)
            elif hasattr(sender, 'title'):
                sender_name = sender.title

        topic_id = getattr(message, 'topic_id', None)
        reply_to = None
        is_forum = getattr(entity, 'forum', False)
        if is_forum and isinstance(raw_dict, dict):
            reply_to = raw_dict.get("reply_to")
            if reply_to and isinstance(reply_to, dict):
                topic_id = reply_to.get("reply_to_top_id") or reply_to.get("reply_to_msg_id")
            elif reply_to and hasattr(reply_to, 'reply_to_top_id'):
                topic_id = reply_to.reply_to_top_id or reply_to.reply_to_msg_id

        # Compact summary replaces the full telethon media blob from to_dict().
        raw_dict["media"] = media_summary(message)

        return TelegramMessage(
            chat_id=chat_id,
            message_id=message.id,
            sender_id=sender_id,
            sender_username=sender_username,
            sender_name=sender_name,
            text=message.text,
            date=message.date,
            raw=raw_dict,
            topic_id=topic_id
        )

    async def get_history(self, chat_id: int, limit: int = 10000, min_id: int = None) -> list[TelegramMessage]:
        """Получить историю сообщений чата. По умолчанию загружает до 10000 последних сообщений.
        
        Args:
            chat_id: ID чата
            limit: Максимальное количество сообщений
            min_id: Если указан, загружает все сообщения начиная с этого ID (для полной истории)
        """
        if not self._client:
            return []
        
        messages = []
        try:
            entity = await self._client.get_entity(chat_id)
            
            kwargs = {"limit": limit, "reverse": True}
            if min_id is not None:
                kwargs["offset_id"] = min_id
            
            async for message in self._client.iter_messages(entity, **kwargs):
                if message:
                    messages.append(await self._build_message(chat_id, entity, message))
        except Exception as e:
            logger.error(f"Failed to get history for chat {chat_id}: {e}")
        return messages

    async def get_history_page(self, chat_id: int, offset_id: Optional[int] = None, limit: int = 1000) -> list[TelegramMessage]:
        """Fetch one history page oldest→newest, exclusive of ``offset_id``.

        Unlike get_history this does NOT swallow exceptions: an empty result is
        then a genuine "nothing above" terminal for the sweep, not a hidden RPC
        failure. Raises if the client is not connected.
        """
        if not self._client:
            raise RuntimeError("Telegram client is not connected")

        entity = await self._client.get_entity(chat_id)
        kwargs = {"limit": limit, "reverse": True}
        if offset_id is not None:
            kwargs["offset_id"] = offset_id

        page = []
        async for message in self._client.iter_messages(entity, **kwargs):
            if message:
                page.append(await self._build_message(chat_id, entity, message))
        return page

    async def get_dialogs_list(self) -> list[dict]:
        if not self._client:
            return []
        
        try:
            dialogs = await self._client.get_dialogs()
            result = []
            for d in dialogs:
                entity = d.entity
                is_group = False
                is_private = False
                
                if entity:
                    if isinstance(entity, (types.User, types.PeerUser)):
                        is_private = True
                    elif isinstance(entity, (types.Channel, types.Chat, types.PeerChannel, types.PeerChat)):
                        if hasattr(entity, 'megagroup') and entity.megagroup:
                            is_group = True
                        elif hasattr(entity, 'broadcast'):
                            is_group = not entity.broadcast
                        elif hasattr(entity, 'participants_count') and entity.participants_count:
                            is_group = True
                
                result.append({
                    "chat_id": d.id,
                    "title": d.name,
                    "is_group": is_group,
                    "is_private": is_private
                })
            return result
        except Exception as e:
            logger.error(f"Failed to get dialogs: {e}")
            return []

    async def get_folder_overview(self) -> list[dict]:
        """Fetch user-defined Telegram FOLDERS (dialog filters) and their members.

        Source of truth mirrors exactly what the Telegram app shows:
        account.GetDialogFilters. Each filter carries explicit peer lists
        (pinned/include/exclude). Members are rendered as marked chat ids
        (-100… for channels, negative for basic groups, positive for users)
        — directly comparable to chats.chat_id stored by dialogs sync.

        Expose-for-everyone folders ("all chats except X") intentionally
        yield an empty member list here — full fallback semantics are not
        needed for the UI folder filter.
        """
        if not self._client:
            raise RuntimeError("Telegram client is not connected")

        # В слое telethon 1.45 RPC папок живёт в messages.*, в старых сборках
        # — в account.*; каскадом, чтобы переживать апгрейды/даунгрейды.
        get_req = (
            getattr(functions.messages, "GetDialogFiltersRequest", None)
            or getattr(functions.account, "GetDialogFiltersRequest", None)
        )
        if get_req is None:
            raise RuntimeError(
                "GetDialogFiltersRequest не найден в этой сборке telethon"
            )
        filters = await self._client(get_req())
        # Response is account.DialogFilters (field .filters) or a bare list —
        # wrapping differs across layers/telethon versions — normalize.
        raw_filters = getattr(filters, "filters", filters)
        if not raw_filters:
            return []

        folders = []
        for flt in raw_filters:
            # DialogFilterDefault = the plain "all chats" view: not a folder.
            if isinstance(flt, types.DialogFilterDefault):
                continue
            title = getattr(flt, "title", None)
            title_text = getattr(title, "text", None) if title is not None else None
            include = [marked_chat_id(p) for p in (getattr(flt, "include_peers", None) or [])]
            exclude = [marked_chat_id(p) for p in (getattr(flt, "exclude_peers", None) or [])]
            pinned = [marked_chat_id(p) for p in (getattr(flt, "pinned_peers", None) or [])]
            members = [c for c in dict.fromkeys(pinned + include) if c not in exclude]
            folders.append({
                "folder_id": flt.id,
                "title": title_text or f"Папка {flt.id}",
                "chat_ids": members,
            })
        return folders

    async def listen(self) -> AsyncGenerator[TelegramMessage, None]:
        while self._running:
            try:
                msg = await asyncio.wait_for(self._queue.get(), timeout=1.0)
                yield msg
            except asyncio.TimeoutError:
                continue

    async def prepare_media_link(
        self,
        chat_id: int,
        message_id: int,
        max_download_seconds: int,
        max_download_mb: int,
    ) -> dict:
        """Fetch one message by id and serve its media from the cache dir.

        The deterministic cache name ({chat_id}_{message_id}{ext}) makes a
        repeated call for the same message hit the file, not Telegram: one
        RPC per call, one download per message ever. Raises ValueError with
        an agent-readable message on any refusal (no media, limits, download
        failure) — the tool layer turns that into the usual {"error": ...}.
        """
        if not self._client:
            raise ValueError("Telegram client is not connected")
        await self.wait_ready(timeout=30)

        # Один RPC: получить сообщение и его медиа одновременно.
        message = await self._client.get_messages(chat_id, ids=message_id)
        if not message:
            raise ValueError("No media found in message")
        kind, media = _media_of(message)
        if kind is None:
            raise ValueError("No media found in message")

        summary = media_summary(message)
        ext = _KIND_EXT.get(kind) or (mimetypes.guess_extension(summary["mime_type"] or "") or "")
        filepath = os.path.join(MEDIA_CACHE_DIR, f"{chat_id}_{message_id}{ext}")

        cached = os.path.exists(filepath)
        if not cached:
            max_bytes = max_download_mb * 1024 * 1024
            if summary["size"] is not None and summary["size"] > max_bytes:
                raise ValueError(
                    f"media is {summary['size']} bytes, over max_download_mb={max_download_mb}"
                )
            os.makedirs(MEDIA_CACHE_DIR, exist_ok=True)
            try:
                file_bytes = await asyncio.wait_for(
                    self._client.download_media(media, bytes),
                    timeout=max_download_seconds,
                )
            except asyncio.TimeoutError:
                raise ValueError(
                    f"download timed out after {max_download_seconds}s (max_download_seconds)"
                )
            if not file_bytes:
                raise ValueError("download failed")
            # Сначала во временный файл, затем атомарный os.replace: обрыв
            # посреди записи не оставляет обрезанный файл, который следующий
            # вызов принял бы за валидный кэш (exists-проверка). Имя tmp
            # уникально на вызов: два параллельных вызова того же сообщения
            # не пишут в один файл, поэтому общий tmp не приводит ни к
            # FileNotFoundError у «проигравшего» вызова, ни к обрезанному
            # кэшу. os.replace атомарен — последний вызов просто перезапишет
            # файл теми же байтами; двойное скачивание допустимо.
            tmp_file = f"{filepath}.{uuid4().hex}.tmp"
            try:
                with open(tmp_file, "wb") as fh:
                    fh.write(file_bytes)
                os.replace(tmp_file, filepath)
            finally:
                if os.path.exists(tmp_file):
                    os.remove(tmp_file)

        return {
            "cached_file": filepath,
            "media_kind": kind,
            "mime_type": summary["mime_type"],
            "size_bytes": summary["size"],
            "file_name": summary["file_name"],
            "cached": cached,
        }
