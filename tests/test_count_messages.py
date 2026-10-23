"""count_messages_tool: per-chat message counts for a period.

Dates are required, ISO 8601 (Z/offsets), BETWEEN semantics (both bounds
inclusive). Only watched chats and non-deleted messages count; zero
counters are returned, not dropped. Scope in the wrapper: None = all
requested; otherwise the intersection counts and the rest lands in
`forbidden`. counts are ordered by chat_id asc.
"""

from datetime import datetime, timezone

import pytest

import src.mcp.server as server_module
from src.db import Chat, Message, get_session_factory
from src.mcp.auth import set_allowed_ids

START = "2026-10-01T00:00:00+00:00"
END = "2026-10-31T23:59:59+00:00"


async def _seed_scoping_world() -> dict:
    """A: watching, 3 in-period messages (both boundaries + middle) + 1
    deleted + 1 out-of-period; B: watching, none; C: NOT watching, 1."""
    async with get_session_factory()() as session:
        a = Chat(chat_id=1601, title="a", watching=True)
        b = Chat(chat_id=1602, title="b", watching=True)
        c = Chat(chat_id=1603, title="c", watching=False)
        session.add_all([a, b, c])
        await session.flush()

        def msg(chat, mid, text, day, deleted=False):
            return Message(
                chat_id=chat.chat_id, message_id=mid, text=text,
                date=datetime(2026, 10, day, 12, 0, tzinfo=timezone.utc),
                is_deleted=deleted,
            )

        session.add_all([
            msg(a, 1, "at start", 1),
            msg(a, 2, "middle", 15),
            msg(a, 3, "at end", 31),
            msg(a, 4, "deleted in period", 15, deleted=True),
            Message(
                chat_id=a.chat_id, message_id=5, text="after end",
                date=datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc),
            ),
            msg(c, 1, "not watched", 15),
        ])
        await session.commit()
        return {"a": a.id, "b": b.id, "c": c.id}


async def test_required_params(db):
    with pytest.raises(ValueError, match="start_datetime is required"):
        await server_module.count_messages_tool(chat_ids=[1], start_datetime=None, end_datetime=END)
    with pytest.raises(ValueError, match="end_datetime is required"):
        await server_module.count_messages_tool(chat_ids=[1], start_datetime=START, end_datetime=None)


async def test_invalid_iso_rejected(db):
    with pytest.raises(ValueError, match="invalid ISO 8601 datetime: 'not-a-date'"):
        await server_module.count_messages_tool(chat_ids=[1], start_datetime="not-a-date", end_datetime=END)
    with pytest.raises(ValueError, match="invalid ISO 8601 datetime: 'also-bad'"):
        await server_module.count_messages_tool(chat_ids=[1], start_datetime=START, end_datetime="also-bad")


async def test_start_after_end_rejected(db):
    with pytest.raises(ValueError, match="start_datetime must be <= end_datetime"):
        await server_module.count_messages_tool(
            chat_ids=[1], start_datetime="2026-10-31T00:00:00+00:00", end_datetime="2026-10-01T00:00:00+00:00",
        )


async def test_chat_ids_must_be_non_empty_list(db):
    with pytest.raises(ValueError, match="chat_ids must be a non-empty list of ints"):
        await server_module.count_messages_tool(chat_ids=[], start_datetime=START, end_datetime=END)
    with pytest.raises(ValueError, match="chat_ids must be a non-empty list of ints"):
        await server_module.count_messages_tool(chat_ids="1,2", start_datetime=START, end_datetime=END)


async def test_between_inclusive_and_deleted_excluded(db):
    ids = await _seed_scoping_world()
    set_allowed_ids(None)

    result = await server_module.count_messages_tool(
        chat_ids=[ids["a"]], start_datetime=START, end_datetime=END,
    )

    # m1 (== start) + m2 (middle) + m3 (== end); deleted and after-end excluded
    assert result["counts"] == [{"chat_id": ids["a"], "count": 3}]
    assert result["total"] == 3
    assert result["forbidden"] == []


async def test_zero_chats_survive_and_order_asc(db):
    ids = await _seed_scoping_world()
    set_allowed_ids(None)

    result = await server_module.count_messages_tool(
        chat_ids=[ids["b"], ids["a"]], start_datetime=START, end_datetime=END,
    )

    assert result["counts"] == [
        {"chat_id": ids["a"], "count": 3},
        {"chat_id": ids["b"], "count": 0},
    ]
    assert result["total"] == 3


async def test_watching_only(db):
    ids = await _seed_scoping_world()
    set_allowed_ids(None)

    result = await server_module.count_messages_tool(
        chat_ids=[ids["c"]], start_datetime=START, end_datetime=END,
    )

    assert result["counts"] == []
    assert result["total"] == 0


async def test_scope_foreign_forbidden_intersection_counts(db):
    ids = await _seed_scoping_world()
    set_allowed_ids(frozenset({ids["a"]}))

    result = await server_module.count_messages_tool(
        chat_ids=[ids["a"], ids["b"]], start_datetime=START, end_datetime=END,
    )

    assert result["counts"] == [{"chat_id": ids["a"], "count": 3}]
    assert result["forbidden"] == [ids["b"]]


async def test_scope_empty_all_forbidden(db):
    ids = await _seed_scoping_world()
    set_allowed_ids(frozenset())

    result = await server_module.count_messages_tool(
        chat_ids=[ids["a"], ids["b"]], start_datetime=START, end_datetime=END,
    )

    assert result["counts"] == []
    assert result["total"] == 0
    assert result["forbidden"] == [ids["a"], ids["b"]]


async def test_duplicates_collapse(db):
    ids = await _seed_scoping_world()
    set_allowed_ids(None)

    result = await server_module.count_messages_tool(
        chat_ids=[ids["a"], ids["a"], ids["b"]], start_datetime=START, end_datetime=END,
    )

    assert result["counts"] == [
        {"chat_id": ids["a"], "count": 3},
        {"chat_id": ids["b"], "count": 0},
    ]
    assert result["total"] == 3
