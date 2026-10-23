"""Marked/bare chat-ID unification.

chats.chat_id stores Telethon Dialog.id — the MARKED form (utils.get_peer_id:
-100… for channels, negative for basic groups, raw for users). History rows
inherit that marked id. The realtime handler used the bare entity id
(event.get_chat().id) instead, so chat lookups failed for channels/groups:
live messages and edits were silently dropped.
"""

from datetime import datetime, timezone

from sqlalchemy import select

from src.db import Chat, MessageRepository, get_session_factory
from src.telegram.client import TelegramMessage
from src.telegram.worker import TelegramWorker


def _seed_chat(chat_id: int, user_id: int):
    return Chat(chat_id=chat_id, title="t", watching=True, user_id=user_id)


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


async def test_realtime_message_with_marked_id_is_saved(db):
    async with get_session_factory()() as session:
        session.add(_seed_chat(-1001234567890, user_id=42))
        await session.commit()

    worker = TelegramWorker(session_string="test", api_id=1, api_hash="h")
    worker.user_id = 42
    await worker._process_message(_msg(-1001234567890, 7))

    async with get_session_factory()() as session:
        repo = MessageRepository(session)
        found = await repo.get_by_chat_and_message_id(-1001234567890, 7)
    assert found is not None


def test_marked_chat_id_helper_normalizes_entities():
    from telethon import types, utils

    from src.telegram.client import marked_chat_id

    channel = types.PeerChannel(channel_id=777)
    group = types.PeerChat(chat_id=555)
    user = types.PeerUser(user_id=42)

    assert marked_chat_id(channel) == utils.get_peer_id(channel)  # canonical marked form
    assert marked_chat_id(channel) != 777  # not bare
    assert marked_chat_id(group) != 555
    assert marked_chat_id(user) == 42  # user ids are identical in both forms


def test_telethon_dialog_ids_are_marked():
    """Documents the Telethon-1.45 wheel behavior the fix relies on."""
    from telethon import types, utils

    assert utils.get_peer_id(types.PeerChannel(channel_id=777)) != 777
    assert utils.get_peer_id(types.PeerChat(chat_id=555)) != 555
    assert utils.get_peer_id(types.PeerUser(user_id=42)) == 42
