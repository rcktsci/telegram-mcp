"""H: reconnect() must disconnect the previous TelegramClient.

The old reconnect() just dropped the reference (self._client = None): the old
connection stayed open (leak) and its background task kept running.
"""

import asyncio

import pytest

from src.telegram.client import TelegramListener


class OldClient:
    def __init__(self):
        self.disconnect_calls = 0

    async def disconnect(self):
        self.disconnect_calls += 1


async def test_reconnect_disconnects_previous_client():
    listener = TelegramListener("test-session", 1, "h")
    old_client = OldClient()
    listener._client = old_client
    listener._running = True

    async def fake_start():
        listener._ready = asyncio.Event()
        listener._ready.set()
        listener._history_loaded = asyncio.Event()
        listener._topic_names = {}
        listener._client = OldClient()  # "new" client

    listener.start = fake_start

    await listener.reconnect()

    assert old_client.disconnect_calls == 1, "old client must be disconnected, not just dropped"
    assert listener._client is not old_client
