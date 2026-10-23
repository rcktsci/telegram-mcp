"""C1: real-time messages must update chat message bounds.

Before the fix, _process_message saved the message but never touched
chats.min/max_message_id, so webhooks (keyed on max_message_id) never fired
for live traffic.
"""

from datetime import datetime, timezone

from sqlalchemy import select

from src.db import Chat, ChatRepository, get_session_factory
from src.telegram.client import TelegramMessage
from src.telegram.worker import TelegramWorker


async def _seed_chat(chat_id: int, user_id: int | None = None, watching: bool = True) -> None:
    async with get_session_factory()() as session:
        session.add(Chat(chat_id=chat_id, title="t", watching=watching, user_id=user_id))
        await session.commit()


async def _get_chat(chat_id: int) -> Chat:
    async with get_session_factory()() as session:
        result = await session.execute(select(Chat).where(Chat.chat_id == chat_id))
        return result.scalar_one()


def _msg(chat_id: int, message_id: int) -> TelegramMessage:
    return TelegramMessage(
        chat_id=chat_id,
        message_id=message_id,
        sender_id=1,
        sender_username="u",
        sender_name="User",
        text="hello",
        date=datetime.now(timezone.utc),
        raw={"message_id": message_id},
    )


async def test_realtime_message_updates_chat_bounds(db):
    await _seed_chat(100, user_id=42)
    worker = TelegramWorker(session_string="test", api_id=1, api_hash="h")
    worker.user_id = 42

    await worker._process_message(_msg(100, 7))

    chat = await _get_chat(100)
    assert chat.max_message_id == 7
    assert chat.min_message_id == 7
    assert chat.max_message_date is not None


async def test_bounds_only_grow_monotonically(db):
    await _seed_chat(101, user_id=42)
    worker = TelegramWorker(session_string="test", api_id=1, api_hash="h")
    worker.user_id = 42

    await worker._process_message(_msg(101, 10))
    await worker._process_message(_msg(101, 11))
    # Out-of-order delivery must not lower the max.
    await worker._process_message(_msg(101, 9))

    chat = await _get_chat(101)
    assert chat.max_message_id == 11
    assert chat.min_message_id == 9


async def test_message_for_foreign_user_is_ignored(db):
    await _seed_chat(102, user_id=42)
    worker = TelegramWorker(session_string="test", api_id=1, api_hash="h")
    worker.user_id = 43

    await worker._process_message(_msg(102, 5))

    chat = await _get_chat(102)
    assert chat.max_message_id is None
