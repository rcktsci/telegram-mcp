"""Webhook checkpoint integrity.

Before the fix, _post swallowed HTTP/network errors and _update_last_message_ids
ran unconditionally, so a failed delivery still advanced the checkpoint and the
notifications were lost forever. Also, watched chats without any messages were
re-selected every interval (checkpoint never set), spamming the receiver.
"""

from datetime import datetime, timezone

from sqlalchemy import select

from src.db import Chat, Message, get_session_factory
from src.telegram.webhook import WebhookRunner


class FailingRunner(WebhookRunner):
    async def _post(self, payload: dict) -> bool:
        return False


class OkRunner(WebhookRunner):
    async def _post(self, payload: dict) -> bool:
        return True


async def _seed(chat_id: int, *, watching: bool = True, max_id=None, checkpoint=None) -> int:
    async with get_session_factory()() as session:
        session.add(Chat(
            chat_id=chat_id,
            title="t",
            watching=watching,
            user_id=42,
            max_message_id=max_id,
            last_webhook_message_id=checkpoint,
        ))
        await session.commit()
        session.add(Message(
            chat_id=chat_id,
            message_id=max_id or 1,
            text="x",
            date=datetime.now(timezone.utc),
            raw_json={},
        ))
        await session.commit()


async def _checkpoint(telegram_chat_id: int):
    async with get_session_factory()() as session:
        result = await session.execute(
            select(Chat.last_webhook_message_id).where(Chat.chat_id == telegram_chat_id)
        )
        return result.scalar_one()


async def test_failed_delivery_does_not_move_checkpoint(db):
    await _seed(500, max_id=10, checkpoint=5)
    runner = FailingRunner()

    await runner._run()

    assert await _checkpoint(500) == 5, "checkpoint advanced despite failed delivery"


async def test_successful_delivery_moves_checkpoint(db):
    await _seed(501, max_id=10, checkpoint=5)
    runner = OkRunner()

    await runner._run()

    assert await _checkpoint(501) == 10


async def test_single_mode_only_moves_checkpoint_for_delivered_chats(db):
    await _seed(502, max_id=10, checkpoint=5)
    async with get_session_factory()() as session:
        session.add(Chat(
            chat_id=503, title="t2", watching=True, user_id=42,
            max_message_id=20, last_webhook_message_id=15,
        ))
        await session.commit()

    # Payload carries the INTERNAL chat id (README: chat_id is the DB id).
    async with get_session_factory()() as session:
        rows = (await session.execute(select(Chat.chat_id, Chat.id).where(Chat.chat_id.in_([502, 503])))).all()
    internal = {tg: i for tg, i in rows}

    runner = OkRunner()
    runner.settings.mode = "single"

    async def _post_mixed(payload: dict) -> bool:
        return payload["chat_id"] == internal[502]  # only 502 delivered

    runner._post = _post_mixed
    await runner._run()

    assert await _checkpoint(502) == 10, "delivered chat must advance"
    assert await _checkpoint(503) == 15, "undelivered chat must keep its checkpoint"


async def test_empty_chats_are_not_selected(db):
    await _seed(504, watching=True, max_id=None, checkpoint=None)
    runner = OkRunner()

    async with get_session_factory()() as session:
        chats = await runner._get_chats_with_updates(session)

    assert chats == [], "chats without messages must not be selected every interval"


async def test_chats_with_new_messages_are_selected(db):
    await _seed(505, max_id=10, checkpoint=5)
    runner = OkRunner()

    async with get_session_factory()() as session:
        chats = await runner._get_chats_with_updates(session)

    assert [c.chat_id for c in chats] == [505]
