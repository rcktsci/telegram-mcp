"""chats.in_folders: persist folder membership for token scoping.

refresh_folders() maps the fetched overview ({folder_id, chat_ids}) onto
chats rows: members keep their folder ids (overview order), every other
chat gets []. Any failure — fetcher raise or no connected client — keeps
the stored data and must not raise.
"""

import src.api.ui as ui_module
import src.telegram.folders as folders_module
from sqlalchemy import select

from src.db import Chat, get_session_factory
from src.telegram.folders import refresh_folders


async def _seed_chat(chat_id: int, in_folders=None) -> None:
    async with get_session_factory()() as session:
        session.add(Chat(chat_id=chat_id, title="t", watching=True, in_folders=in_folders or []))
        await session.commit()


async def _read_in_folders(chat_id: int) -> list:
    async with get_session_factory()() as session:
        chat = (await session.execute(select(Chat).where(Chat.chat_id == chat_id))).scalar_one()
    return chat.in_folders


async def test_refresh_writes_folder_membership(db, monkeypatch):
    await _seed_chat(1200)  # member of two folders -> both ids
    await _seed_chat(1201)  # member of one folder
    await _seed_chat(1202)  # in no folder -> []

    overview = [
        {"folder_id": 7, "title": "Work", "chat_ids": [1200, 1201]},
        {"folder_id": 9, "title": "News", "chat_ids": [1200]},
    ]

    async def fake_fetch():
        return overview

    monkeypatch.setattr(folders_module, "fetch_folders_connected", fake_fetch)

    await refresh_folders()

    assert await _read_in_folders(1200) == [7, 9]
    assert await _read_in_folders(1201) == [7]
    assert await _read_in_folders(1202) == []


async def test_refresh_keeps_data_when_fetcher_raises(db, monkeypatch):
    await _seed_chat(1210, in_folders=[5])

    async def broken_fetch():
        raise RuntimeError("rpc down")

    monkeypatch.setattr(folders_module, "fetch_folders_connected", broken_fetch)

    # No exception escapes; stored membership stays as is.
    await refresh_folders()
    assert await _read_in_folders(1210) == [5]


async def test_refresh_keeps_data_when_no_client(db, monkeypatch):
    # None means "no connected client", NOT "account has no folders":
    # wiping memberships here would silently cut every token's scope.
    await _seed_chat(1211, in_folders=[5])

    async def no_client():
        return None

    monkeypatch.setattr(folders_module, "fetch_folders_connected", no_client)

    await refresh_folders()
    assert await _read_in_folders(1211) == [5]


async def test_refresh_never_spawns_status_checker_client(db, monkeypatch):
    """F-1: the refresh path uses connected workers ONLY.

    The UI fetcher's status-checker fallback would START a second live
    client with the same session string (connect race with worker #0 on
    every service start); the refresher must be unable to reach it.
    """
    await _seed_chat(1212, in_folders=[5])

    import src.runtime as runtime_module
    import src.telegram.status as status_module

    fallback_calls = []

    class _WouldSpawn:
        async def get_first_client(self):
            fallback_calls.append(True)
            return None

    # both possible bindings: module-level (top import) and lazy (call-time)
    monkeypatch.setattr(folders_module, "get_status_checker", lambda: _WouldSpawn(), raising=False)
    monkeypatch.setattr(status_module, "get_status_checker", lambda: _WouldSpawn())
    monkeypatch.setattr(runtime_module, "get_worker_manager", lambda: None)

    await refresh_folders()

    assert fallback_calls == [], "refresh_folders must not create Telegram clients"
    assert await _read_in_folders(1212) == [5]


async def test_refresh_atomic_failure_keeps_previous_membership(db, monkeypatch):
    """F-3: the reset and the assignments are one transaction — a failure
    between statements rolls everything back and raises nothing.
    """
    await _seed_chat(1213, in_folders=[5])

    real_factory = folders_module.get_session_factory

    class _FailingOnSecondExecute:
        def __init__(self, session):
            self._session = session
            self.calls = 0

        async def __aenter__(self):
            # the body must talk to the PROXY; returning the inner session
            # lets every statement bypass the interceptor unnoticed
            await self._session.__aenter__()
            return self

        async def __aexit__(self, *exc):
            return await self._session.__aexit__(*exc)

        async def execute(self, *a, **kw):
            self.calls += 1
            if self.calls == 2:  # the membership UPDATE, after the reset
                raise RuntimeError("db died mid-refresh")
            return await self._session.execute(*a, **kw)

        def __getattr__(self, name):
            return getattr(self._session, name)

    def failing_factory():
        # the factory must return a CALLABLE that builds the session proxy:
        # refresh_folders does `async with get_session_factory()() as ...`
        return lambda: _FailingOnSecondExecute(real_factory()())

    monkeypatch.setattr(folders_module, "get_session_factory", failing_factory)

    async def fake_fetch():
        return [{"folder_id": 8, "title": "X", "chat_ids": [1213]}]

    # the refresh path's fetcher (not the UI one): otherwise no worker ->
    # early return and the DB failure is never modeled
    monkeypatch.setattr(folders_module, "fetch_folders_connected", fake_fetch)

    # no exception escapes
    await refresh_folders()

    # the reset itself rolled back with the failed assignment
    assert await _read_in_folders(1213) == [5]


async def test_folders_route_uses_moved_fetcher(monkeypatch):
    # The /api/folders route must serve through the moved fetcher;
    # the local _fetch_folders helper is gone from ui.py.
    assert not hasattr(ui_module, "_fetch_folders")
    assert hasattr(ui_module, "fetch_folders")

    async def fake_fetch():
        return [{"folder_id": 1, "title": "F", "chat_ids": []}]

    monkeypatch.setattr(ui_module, "fetch_folders", fake_fetch)
    monkeypatch.setattr(ui_module, "_folders_cache", {})

    assert await ui_module.get_folders() == [{"folder_id": 1, "title": "F", "chat_ids": []}]


class _FakeSweepListener:
    async def sweep_history(self, chat_id):
        return None


async def test_sweep_pass_refreshes_folders(db, monkeypatch):
    # The hourly refresh hooks into the sweep pass (one overview per pass).
    from src.worker.manager import WorkerManager

    calls = []

    async def fake_refresh():
        calls.append(True)

    monkeypatch.setattr("src.worker.manager.refresh_folders", fake_refresh)

    manager = WorkerManager()
    total, aborted = await manager._sweep_all_watching_chats(_FakeSweepListener())

    assert (total, aborted) == (0, 0)
    assert calls == [True]
