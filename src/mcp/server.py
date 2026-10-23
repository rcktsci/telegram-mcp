import asyncio
import json
import logging
from datetime import datetime
from typing import Annotated, Literal

import uvicorn
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from sqlalchemy import text

from mcp.server.mcpserver import MCPServer
from pydantic import Field

from src.mcp.tools import (
    list_chats,
    read_messages,
    get_chat_members,
    get_media_link,
    get_message,
    search_messages_since,
    count_messages,
)
from src.config import get_settings
from src.db.connection import get_engine
from src.mcp.auth import BearerAuthMiddleware, get_allowed_ids
from src.telegram.status import get_status_checker

logger = logging.getLogger(__name__)

web_app = FastAPI()


def _forbidden(chat_id: int) -> dict:
    return {"error": f"forbidden: chat {chat_id} not allowed for this token"}


def _scope_blocks(chat_id: int) -> bool:
    """None = no authorization happened (unrestricted); ∅ = nothing allowed."""
    allowed = get_allowed_ids()
    return allowed is not None and chat_id not in allowed


def _parse_utc_datetime(name: str, value: str) -> datetime:
    """Required ISO 8601 with Z/offsets (same parser as read_messages)."""
    if not value:
        raise ValueError(f"{name} is required")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError(f"invalid ISO 8601 datetime: '{value}'")

# mcp 2.x: FastMCP -> MCPServer; transport options (host, stateless_http,
# json_response) moved from the constructor into run()/run_*_async().
mcp_server = MCPServer("telegram-mcp")


def _wrap_chat_list(chats: list[dict]) -> dict:
    """Pack a chat list into one dict.

    A bare list return renders as one text block per element: clients get a
    concatenation of pretty-printed JSON objects, not valid JSON.
    """
    return {"count": len(chats), "chats": chats}


@mcp_server.tool(name="list_chats")
async def list_chats_tool():
    """List all monitored chats with internal IDs and titles. Returns {"count": N, "chats": [...]}."""
    chats = await list_chats()
    allowed = get_allowed_ids()
    if allowed is not None:
        chats = [chat for chat in chats if chat["chat_id"] in allowed]
    return _wrap_chat_list(chats)


@mcp_server.prompt()
def telegram_mcp_guide():
    """Guide for using Telegram MCP tools."""
    return """## Telegram MCP Tools Quick Guide

**chat_id** = internal ID from list_chats (NOT Telegram chat_id)

### Forum chats:

- Non-forum chats have `topics = {}` (empty dict, never null)
- Forum chats have non-empty `topics` field in list_chats
- Explicit topic_id = 0 returns only `General` topic.

### Reading messages argument snippets:

- Last N: `fetch_direction="new_to_old"`
- Oldest N: `fetch_direction="old_to_new"` (default)
- Date range: `start_datetime`, `end_datetime` (ISO 8601)
- Topic only: `topic_id=20489` (from topics dict)
- General topic only: `topic_id=0`
- Search: `search_regex="pattern"`
- Full-text: `search_text="модели ИИ"` (RU+EN word forms, websearch syntax:
  `"exact phrase"`, `-exclude`; faster and broader than the regex)
- Attachments: `has_media=true` (or `false` for strictly messages without
  files); `media_kind` in the result is the type: photo, video, document,
  voice, audio, animation, sticker, video_note

### Downloading attachments:

- `get_media_link(chat_id, message_id)` returns a time-limited public URL
  (`media_links.public_url` must be configured, otherwise it errors) plus
  file metadata: `{"url", "file_name", "media_kind", "mime_type",
  "size_bytes", "expires_at", "cached"}`. The file downloads once and is
  then served from cache; give the URL to the user as-is, it dies at
  `expires_at` and 404s afterwards.

### Pagination:

- old_to_new: `min_message_id` = oldest ID + 1
- new_to_old: `max_message_id` = newest ID - 1

### Cross-chat search:

- `search_messages(start_datetime="2026-10-20T00:00:00+07:00")` — all
  messages in watched chats allowed for your token since the moment, newest
  first. Optional:
  `end_datetime`, `search_regex`, `search_text` (full-text, RU+EN word
  forms), `limit` (max 1000, default 200).
- Need just the NUMBER of messages per chat for a period —
  `count_messages(chat_ids=[...], start_datetime=..., end_datetime=...)`,
  not read_messages. Zeros included; ids not allowed for your token land
  in `forbidden`."""


@mcp_server.tool(name="read_messages")
async def read_messages_tool(
    chat_id: Annotated[int, Field(description="Internal ID from list_chats (not the Telegram chat_id)")],
    limit: int = 100,
    start_datetime: str = None,
    min_message_id: int = None,
    sender_id: int = None,
    search_regex: str = None,
    search_text: str = None,
    has_media: Annotated[bool | None, Field(description="true = only messages with attachments, false = only without, null = no filter")] = None,
    return_raw: bool = False,
    end_datetime: str = None,
    topic_id: Annotated[int | None, Field(description="Forum topic ID from the topics dict in list_chats; 0 = General, null = all topics")] = None,
    fetch_direction: Literal["old_to_new", "new_to_old"] = "old_to_new",
    max_message_id: int = None,
):
    """Fetch messages from a chat with optional filters. Returns messages in chronological order (oldest first) by default. Deleted messages excluded. Check has_more flag for pagination."""
    if _scope_blocks(chat_id):
        return _forbidden(chat_id)
    result = await read_messages(
        chat_id=chat_id,
        limit=limit,
        start_datetime=start_datetime,
        min_message_id=min_message_id,
        sender_id=sender_id,
        search_regex=search_regex,
        search_text=search_text,
        has_media=has_media,
        return_raw=return_raw,
        end_datetime=end_datetime,
        topic_id=topic_id,
        fetch_direction=fetch_direction,
        max_message_id=max_message_id,
    )
    return result


@mcp_server.tool(name="search_messages")
async def search_messages_tool(start_datetime: str, end_datetime: str = None, limit: int = 200, search_regex: str = None, search_text: str = None):
    """Search messages across watched chats allowed for your token by date range (newest first). Use for cross-chat questions like 'what arrived today' in ONE call instead of a per-chat read_messages loop. Returns chat_title, chat_id (internal, from list_chats), telegram_chat_id, message_id, sender, date, text. Only watched chats; deleted messages excluded. Datetimes are ISO 8601 and may carry an offset (e.g. '2026-10-20T00:00:00+07:00'). Check has_more flag when limit is hit."""
    result = await search_messages_since(
        start_datetime=start_datetime,
        end_datetime=end_datetime,
        limit=limit,
        search_regex=search_regex,
        search_text=search_text,
        allowed_internal_ids=get_allowed_ids(),
    )
    return result


@mcp_server.tool(name="count_messages")
async def count_messages_tool(
    chat_ids: Annotated[list[int], Field(description="Internal chat IDs from list_chats (not Telegram chat_ids)")],
    start_datetime: Annotated[str, Field(description="Range start, ISO 8601 (inclusive)")],
    end_datetime: Annotated[str, Field(description="Range end, ISO 8601 (inclusive)")],
):
    """Count non-deleted messages per chat for a period (both bounds inclusive, BETWEEN). Returns {"counts": [{"chat_id", "count"}...] (zeros included, chat_id asc), "total", "forbidden"} — chat_id is internal (from list_chats); ids not allowed for your token are listed in forbidden instead of counted. Cheaper than read_messages when only the number is needed."""
    start_dt = _parse_utc_datetime("start_datetime", start_datetime)
    end_dt = _parse_utc_datetime("end_datetime", end_datetime)
    if not isinstance(chat_ids, list) or not chat_ids:
        raise ValueError("chat_ids must be a non-empty list of ints")
    if start_dt > end_dt:
        raise ValueError("start_datetime must be <= end_datetime")

    requested = sorted(set(chat_ids))
    allowed = get_allowed_ids()
    forbidden: list[int] = []
    if allowed is not None:
        forbidden = [chat_id for chat_id in requested if chat_id not in allowed]
        requested = [chat_id for chat_id in requested if chat_id in allowed]

    result = await count_messages(requested, start_datetime, end_datetime)
    result["forbidden"] = forbidden
    return result


@mcp_server.tool(name="get_chat_members")
async def get_chat_members_tool(chat_id: Annotated[int, Field(description="Internal ID from list_chats (not the Telegram chat_id)")]):
    """Get chat members with IDs, usernames, and names. Requires Telegram API credentials."""
    if _scope_blocks(chat_id):
        return _forbidden(chat_id)
    result = await get_chat_members(chat_id=chat_id)
    return result


@mcp_server.tool(name="get_media_link")
async def get_media_link_tool(
    chat_id: Annotated[int, Field(description="Internal ID from list_chats (not the Telegram chat_id)")],
    message_id: Annotated[int, Field(description="Telegram message ID")],
):
    """Get a time-limited public URL for the media attached to a message (photo, video, document, voice...). The file is cached server-side and served at GET <url> until expires_at. Returns {"url", "file_name", "media_kind", "mime_type", "size_bytes", "expires_at", "cached"}."""
    if _scope_blocks(chat_id):
        return _forbidden(chat_id)
    result = await get_media_link(chat_id=chat_id, message_id=message_id)
    return result


@mcp_server.tool(name="get_message")
async def get_message_tool(
    chat_id: Annotated[int, Field(description="Internal ID from list_chats (not the Telegram chat_id)")],
    message_id: Annotated[int, Field(description="Telegram message ID")],
    return_raw: bool = False,
):
    """Get single message by ID. Use for reading replies or quoted messages."""
    if _scope_blocks(chat_id):
        return _forbidden(chat_id)
    result = await get_message(chat_id=chat_id, message_id=message_id, return_raw=return_raw)
    return result


@web_app.get("/health")
async def health():
    """Проверить здоровье сервиса: БД и соединение с Telegram."""
    errors = []

    # Проверка БД
    try:
        engine = get_engine()
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        db_ok = True
    except Exception as e:
        db_ok = False
        errors.append(f"DB: {str(e)}")

    # Проверка Telegram
    try:
        status_checker = get_status_checker()
        tg_ok = await status_checker.is_connected()
        if not tg_ok:
            errors.append("Telegram: not connected")
    except Exception as e:
        tg_ok = False
        errors.append(f"Telegram: {str(e)}")

    if db_ok and tg_ok:
        return {"status": "ok", "db": "connected", "telegram": "connected"}
    else:
        return JSONResponse(
            {"status": "degraded", "db": "connected" if db_ok else "disconnected", "telegram": "connected" if tg_ok else "disconnected", "errors": errors},
            status_code=503
        )


@web_app.get("/")
async def root():
    return {"service": "telegram-mcp", "version": "1.0.0"}


def run_server():
    import threading
    import asyncio

    def run_mcp():
        # Same Bearer wrapper as main.py: the legacy entry point must not
        # serve an unauthenticated MCP port (deprecated path).
        app = BearerAuthMiddleware(mcp_server.streamable_http_app(stateless_http=True, json_response=True))
        uvicorn.Server(uvicorn.Config(app, host="0.0.0.0", port=8001)).run()

    mcp_thread = threading.Thread(target=run_mcp, daemon=True)
    mcp_thread.start()

    uvicorn.run(web_app, host="0.0.0.0", port=8000, log_level="info")


if __name__ == "__main__":
    run_server()
