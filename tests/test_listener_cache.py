"""M9: listener cache must not stick.

_get_listener_for_chat builds _user_id_to_listener once. If every session fails
at startup the map stays {} (not None), so the "uninitialized" guard never fires
again and get_media_link/get_chat_members stay dead until process restart.
"""

import pytest

from src.db import Chat, get_session_factory
from src.mcp import tools as mcp_tools


class FakeClient:
    def __init__(self, user_id: int):
        self._user_id = user_id

    async def get_me(self):
        class Me:
            pass
        me = Me()
        me.id = self._user_id
        me.username = "u"
        return me

    def is_connected(self) -> bool:
        return True

    async def disconnect(self):
        pass


class FakeListener:
    #: set by the fixture: None => start() raises, int => ready client
    behavior = None
    builds = 0

    def __init__(self, *a, **kw):
        pass

    async def start(self):
        FakeListener.builds += 1
        if FakeListener.behavior is None:
            raise RuntimeError("tg down")
        self._client = FakeClient(FakeListener.behavior)

    async def wait_ready(self, timeout: float = 120.0) -> bool:
        return FakeListener.behavior is not None

    async def reconnect(self):
        pass


@pytest.fixture()
async def fake_tools_env(db, monkeypatch):
    import src.config as config_mod

    FakeListener.builds = 0
    FakeListener.behavior = None
    mcp_tools._user_id_to_listener = None

    class S:
        session_string = "s1"

    class TG:
        api_id = 1
        api_hash = "h"
        sessions = [S()]

    class Settings:
        telegram = TG()

    monkeypatch.setattr(config_mod, "get_settings", lambda: Settings())
    monkeypatch.setattr(mcp_tools, "TelegramListener", FakeListener)

    async with get_session_factory()() as session:
        session.add(Chat(chat_id=700, title="t", watching=True, user_id=42))
        await session.commit()

    yield FakeListener

    mcp_tools._user_id_to_listener = None


async def test_failed_build_is_retried_on_next_call(fake_tools_env):
    listener = await mcp_tools._get_listener_for_chat(700)
    assert listener is None, "no session available yet"
    assert fake_tools_env.builds == 1

    fake_tools_env.behavior = 42  # telegram recovered

    listener = await mcp_tools._get_listener_for_chat(700)
    assert listener is not None, "empty cache must be rebuilt on the next call"
    assert fake_tools_env.builds == 2


async def test_successful_build_is_cached(fake_tools_env):
    fake_tools_env.behavior = 42

    first = await mcp_tools._get_listener_for_chat(700)
    assert first is not None
    second = await mcp_tools._get_listener_for_chat(700)
    assert second is first
    assert fake_tools_env.builds == 1, "healthy cache must not be rebuilt"
