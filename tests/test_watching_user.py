"""H3/H4: chat ownership (user_id).

- upsert_chats must stamp new chats with the owning account's user_id and must
  NOT reassign user_id on conflict (multi-account: first worker would steal).
- PATCH /api/chats/{id}/watching on a chat without owner must bind an owner
  and trigger history loading; unknown chat must be 404, not silent 200.
"""

import pytest
from sqlalchemy import select

from src.db import Chat, ChatRepository, get_session_factory
from src.api.ui import ui_app


async def _row(telegram_chat_id: int) -> Chat | None:
    async with get_session_factory()() as session:
        result = await session.execute(select(Chat).where(Chat.chat_id == telegram_chat_id))
        return result.scalar_one_or_none()


async def test_upsert_stamps_user_id_on_new_chats(db):
    async with get_session_factory()() as session:
        repo = ChatRepository(session)
        try:
            await repo.upsert_chats(
                [{"chat_id": 600, "title": "a", "is_group": True, "is_private": False}],
                user_id=42,
            )
        except TypeError as e:
            pytest.fail(f"upsert_chats must accept user_id: {e}")

    chat = await _row(600)
    assert chat is not None
    assert chat.user_id == 42


async def test_upsert_preserves_owner_on_conflict(db):
    async with get_session_factory()() as session:
        repo = ChatRepository(session)
        await repo.upsert_chats(
            [{"chat_id": 601, "title": "a", "is_group": False, "is_private": False}],
            user_id=42,
        )
        # Second account syncs the same shared chat: owner must not be stolen.
        await repo.upsert_chats(
            [{"chat_id": 601, "title": "a2", "is_group": False, "is_private": False}],
            user_id=777,
        )

    chat = await _row(601)
    assert chat.user_id == 42
    assert chat.title == "a2"  # metadata still refreshed


class _FakeManager:
    def __init__(self, user_id):
        self._user_id = user_id
        self.load_calls: list[tuple[int, int]] = []

    def pick_user_id(self) -> int | None:
        return self._user_id

    async def load_history(self, chat_id: int, user_id: int):
        self.load_calls.append((chat_id, user_id))


async def _patch_watching(internal_id: int, watching: bool, manager):
    import httpx
    import src.runtime as runtime

    # ui.py resolves the manager via src.runtime (NOT main) so that UI toggles
    # keep working when main.py runs as __main__ (from main import would
    # re-execute it into a namespace where the manager is still None).
    original = runtime.get_worker_manager
    runtime.get_worker_manager = lambda: manager
    try:
        transport = httpx.ASGITransport(app=ui_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.patch(f"/api/chats/{internal_id}/watching", json={"watching": watching})
        return resp
    finally:
        runtime.get_worker_manager = original


async def test_patch_watching_binds_owner_and_triggers_history(db):
    async with get_session_factory()() as session:
        session.add(Chat(chat_id=610, title="t", watching=False, user_id=None))
        await session.commit()
    internal = (await _row(610)).id

    manager = _FakeManager(user_id=42)
    resp = await _patch_watching(internal, watching=True, manager=manager)

    assert resp.status_code == 200
    chat = await _row(610)
    assert chat.watching is True
    assert chat.user_id == 42, "ownerless chat must be bound to an available account"
    assert manager.load_calls == [(610, 42)], "history loading must be triggered"


async def test_patch_watching_unknown_chat_returns_404(db):
    manager = _FakeManager(user_id=42)
    resp = await _patch_watching(99999, watching=True, manager=manager)
    assert resp.status_code == 404


async def test_patch_watching_with_owner_keeps_owner(db):
    async with get_session_factory()() as session:
        session.add(Chat(chat_id=611, title="t", watching=False, user_id=42))
        await session.commit()
    internal = (await _row(611)).id

    manager = _FakeManager(user_id=777)  # another account picked first
    resp = await _patch_watching(internal, watching=True, manager=manager)

    assert resp.status_code == 200
    chat = await _row(611)
    assert chat.user_id == 42, "existing owner must not be reassigned"
    assert manager.load_calls == [(611, 42)]
