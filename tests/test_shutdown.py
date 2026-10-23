"""C3: shutdown must be observable and must stop the workers.

main() created shutdown_event and set it in a signal handler, but nothing ever
awaited it, and worker_manager.stop() was called from nowhere. The blocking
await ui_server.serve() kept the process alive forever.
"""

import asyncio

import pytest

import main as main_module


async def _sleep_task(seconds: float = 60.0) -> asyncio.Task:
    return asyncio.create_task(asyncio.sleep(seconds))


async def test_helper_exists():
    if not hasattr(main_module, "run_until_shutdown"):
        pytest.fail(
            "main.run_until_shutdown is missing — shutdown_event still unreachable "
            "and worker_manager.stop() still never called"
        )


async def test_shutdown_event_stops_services_and_runs_callbacks():
    shutdown_event = asyncio.Event()
    calls: list[str] = []

    async def cb_webhook():
        calls.append("webhook")

    async def cb_workers():
        calls.append("workers")

    services = [await _sleep_task(), await _sleep_task()]

    runner = asyncio.create_task(
        main_module.run_until_shutdown(shutdown_event, [cb_webhook, cb_workers], *services)
    )
    await asyncio.sleep(0.05)
    assert not runner.done(), "runner must keep waiting until shutdown or service exit"

    shutdown_event.set()
    await asyncio.wait_for(runner, timeout=2)

    assert calls == ["webhook", "workers"]
    for s in services:
        assert s.cancelled() or s.done(), "service tasks must be cancelled on shutdown"


async def test_service_exit_stops_everything_too():
    """uvicorn installs its own signal handlers (capture_signals), so SIGTERM
    surfaces as a service task completing — not as our shutdown_event. Workers
    must be stopped on that path as well, or the process dies with live
    Telegram connections."""
    shutdown_event = asyncio.Event()
    calls: list[str] = []

    async def cb():
        calls.append("cb")

    exiting = asyncio.create_task(asyncio.sleep(0.01))
    idle = await _sleep_task()

    runner = asyncio.create_task(
        main_module.run_until_shutdown(shutdown_event, [cb], exiting, idle)
    )
    await asyncio.wait_for(runner, timeout=2)

    assert calls == ["cb"], "callbacks must run when a service exits on its own (uvicorn SIGTERM path)"
    assert idle.cancelled() or idle.done(), "remaining services must be cancelled"


async def test_callback_error_does_not_block_cancellation():
    shutdown_event = asyncio.Event()

    async def broken():
        raise RuntimeError("stop failed")

    services = [await _sleep_task()]
    runner = asyncio.create_task(
        main_module.run_until_shutdown(shutdown_event, [broken], *services)
    )
    await asyncio.sleep(0.05)
    shutdown_event.set()

    await asyncio.wait_for(runner, timeout=2)  # must not raise
    assert services[0].cancelled() or services[0].done()
