import asyncio
import json
import logging
import time
import uuid
from collections.abc import Collection
from datetime import datetime, timedelta, timezone
from typing import Literal, Optional

from sqlalchemy import select, and_, or_, func, cast, String, text

from src.db.connection import get_session_factory
from src.db.models import Chat, Message
from src.db.repositories import AttachmentDownloadRepository
from src.telegram.client import TelegramListener

logger = logging.getLogger(__name__)


async def _resolve_chat_id(internal_id: int) -> int:
    """Convert internal id to Telegram chat_id."""
    session_factory = get_session_factory()
    async with session_factory() as session:
        result = await session.execute(
            select(Chat.chat_id).where(Chat.id == internal_id)
        )
        row = result.scalar_one_or_none()
        if row is None:
            raise ValueError(f"Chat with internal id {internal_id} not found")
        return row


def _fts_conditions(search_text: Optional[str]) -> list:
    """Full-text search on the fts_vector GIN index (migration 012).

    One OR over two per-language queries: each keeps its own AND-of-words
    semantics, while the combined document vector stores both stems.
    """
    return [or_(
        text("fts_vector(messages.text) @@ websearch_to_tsquery('russian', :fts_q)").bindparams(fts_q=search_text),
        text("fts_vector(messages.text) @@ websearch_to_tsquery('english', :fts_q)").bindparams(fts_q=search_text),
    )]


def _message_filter_conditions(
    *,
    start_dt: Optional[datetime],
    end_dt: Optional[datetime],
    min_message_id: Optional[int],
    max_message_id: Optional[int],
    sender_id: Optional[int],
    search_regex: Optional[str],
    search_text: Optional[str],
    topic_id: Optional[int],
    has_media: Optional[bool],
) -> list:
    """WHERE conditions for read_messages, assembled in one place.

    Each input filter maps to exactly one condition here, so extending the
    filter set touches a single list instead of an if-chain that mutates
    the query incrementally. Behavior must stay identical to the previous
    inline chain (covered by the read_messages tests).
    """
    conditions = []
    if start_dt:
        conditions.append(Message.date >= start_dt)
    if end_dt:
        conditions.append(Message.date <= end_dt)
    if min_message_id is not None:
        conditions.append(Message.message_id >= min_message_id)
    if max_message_id is not None:
        conditions.append(Message.message_id <= max_message_id)
    if sender_id:
        conditions.append(Message.sender_id == sender_id)
    # The timeout must span the actual SELECT: set before, reset after
    # (finally), so a pathological regex cannot run unbounded on PG.
    if search_regex:
        conditions.append(Message.text.op("~")(search_regex))
    if search_text:
        conditions.extend(_fts_conditions(search_text))
    if topic_id is not None:
        if topic_id == 0:
            conditions.append(Message.topic_id.is_(None))
        else:
            conditions.append(Message.topic_id == topic_id)
    if has_media is not None:
        if has_media:
            conditions.append(
                and_(
                    Message.raw_json.isnot(None),
                    func.jsonb_typeof(Message.raw_json['media']) != 'null'
                )
            )
        else:
            conditions.append(
                or_(
                    Message.raw_json.is_(None),
                    func.jsonb_typeof(Message.raw_json['media']) == 'null',
                    Message.raw_json['media'].is_(None)
                )
            )
    return conditions


def _media_kind(raw_json: Optional[dict]) -> Optional[str]:
    """Attachment type from the stored compact summary (raw_json["media"].kind)."""
    media = (raw_json or {}).get("media")
    return media.get("kind") if isinstance(media, dict) else None


async def _get_listener_for_chat(chat_id: int):
    """Get or create a persistent TelegramListener for the chat's user."""
    from src.config import get_settings
    logger = logging.getLogger(__name__)
    
    logger.debug(f"_get_listener_for_chat called with chat_id={chat_id}")
    
    session_factory = get_session_factory()
    async with session_factory() as session:
        result = await session.execute(
            select(Chat.user_id).where(Chat.chat_id == chat_id)
        )
        chat_user_id = result.scalar_one_or_none()
        logger.debug(f"  chat_user_id={chat_user_id}")
    
    if not chat_user_id:
        logger.warning(f"  Returning None because chat_user_id is None")
        return None
    
    global _user_id_to_listener
    async with _init_lock:
        if _user_id_to_listener is None:
            logger.debug("  Building _user_id_to_listener mapping...")
            await _build_listeners()
        elif chat_user_id not in _user_id_to_listener:
            # Retry policy: empty map means every session failed last time —
            # retry on each call; a non-empty map missing this user means the
            # account is unknown or appeared recently — rebuild at most once
            # per interval instead of caching the miss forever.
            stale = (
                _listeners_built_at is None
                or (time.monotonic() - _listeners_built_at) > _LISTENER_REBUILD_INTERVAL
            )
            if not _user_id_to_listener or stale:
                logger.debug("  Rebuilding _user_id_to_listener mapping...")
                await _build_listeners()
        else:
            logger.debug(f"  Using cached _user_id_to_listener")
        
        listener = _user_id_to_listener.get(chat_user_id)
        if listener is None:
            logger.warning(f"  No listener found for chat_user_id={chat_user_id}")
            return None
        
        if not listener._client or not listener._client.is_connected():
            logger.info(f"  Listener for user {chat_user_id} disconnected, reconnecting...")
            try:
                await listener.reconnect()
            except RuntimeError as e:
                logger.error(f"  Reconnection failed: {e}")
                return None
        
        logger.debug(f"  Returning listener: {listener is not None}")
        return listener


_user_id_to_listener: dict = None  # type: ignore[assignment]
_init_lock = asyncio.Lock()
_LISTENER_REBUILD_INTERVAL = 60.0
_listeners_built_at: float | None = None


async def _build_listeners() -> None:
    """(Re)build the user_id -> TelegramListener map from configured sessions."""
    global _user_id_to_listener, _listeners_built_at

    from src.config import get_settings

    settings = get_settings()
    mapping: dict = {}
    for s in settings.telegram.sessions:
        if not s.session_string:
            continue
        listener = TelegramListener(
            s.session_string,
            settings.telegram.api_id,
            settings.telegram.api_hash
        )
        try:
            await listener.start()
            ready = await listener.wait_ready()
            if not ready:
                logger.warning(f"  wait_ready() timed out for session {s.session_string[:10]}")
                continue
            me = await listener._client.get_me()
            logger.debug(f"  Mapped user_id={me.id}")
            mapping[me.id] = listener
        except Exception:
            logger.exception(f"  Failed to initialize session for user {s.session_string[:10]}")
    _user_id_to_listener = mapping
    _listeners_built_at = time.monotonic()


async def list_chats() -> list[dict]:
    session_factory = get_session_factory()
    async with session_factory() as session:
        result = await session.execute(
            select(Chat).where(Chat.watching == True)
        )
        chats = result.scalars().all()
        
        return [
            {
                "chat_id": chat.id,
                "title": chat.title,
                "watching": chat.watching,
                "members": [],
                "min_message_id": chat.min_message_id,
                "max_message_id": chat.max_message_id,
                "min_message_date": chat.min_message_date.isoformat() if chat.min_message_date else None,
                "max_message_date": chat.max_message_date.isoformat() if chat.max_message_date else None,
                "topics": chat.topics if chat.topics else {},
            }
            for chat in chats
        ]


async def read_messages(
    chat_id: int,
    start_datetime: Optional[str] = None,
    min_message_id: Optional[int] = None,
    limit: int = 100,
    sender_id: Optional[int] = None,
    search_regex: Optional[str] = None,
    search_text: Optional[str] = None,
    has_media: Optional[bool] = None,
    return_raw: bool = False,
    end_datetime: Optional[str] = None,
    topic_id: Optional[int] = None,
    fetch_direction: Literal["old_to_new", "new_to_old"] = "old_to_new",
    max_message_id: Optional[int] = None,
) -> dict:
    telegram_chat_id = await _resolve_chat_id(chat_id)
    
    if limit < 1 or limit > 1000:
        raise ValueError("Limit must be between 1 and 1000")
    
    if fetch_direction not in ("old_to_new", "new_to_old"):
        raise ValueError("fetch_direction must be 'old_to_new' or 'new_to_old'")
    
    start_dt = None
    if start_datetime:
        start_dt = datetime.fromisoformat(start_datetime.replace("Z", "+00:00"))
    
    end_dt = None
    if end_datetime:
        end_dt = datetime.fromisoformat(end_datetime.replace("Z", "+00:00"))
    
    session_factory = get_session_factory()
    async with session_factory() as session:
        watching_result = await session.execute(
            select(Chat.watching).where(Chat.chat_id == telegram_chat_id)
        )
        is_watching = watching_result.scalar_one_or_none()
        if not is_watching:
            raise ValueError("Chat is not being watched. Messages can only be read from watched chats.")
        
        query = select(Message).where(
            Message.chat_id == telegram_chat_id,
            Message.is_deleted == False,
            *_message_filter_conditions(
                start_dt=start_dt,
                end_dt=end_dt,
                min_message_id=min_message_id,
                max_message_id=max_message_id,
                sender_id=sender_id,
                search_regex=search_regex,
                search_text=search_text,
                topic_id=topic_id,
                has_media=has_media,
            ),
        )

        use_regex_timeout = search_regex is not None

        order_date = Message.date.desc() if fetch_direction == "new_to_old" else Message.date.asc()
        order_id = Message.message_id.desc() if fetch_direction == "new_to_old" else Message.message_id.asc()
        # message_id breaks date ties: timestamps have second precision and
        # pagination is keyed on message_id, so the order must be total —
        # with the tie-breaker following the same direction as the date key.
        query = query.order_by(order_date, order_id).limit(limit + 1)

        if use_regex_timeout:
            await session.execute(text("SET statement_timeout = '5s'"))
        try:
            result = await session.execute(query)
            messages = result.scalars().all()
        except Exception as e:
            if use_regex_timeout and "statement timeout" in str(e).lower():
                raise ValueError("Regex search timeout - pattern too complex or database overloaded") from e
            raise
        finally:
            if use_regex_timeout:
                try:
                    await session.execute(text("RESET statement_timeout"))
                except Exception as e:
                    logger.warning(f"Failed to RESET statement_timeout: {e}")

        # Определяем has_more и обрезаем до лимита
        has_more = len(messages) > limit
        if has_more:
            messages = messages[:limit]

        return {
            "messages": [
                {
                    "message_id": m.message_id,
                    "sender_id": m.sender_id,
                    "sender_username": m.sender_username,
                    "sender_name": m.sender_name,
                    "raw": m.raw_json,
                    "date": m.date.isoformat() if m.date else None,
                    "has_media": bool(m.raw_json and m.raw_json.get("media")),
                    "media_kind": _media_kind(m.raw_json),
                    "topic_id": m.topic_id,
                    "topic_name": m.topic_name,
                }
                if return_raw else {
                    "message_id": m.message_id,
                    "sender_id": m.sender_id,
                    "sender_username": m.sender_username,
                    "sender_name": m.sender_name,
                    "text": m.text,
                    "date": m.date.isoformat() if m.date else None,
                    "has_media": bool(m.raw_json and m.raw_json.get("media")),
                    "media_kind": _media_kind(m.raw_json),
                    "topic_id": m.topic_id,
                    "topic_name": m.topic_name,
                }
                for m in messages
            ],
            "has_more": has_more,
        }


async def search_messages_since(
    start_datetime: str,
    end_datetime: Optional[str] = None,
    limit: int = 200,
    search_regex: Optional[str] = None,
    search_text: Optional[str] = None,
    allowed_internal_ids: Optional[Collection[int]] = None,
) -> dict:
    """Search messages across watched chats by date range (newest first).

    Answers cross-chat questions like "what arrived today" in ONE call
    instead of a per-chat read_messages loop. Only watched chats are
    searched; deleted messages are excluded. chat_id is internal (same
    ids as list_chats / read_messages); telegram_chat_id is included for
    referencing the source chat directly.

    allowed_internal_ids: None = unrestricted (internal calls); a set =
    only these internal chat ids (token scope); empty set = no results.
    """
    if not start_datetime:
        raise ValueError("start_datetime is required")
    start_dt = datetime.fromisoformat(start_datetime.replace("Z", "+00:00"))
    end_dt = None
    if end_datetime:
        end_dt = datetime.fromisoformat(end_datetime.replace("Z", "+00:00"))

    if limit < 1 or limit > 1000:
        raise ValueError("Limit must be between 1 and 1000")

    conditions = [
        Chat.watching == True,  # noqa: E712 — SQLAlchemy truthiness, not a typo
        Message.is_deleted == False,  # noqa: E712
        Message.date >= start_dt,
    ]
    if allowed_internal_ids is not None:
        conditions.append(Chat.id.in_(set(allowed_internal_ids)))
    if end_dt:
        conditions.append(Message.date <= end_dt)
    if search_regex:
        conditions.append(Message.text.op("~")(search_regex))
    if search_text:
        conditions.extend(_fts_conditions(search_text))

    session_factory = get_session_factory()
    async with session_factory() as session:
        use_regex_timeout = search_regex is not None
        if use_regex_timeout:
            await session.execute(text("SET statement_timeout = '5s'"))
        try:
            query = (
                select(
                    Chat.id.label("internal_id"),
                    Chat.title,
                    Message.chat_id,
                    Message.message_id,
                    Message.sender_id,
                    Message.sender_name,
                    Message.date,
                    Message.text,
                    # Только kind из сводки: raw_json целиком — легаси-блоб
                    # до сотен КБ на строку, ответ его всё равно не содержит.
                    Message.raw_json["media"]["kind"].astext.label("media_kind"),
                )
                .join(Chat, Chat.chat_id == Message.chat_id)
                .where(*conditions)
                # message_id breaks date ties: timestamps have second
                # precision, newest-first needs a total order.
                .order_by(Message.date.desc(), Message.message_id.desc())
                .limit(limit + 1)
            )
            rows = (await session.execute(query)).all()
        except Exception as e:
            if use_regex_timeout and "statement timeout" in str(e).lower():
                raise ValueError("Regex search timeout - pattern too complex or database overloaded") from e
            raise
        finally:
            if use_regex_timeout:
                try:
                    await session.execute(text("RESET statement_timeout"))
                except Exception as e:
                    logger.warning(f"Failed to RESET statement_timeout: {e}")

        # limit+1 fetch: has_more is derived like read_messages does.
        has_more = len(rows) > limit
        if has_more:
            rows = rows[:limit]

        return {
            "messages": [
                {
                    "chat_id": r.internal_id,
                    "chat_title": r.title,
                    "telegram_chat_id": r.chat_id,
                    "message_id": r.message_id,
                    "sender_id": r.sender_id,
                    "sender_name": r.sender_name,
                    "date": r.date.isoformat() if r.date else None,
                    "text": r.text,
                    "media_kind": r.media_kind,
                }
                for r in rows
            ],
            "messages_returned": len(rows),
            "has_more": has_more,
        }


async def count_messages(
    chat_ids: list[int],
    start_datetime: str,
    end_datetime: str,
) -> dict:
    """Count non-deleted messages per watched chat in [start, end].

    chat_ids are internal ids. LEFT JOIN keeps zero-count chats alive;
    the period/deleted conditions live in ON (not WHERE) so they filter
    only the joined messages, never drop the chat row. COUNT(messages.id),
    not COUNT(*): unmatched rows must not count as 1.
    """
    start_dt = datetime.fromisoformat(start_datetime.replace("Z", "+00:00"))
    end_dt = datetime.fromisoformat(end_datetime.replace("Z", "+00:00"))

    session_factory = get_session_factory()
    async with session_factory() as session:
        query = (
            select(
                Chat.id.label("chat_id"),
                func.count(Message.id).label("cnt"),
            )
            .outerjoin(
                Message,
                and_(
                    Message.chat_id == Chat.chat_id,
                    Message.is_deleted == False,  # noqa: E712
                    Message.date >= start_dt,
                    Message.date <= end_dt,
                ),
            )
            .where(
                Chat.watching == True,  # noqa: E712
                Chat.id.in_(chat_ids),
            )
            .group_by(Chat.id)
            .order_by(Chat.id)
        )
        rows = (await session.execute(query)).all()

    counts = [{"chat_id": r.chat_id, "count": r.cnt} for r in rows]
    return {
        "counts": counts,
        "total": sum(c["count"] for c in counts),
    }


async def get_message(
    chat_id: int,
    message_id: int,
    return_raw: bool = False,
) -> Optional[dict]:
    telegram_chat_id = await _resolve_chat_id(chat_id)
    
    session_factory = get_session_factory()
    async with session_factory() as session:
        watching_result = await session.execute(
            select(Chat.watching).where(Chat.chat_id == telegram_chat_id)
        )
        is_watching = watching_result.scalar_one_or_none()
        if not is_watching:
            raise ValueError("Chat is not being watched. Messages can only be read from watched chats.")
        
        query = select(Message).where(
            Message.chat_id == telegram_chat_id,
            Message.message_id == message_id,
            Message.is_deleted == False
        )
        result = await session.execute(query)
        m = result.scalar_one_or_none()
        
        if not m:
            return None
        
        if return_raw:
            return {
                "message_id": m.message_id,
                "sender_id": m.sender_id,
                "sender_username": m.sender_username,
                "sender_name": m.sender_name,
                "raw": m.raw_json,
                "date": m.date.isoformat() if m.date else None,
                "has_media": bool(m.raw_json and m.raw_json.get("media")),
                "media_kind": _media_kind(m.raw_json),
                "topic_id": m.topic_id,
                "topic_name": m.topic_name,
            }
        
        return {
            "message_id": m.message_id,
            "sender_id": m.sender_id,
            "sender_username": m.sender_username,
            "sender_name": m.sender_name,
            "text": m.text,
            "date": m.date.isoformat() if m.date else None,
            "has_media": bool(m.raw_json and m.raw_json.get("media")),
            "media_kind": _media_kind(m.raw_json),
            "topic_id": m.topic_id,
            "topic_name": m.topic_name,
        }


async def get_chat_members(chat_id: int) -> dict:
    """Вернуть members или ошибку."""
    from src.config import get_settings
    
    telegram_chat_id = await _resolve_chat_id(chat_id)
    
    settings = get_settings()
    if not settings.telegram.api_id or not settings.telegram.api_hash:
        return {"error": "Telegram API not configured"}
    
    valid_sessions = [s for s in settings.telegram.sessions if s.session_string]
    if not valid_sessions:
        return {"error": "No valid sessions configured"}
    
    listener = await _get_listener_for_chat(telegram_chat_id)
    if not listener:
        return {"error": "No valid session available for this chat"}
    
    try:
        members = await listener.get_chat_members(telegram_chat_id)
        return {"members": members}
    except Exception as e:
        return {"error": str(e)}


async def get_media_link(chat_id: int, message_id: int) -> dict:
    """Time-limited public link to the message's media; caches the file once."""
    from src.config import get_settings

    telegram_chat_id = await _resolve_chat_id(chat_id)

    settings = get_settings()
    media_links = settings.media_links
    if not media_links.public_url:
        # fail closed: without a public base URL there is nowhere to serve
        # /files/<code> from, so refuse instead of handing out a dead link.
        return {"error": "media_links.public_url is not configured"}

    if not settings.telegram.api_id or not settings.telegram.api_hash:
        return {"error": "Telegram API not configured"}

    valid_sessions = [s for s in settings.telegram.sessions if s.session_string]
    if not valid_sessions:
        return {"error": "No valid sessions configured"}

    listener = await _get_listener_for_chat(telegram_chat_id)
    if not listener:
        return {"error": "No valid session available for this chat"}

    try:
        prepared = await listener.prepare_media_link(
            telegram_chat_id,
            message_id,
            max_download_seconds=media_links.max_download_seconds,
            max_download_mb=media_links.max_download_mb,
        )
    except Exception as e:
        return {"error": str(e)}

    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(seconds=media_links.ttl_seconds)
    download_code = uuid.uuid4().hex
    async with get_session_factory()() as session:
        await AttachmentDownloadRepository(session).insert(
            download_code=download_code,
            chat_id=telegram_chat_id,
            message_id=message_id,
            media_kind=prepared["media_kind"],
            file_name=prepared["file_name"],
            mime_type=prepared["mime_type"],
            size_bytes=prepared["size_bytes"],
            cached_file=prepared["cached_file"],
            created_at=now,
            expires_at=expires_at,
        )

    return {
        "url": f"{media_links.public_url.rstrip('/')}/files/{download_code}",
        "file_name": prepared["file_name"],
        "media_kind": prepared["media_kind"],
        "mime_type": prepared["mime_type"],
        "size_bytes": prepared["size_bytes"],
        "expires_at": expires_at.isoformat(),
        "cached": prepared["cached"],
    }
