"""Token scope enforcement in MCP tools (PLAN_AUTH_TOKENS Task 4).

get_allowed_ids() is None = no authorization happened → nothing is
restricted; frozenset() = a token without rights → everything forbidden
(None ≠ ∅). Point tools refuse with a forbidden-error dict BEFORE any
implementation call (get_file/get_chat_members must not touch Telegram);
list_chats filters its output; search filters in SQL by INTERNAL id.
"""

from datetime import datetime, timezone

import src.mcp.server as server_module
from src.db import Chat, Message, get_session_factory
from src.mcp.auth import set_allowed_ids

# Telegram-level chat_ids deliberately ≠ internal ids (fresh schema: 1, 2)
TG_FIRST, TG_SECOND = 1501, 1502


async def _seed_two_chats_with_messages() -> tuple[int, int]:
    """Seed two watching chats (one message each); return internal ids."""
    async with get_session_factory()() as session:
        first = Chat(chat_id=TG_FIRST, title="first", watching=True)
        second = Chat(chat_id=TG_SECOND, title="second", watching=True)
        session.add_all([first, second])
        await session.flush()
        session.add_all([
            Message(
                chat_id=TG_FIRST, message_id=1, text="first chat message",
                date=datetime(2026, 10, 22, 12, 0, tzinfo=timezone.utc),
            ),
            Message(
                chat_id=TG_SECOND, message_id=1, text="second chat message",
                date=datetime(2026, 10, 22, 12, 30, tzinfo=timezone.utc),
            ),
        ])
        await session.commit()
        return first.id, second.id


async def test_empty_scope_blocks_all_and_none_is_unrestricted(db):
    first_id, second_id = await _seed_two_chats_with_messages()

    set_allowed_ids(frozenset())
    listed = await server_module.list_chats_tool()
    found = await server_module.search_messages_tool(start_datetime="2026-01-01T00:00:00+00:00")
    read = await server_module.read_messages_tool(chat_id=first_id)

    assert listed["count"] == 0
    assert found["messages"] == []
    assert read == {"error": f"forbidden: chat {first_id} not allowed for this token"}

    set_allowed_ids(None)
    listed = await server_module.list_chats_tool()
    found = await server_module.search_messages_tool(start_datetime="2026-01-01T00:00:00+00:00")
    read = await server_module.read_messages_tool(chat_id=first_id)

    assert listed["count"] == 2
    assert found["messages_returned"] == 2
    assert len(read["messages"]) == 1


async def test_list_chats_filters_by_scope(db):
    first_id, _ = await _seed_two_chats_with_messages()

    set_allowed_ids(frozenset({first_id}))
    listed = await server_module.list_chats_tool()

    assert listed["count"] == 1
    assert listed["chats"][0]["chat_id"] == first_id


async def test_read_get_message_get_file_members_forbidden_on_foreign_chat(db, monkeypatch):
    first_id, second_id = await _seed_two_chats_with_messages()
    set_allowed_ids(frozenset({first_id}))

    async def forbidden_implementation(*a, **kw):
        raise AssertionError("implementation must not run (and Telegram must not be touched) on a forbidden chat")

    monkeypatch.setattr("src.mcp.tools._resolve_chat_id", forbidden_implementation)

    read = await server_module.read_messages_tool(chat_id=second_id)
    message = await server_module.get_message_tool(chat_id=second_id, message_id=1)
    file = await server_module.get_file_tool(chat_id=second_id, message_id=1)
    members = await server_module.get_chat_members_tool(chat_id=second_id)

    expected = {"error": f"forbidden: chat {second_id} not allowed for this token"}
    assert read == expected
    assert message == expected
    assert file == expected
    assert members == expected


async def test_search_filters_by_internal_id(db):
    first_id, _ = await _seed_two_chats_with_messages()

    set_allowed_ids(frozenset({first_id}))
    found = await server_module.search_messages_tool(start_datetime="2026-01-01T00:00:00+00:00")

    assert found["messages_returned"] == 1
    assert found["messages"][0]["chat_id"] == first_id
    assert found["messages"][0]["telegram_chat_id"] == TG_FIRST
    assert found["messages"][0]["text"] == "first chat message"


async def test_search_empty_scope_returns_empty(db):
    await _seed_two_chats_with_messages()

    set_allowed_ids(frozenset())
    found = await server_module.search_messages_tool(start_datetime="2026-01-01T00:00:00+00:00")

    assert found["messages"] == []
    assert found["messages_returned"] == 0
    assert found["has_more"] is False
