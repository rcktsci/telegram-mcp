"""C2: forum-topic creation must not push a dict into the message queue.

The old service_handler enqueued a plain dict; _process_message then crashed
with AttributeError on message.chat_id, killing the worker loop for 5s and
dropping the event. The fix routes topic creation straight to the DB and the
in-memory cache, bypassing the message queue.
"""

import pytest
from sqlalchemy import select

from src.db import Chat, get_session_factory
from src.telegram.client import TelegramListener


async def _seed_chat(chat_id: int, topics: dict | None = None) -> None:
    async with get_session_factory()() as session:
        session.add(Chat(chat_id=chat_id, title="forum", watching=True, user_id=42, topics=topics or {}))
        await session.commit()


async def _get_topics(chat_id: int) -> dict:
    async with get_session_factory()() as session:
        result = await session.execute(select(Chat).where(Chat.chat_id == chat_id))
        chat = result.scalar_one()
        return chat.topics or {}


async def test_topic_created_persists_and_caches(db):
    await _seed_chat(200, topics={"1": "General"})
    listener = TelegramListener("test-session", 1, "h")

    if not hasattr(listener, "_handle_topic_created"):
        pytest.fail("TelegramListener._handle_topic_created is missing — dict still routed via message queue")

    await listener._handle_topic_created(200, 555, "Новая тема")

    assert listener._topic_names[555] == "Новая тема"
    topics = await _get_topics(200)
    assert topics == {"1": "General", "555": "Новая тема"}


async def test_topic_created_for_unknown_chat_does_not_crash(db):
    listener = TelegramListener("test-session", 1, "h")

    await listener._handle_topic_created(999999, 556, "Orphan")

    assert listener._topic_names[556] == "Orphan"  # cache kept for future messages
    async with get_session_factory()() as session:
        from sqlalchemy import select
        row = (await session.execute(select(Chat).where(Chat.chat_id == 999999))).scalar_one_or_none()
    assert row is None, "orphan topic must not create a chat row"


async def test_topic_created_on_legacy_empty_array_topics(db):
    """Old rows may hold topics = [] (model default was list); the merge must
    coerce it to an object instead of failing the jsonb concatenation."""
    async with get_session_factory()() as session:
        session.add(Chat(chat_id=201, title="legacy", watching=True, user_id=42, topics=[]))
        await session.commit()

    listener = TelegramListener("test-session", 1, "h")
    await listener._handle_topic_created(201, 557, "Тема")

    topics = await _get_topics(201)
    assert topics == {"557": "Тема"}, f"legacy [] topics must be upgraded to an object, got: {topics!r}"
