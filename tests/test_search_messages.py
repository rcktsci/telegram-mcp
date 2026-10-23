"""search_messages_since: cross-watched-chat date search in ONE call.

The per-chat read_messages loop answered "what arrived today" in N calls;
the dedicated tool joins watch state into the query (only watched chats,
deleted excluded) and returns the newest-first feed with chat titles.
"""

from datetime import datetime, timedelta, timezone

import pytest

from src.db import Chat, Message, get_session_factory
from src.mcp import tools as mcp_tools


async def _seed_watching(chat_id: int, title: str, watching: bool = True) -> None:
    async with get_session_factory()() as session:
        session.add(Chat(chat_id=chat_id, title=title, watching=watching, user_id=42))
        await session.commit()


async def test_returns_only_watching_chats_within_range(db):
    now = datetime.now(timezone.utc)

    await _seed_watching(-100111, "Watching")
    await _seed_watching(-100222, "Ignored", watching=False)
    await _seed_watching(-100333, "Too old")

    async with get_session_factory()() as session:
        session.add(Message(
            chat_id=-100111, message_id=1,
            text="fresh today", date=now - timedelta(minutes=5), raw_json={},
        ))
        session.add(Message(
            chat_id=-100222, message_id=2,
            text="not watched", date=now - timedelta(minutes=5), raw_json={},
        ))
        session.add(Message(
            chat_id=-100333, message_id=3,
            text="before range", date=now - timedelta(days=2), raw_json={},
        ))
        await session.commit()

    result = await mcp_tools.search_messages_since(
        start_datetime=(now - timedelta(hours=1)).isoformat()
    )

    texts = [m["text"] for m in result["messages"]]
    assert texts == ["fresh today"]

    row = result["messages"][0]
    assert row["chat_title"] == "Watching"
    assert row["telegram_chat_id"] == -100111
    assert result["messages_returned"] == 1
    assert result["has_more"] is False


async def test_newest_first_across_chats_with_limit_and_has_more(db):
    now = datetime.now(timezone.utc)

    await _seed_watching(-100555, "Bank A")
    await _seed_watching(-100666, "Bank B")

    async with get_session_factory()() as session:
        # 5 messages, interleaved across two chats; higher id = newer.
        for i in range(1, 6):
            session.add(Message(
                chat_id=-100555 if i % 2 else -100666,
                message_id=i,
                text=f"m{i}",
                date=now - timedelta(minutes=10 - i),
                raw_json={},
            ))
        await session.commit()

    result = await mcp_tools.search_messages_since(
        start_datetime=(now - timedelta(hours=1)).isoformat(),
        limit=3,
    )

    assert result["messages_returned"] == 3
    assert result["has_more"] is True

    ids = [m["message_id"] for m in result["messages"]]
    assert ids == [5, 4, 3]
    titles = [m["chat_title"] for m in result["messages"]]
    # m5 -> Bank A (odd), m4 -> Bank B (even), m3 -> Bank A.
    assert titles == ["Bank A", "Bank B", "Bank A"]


async def test_limit_bounds_are_validated(db):
    await _seed_watching(-100777, "t")

    with pytest.raises(ValueError, match="Limit"):
        await mcp_tools.search_messages_since(
            start_datetime="2026-01-01T00:00:00+00:00",
            limit=0,
        )
    with pytest.raises(ValueError, match="Limit"):
        await mcp_tools.search_messages_since(
            start_datetime="2026-01-01T00:00:00+00:00",
            limit=1001,
        )


async def test_regex_timeout_maps_to_value_error(db):
    await _seed_watching(307, "t")
    async with get_session_factory()() as session:
        session.add(Message(
            chat_id=307, message_id=1,
            text="hello world", date=datetime.now(timezone.utc), raw_json={},
        ))
        await session.commit()

    # Mirrors test_read_regex_timeout: a statement timeout raised by the
    # messages SELECT must surface the friendly ValueError, not a raw DB error.
    import src.db.connection as conn_mod

    original_factory = conn_mod._session_factory

    class FailingSession:
        def __init__(self, real):
            self._real = real

        async def __aenter__(self):
            await self._real.__aenter__()
            return self

        async def __aexit__(self, *a):
            return await self._real.__aexit__(*a)

        async def execute(self, stmt, *a, **kw):
            sql = str(stmt)
            if "FROM messages" in sql:
                raise Exception("canceling statement due to statement timeout")
            return await self._real.execute(stmt, *a, **kw)

    def factory():
        return FailingSession(original_factory())

    conn_mod._session_factory = factory
    try:
        with pytest.raises(ValueError, match="timeout"):
            await mcp_tools.search_messages_since(
                start_datetime="2026-01-01T00:00:00+00:00",
                search_regex="hello",
            )
    finally:
        conn_mod._session_factory = original_factory


async def test_search_text_matches_russian_inflections(db):
    now = datetime.now(timezone.utc)

    await _seed_watching(-100778, "Ru chat")
    async with get_session_factory()() as session:
        session.add(Message(
            chat_id=-100778, message_id=1,
            text="Обзор новых моделей нейросетей", date=now, raw_json={},
        ))
        session.add(Message(
            chat_id=-100778, message_id=2,
            text="погода и праздники", date=now, raw_json={},
        ))
        await session.commit()

    result = await mcp_tools.search_messages_since(
        start_datetime=(now - timedelta(hours=1)).isoformat(),
        search_text="модель",
    )

    texts = [m["text"] for m in result["messages"]]
    assert texts == ["Обзор новых моделей нейросетей"]


async def test_search_text_matches_english_inflections(db):
    now = datetime.now(timezone.utc)

    await _seed_watching(-100779, "En chat")
    async with get_session_factory()() as session:
        session.add(Message(
            chat_id=-100779, message_id=1,
            text="Training models is cheap", date=now, raw_json={},
        ))
        await session.commit()

    result = await mcp_tools.search_messages_since(
        start_datetime=(now - timedelta(hours=1)).isoformat(),
        search_text="models",
    )

    texts = [m["text"] for m in result["messages"]]
    assert texts == ["Training models is cheap"]
