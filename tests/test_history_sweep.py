"""History sweep: resumable bottom-up fill guarantee.

Critical invariants (PLAN_HISTORY_VERIFY_FILL §5.2):
- only the sweep moves history_sweep_upto, and only forward (GREATEST);
- a page error aborts the pass: no checkpoint move and nothing saved beyond
  the pages that already succeeded;
- the only terminal is a clean empty page;
- resume starts exactly at the checkpoint;
- pages are upserted batch by batch, not collected until the end.
"""

import asyncio
import contextlib
import types
from datetime import datetime, timezone
from collections import deque

import pytest
from sqlalchemy import select

from src.db import Chat, ChatRepository, Message, get_session_factory
from src.telegram.client import TelegramListener, TelegramMessage
from src.telegram.sweep import sweep_chat
from src.worker.manager import WorkerManager


def _msg(chat_id: int, message_id: int) -> TelegramMessage:
    return TelegramMessage(
        chat_id=chat_id,
        message_id=message_id,
        sender_id=1,
        sender_username="u",
        sender_name="User",
        text=f"m{message_id}",
        date=datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc),
        raw={"message_id": message_id},
    )


async def _seed_chat(chat_id: int, sweep_upto: int | None = None) -> None:
    async with get_session_factory()() as session:
        session.add(Chat(chat_id=chat_id, title="t", watching=True, history_sweep_upto=sweep_upto))
        await session.commit()


async def _chat(chat_id: int) -> Chat:
    async with get_session_factory()() as session:
        result = await session.execute(select(Chat).where(Chat.chat_id == chat_id))
        return result.scalar_one()


async def _message_ids(chat_id: int) -> list[int]:
    async with get_session_factory()() as session:
        result = await session.execute(
            select(Message.message_id)
            .where(Message.chat_id == chat_id)
            .order_by(Message.message_id)
        )
        return [row[0] for row in result.all()]


class _Pages:
    """Fake page source: offset -> page, with an optional failing call."""

    def __init__(self, pages: dict, fail_on_call: int | None = None, on_call=None):
        self.pages = pages
        self.fail_on_call = fail_on_call
        self.on_call = on_call
        self.calls: list[int | None] = []

    async def __call__(self, offset):
        self.calls.append(offset)
        if self.on_call is not None:
            await self.on_call(offset)
        if self.fail_on_call is not None and len(self.calls) == self.fail_on_call:
            raise RuntimeError("RPC failed")
        return self.pages.get(offset, [])


def _listener() -> TelegramListener:
    # No _client needed: save_history_batch only touches the DB.
    return TelegramListener(session_string="test", api_id=1, api_hash="h")


async def test_sweep_walks_to_top_and_moves_checkpoint(db):
    await _seed_chat(700)
    pages = _Pages({
        None: [_msg(700, i) for i in (1, 2, 3)],
        3: [_msg(700, i) for i in (4, 5, 6)],
        6: [],
    })

    await sweep_chat(700, pages, _listener().save_history_batch, pause_seconds=0)

    assert pages.calls == [None, 3, 6]
    assert await _message_ids(700) == [1, 2, 3, 4, 5, 6]
    chat = await _chat(700)
    assert chat.history_sweep_upto == 6
    assert (chat.min_message_id, chat.max_message_id) == (1, 6)


async def test_page_error_aborts_without_moving_checkpoint_and_resumes(db):
    await _seed_chat(701)
    broken = _Pages({None: [_msg(701, i) for i in (1, 2, 3)]}, fail_on_call=2)

    with pytest.raises(RuntimeError):
        await sweep_chat(701, broken, _listener().save_history_batch, pause_seconds=0)

    assert broken.calls == [None, 3]
    assert await _message_ids(701) == [1, 2, 3]
    assert (await _chat(701)).history_sweep_upto == 3, "failed page must not close the range"

    resumed = _Pages({3: [_msg(701, 4), _msg(701, 5)], 5: []})
    await sweep_chat(701, resumed, _listener().save_history_batch, pause_seconds=0)

    assert resumed.calls[0] == 3, "resume must start exactly at the checkpoint"
    assert await _message_ids(701) == [1, 2, 3, 4, 5]
    assert (await _chat(701)).history_sweep_upto == 5


async def test_each_page_is_saved_before_the_next_fetch(db):
    await _seed_chat(702)
    seen: list[tuple] = []

    async def probe(offset):
        seen.append((offset, await _message_ids(702)))

    pages = _Pages(
        {None: [_msg(702, i) for i in (1, 2)], 2: [_msg(702, 3)], 3: []},
        on_call=probe,
    )
    await sweep_chat(702, pages, _listener().save_history_batch, pause_seconds=0)

    assert seen[0] == (None, [])
    assert seen[1] == (2, [1, 2]), "first page must be in the DB before the second fetch"


async def test_checkpoint_is_monotonic_and_only_sweep_writes_it(db):
    await _seed_chat(703, sweep_upto=100)

    async with get_session_factory()() as session:
        repo = ChatRepository(session)
        await repo.update_history_sweep_upto(703, 50)
        # Listener/loader path: must not touch the sweep checkpoint.
        await repo.update_message_bounds(703, 1, 200, None, None)

    assert (await _chat(703)).history_sweep_upto == 100, "GREATEST must ignore a lower value"

    pages = _Pages({100: [_msg(703, 101), _msg(703, 102)], 102: []})
    await sweep_chat(703, pages, _listener().save_history_batch, pause_seconds=0)

    assert pages.calls[0] == 100
    assert (await _chat(703)).history_sweep_upto == 102


async def test_clean_empty_page_is_the_only_terminal(db):
    await _seed_chat(704)
    pages = _Pages({None: []})

    await sweep_chat(704, pages, _listener().save_history_batch, pause_seconds=0)
    assert (await _chat(704)).history_sweep_upto is None
    assert await _message_ids(704) == []


async def test_pagination_loader_saves_each_page(db):
    await _seed_chat(705)
    pages = {None: [_msg(705, 1), _msg(705, 2)], 2: [_msg(705, 3)], 3: []}
    listener = _listener()

    async def fake_get_history(chat_id, limit=1000, min_id=None):
        return pages.get(min_id, [])

    listener.get_history = fake_get_history
    saved_pages = []

    async def on_batch(batch):
        saved_pages.append([m.message_id for m in batch])
        await listener.save_history_batch(705, batch)

    total = await listener._load_history_with_pagination(705, on_batch=on_batch)

    assert total == 3
    assert saved_pages == [[1, 2], [3]]
    assert await _message_ids(705) == [1, 2, 3]


class _RawMessage:
    """Bare stand-in for a Telethon message (enough for _build_message)."""

    def __init__(self, message_id: int):
        self.id = message_id
        self.text = f"m{message_id}"
        self.date = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)

    def to_dict(self):
        return {"message_id": self.id, "text": self.text}

    async def get_sender(self):
        return None


class _FakePageClient:
    """get_entity + iter_messages recording kwargs; no network.

    Each get_history_page call consumes the next page from the queue.
    """

    def __init__(self, pages: list[list[int]]):
        self.remaining = deque(pages)
        self.calls: list[dict] = []

    async def get_entity(self, chat_id):
        return "entity"

    def iter_messages(self, entity, **kwargs):
        self.calls.append(kwargs)

        async def _gen():
            if self.remaining:
                for message_id in self.remaining.popleft():
                    yield _RawMessage(message_id)

        return _gen()


class _BrokenClient:
    async def get_entity(self, chat_id):
        return "entity"

    def iter_messages(self, entity, **kwargs):
        async def _gen():
            raise RuntimeError("rpc down")
            yield  # pragma: no cover

        return _gen()


async def test_get_history_page_propagates_iter_error(db):
    # B1 invariant: the sweep's page path must NOT swallow RPC errors into an
    # empty list — an empty page is the terminal, so a hidden error would fake it.
    listener = _listener()
    listener._client = _BrokenClient()

    with pytest.raises(RuntimeError):
        await listener.get_history_page(710, offset_id=5)


async def test_get_history_page_walks_reverse_with_offset_chain(db):
    listener = _listener()
    client = _FakePageClient(pages=[[1, 2], [3]])
    listener._client = client

    page1 = await listener.get_history_page(711, offset_id=None)
    page2 = await listener.get_history_page(711, offset_id=page1[-1].message_id)

    assert [m.message_id for m in page1] == [1, 2]
    assert [m.message_id for m in page2] == [3]


class _FakeReadyListener:
    """Enough of a listener for the sweep loop itself (no network, no DB)."""

    async def wait_ready(self, timeout: float = 120.0) -> bool:
        return True


async def test_sweep_loop_wakes_on_request_sweep_now(monkeypatch):
    # Timer is a full hour: a pass can only happen through the manual wake.
    cfg = types.SimpleNamespace(history_sweep=types.SimpleNamespace(interval_hours=1.0))
    monkeypatch.setattr("src.worker.manager.get_settings", lambda: cfg)

    manager = WorkerManager()
    manager._running = True
    manager.get_connected_listener = lambda: _FakeReadyListener()

    loop_time = asyncio.get_running_loop().time
    passes: list[float] = []

    async def fake_pass(listener):
        passes.append(loop_time())
        return 1, 0

    manager._sweep_all_watching_chats = fake_pass

    task = asyncio.create_task(manager._history_sweep_loop())
    try:
        await asyncio.sleep(0.2)
        assert passes == [], "no pass before the manual request"

        manager.request_sweep_now()
        requested_at = loop_time()
        for _ in range(100):
            await asyncio.sleep(0.05)
            if passes:
                break
        assert passes, "manual request must wake the loop"
        assert passes[0] - requested_at < 1.0, "pass must start now, not on the next tick"

        assert not manager._sweep_now_event.is_set(), "event must be consumed by the pass"

        count = len(passes)
        manager.request_sweep_now()
        for _ in range(100):
            await asyncio.sleep(0.05)
            if len(passes) > count:
                break
        assert len(passes) > count, "a fresh request must trigger another pass"
    finally:
        manager._running = False
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

