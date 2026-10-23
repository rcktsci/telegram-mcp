import asyncio
import logging
from typing import Optional

from src.config import get_settings
from src.db import ChatRepository, get_session_factory
from src.telegram.folders import refresh_folders
from src.telegram.worker import TelegramWorker

logger = logging.getLogger(__name__)

# Pause between chats within one sweep pass: keeps the RPC rate low enough
# that Telegram's FloodWait rarely triggers even on large backlogs.
SWEEP_CHAT_PAUSE_SECONDS = 2

# Short backoff when a whole pass aborted (link down, FloodWait storm):
# retry soon instead of silently idling for the full interval. The delay
# doubles on every consecutive all-aborted pass up to the cap, so a persistent
# outage does not hammer the API (anti-FloodWait spirit of plan §8).
SWEEP_RETRY_DELAY_SECONDS = 5 * 60
SWEEP_RETRY_DELAY_MAX_SECONDS = 60 * 60


class WorkerManager:
    def __init__(self):
        self.settings = get_settings()
        self._workers: list[TelegramWorker] = []
        self._running = False
        self._sweep_task: Optional[asyncio.Task] = None
        self._sweep_now_event = asyncio.Event()

    async def start(self):
        settings = get_settings()
        
        if not settings.telegram.api_id or not settings.telegram.api_hash:
            logger.warning("Telegram API credentials not configured")
            return
        
        for session in settings.telegram.sessions:
            if not session.session_string:
                logger.warning("Skipping empty session")
                continue
            
            worker = TelegramWorker(
                session_string=session.session_string,
                api_id=settings.telegram.api_id,
                api_hash=settings.telegram.api_hash,
                poll_interval=settings.worker.poll_interval_seconds,
                mtproxy_host=settings.telegram.mtproxy_host,
                mtproxy_port=settings.telegram.mtproxy_port,
                mtproxy_secret=settings.telegram.mtproxy_secret,
            )
            self._workers.append(worker)
            
            asyncio.create_task(worker.run())
            logger.info(f"Started worker for session")
        
        self._running = True
        self._sweep_task = asyncio.create_task(self._history_sweep_loop())

    async def stop(self):
        self._running = False
        if self._sweep_task is not None:
            self._sweep_task.cancel()
            try:
                await self._sweep_task
            except asyncio.CancelledError:
                pass
            except Exception as e:
                # A broken sweep task must not prevent workers from stopping.
                logger.warning(f"History sweep task failed during stop: {e}")
            self._sweep_task = None
        for worker in self._workers:
            await worker.stop()
        logger.info("All workers stopped")

    def request_sweep_now(self) -> None:
        """Ask for an immediate sweep pass (idempotent, no side effects).

        If a pass is already running, the next one starts right after it; the
        event is consumed by the loop, so repeated clicks never pile up passes.
        """
        self._sweep_now_event.set()

    async def _history_sweep_loop(self):
        """Sweep all watching chats bottom-up, one chat at a time.

        A pass fires on schedule, every history_sweep.interval_hours, or
        immediately on a manual request (request_sweep_now). The schedule wait
        also keeps the first pass clear of the startup catch-up's RPC budget.
        A fully aborted pass retries sooner with a growing backoff (capped);
        any partial or clean pass resets it.
        """
        interval = max(0.01, float(get_settings().history_sweep.interval_hours)) * 3600
        retry_delay = SWEEP_RETRY_DELAY_SECONDS
        wait = interval
        while self._running:
            # Leading wait: a pass fires on schedule (N hours after deploy)
            # or at once when woken manually. After a fully aborted pass
            # `wait` holds the retry backoff instead.
            try:
                await asyncio.wait_for(self._sweep_now_event.wait(), timeout=wait)
                self._sweep_now_event.clear()
                logger.info("History sweep triggered manually")
            except asyncio.TimeoutError:
                pass

            listener = self.get_connected_listener()
            if listener is None:
                await asyncio.sleep(5)
                continue
            if not await listener.wait_ready(timeout=60):
                # Client object exists but is still handshaking; retry shortly.
                await asyncio.sleep(5)
                continue

            try:
                total, aborted = await self._sweep_all_watching_chats(listener)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"History sweep pass failed: {e}")
                total, aborted = 0, 0

            if total > 0 and aborted == total:
                logger.warning(
                    f"History sweep pass aborted for all {total} chats; "
                    f"retrying in {retry_delay}s"
                )
                wait = retry_delay
                retry_delay = min(retry_delay * 2, SWEEP_RETRY_DELAY_MAX_SECONDS)
            else:
                # Partial or clean pass: reset the backoff, next pass on schedule.
                retry_delay = SWEEP_RETRY_DELAY_SECONDS
                wait = interval

    async def _sweep_all_watching_chats(self, listener) -> tuple[int, int]:
        """Sweep every watching chat once. Returns (total, aborted) chats."""
        # Folder membership feeds token scoping: one overview refresh per
        # pass, best-effort (refresh_folders logs failures, keeps old data).
        await refresh_folders()
        user_id = next((w.user_id for w in self._workers if w.get_listener() is listener), None)
        async with get_session_factory()() as session:
            chats = await ChatRepository(session).get_watching_chats(user_id)
        logger.info(f"History sweep pass over {len(chats)} watching chats")

        aborted = 0
        for index, chat in enumerate(chats):
            if not self._running:
                return len(chats), aborted
            try:
                await listener.sweep_history(chat.chat_id)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                # ABORT for this chat only; its checkpoint stays for the next pass.
                logger.warning(f"History sweep aborted for chat {chat.chat_id}: {e}")
                aborted += 1
            if index < len(chats) - 1:
                await asyncio.sleep(SWEEP_CHAT_PAUSE_SECONDS)
        return len(chats), aborted

    @property
    def is_running(self) -> bool:
        return self._running

    def get_sessions_status(self) -> list[dict]:
        return [
            {
                "session": w.session_string[-4:] if w.session_string else "none",
                "connected": w.is_connected(),
                "user_id": w.user_id
            }
            for w in self._workers
        ]

    def pick_user_id(self) -> Optional[int]:
        """Pick an owning account: any connected worker with known user_id first."""
        for worker in self._workers:
            if worker.is_connected() and worker.user_id:
                return worker.user_id
        for worker in self._workers:
            if worker.user_id:
                return worker.user_id
        return None

    def get_connected_listener(self):
        """Listener of the first connected worker, for on-demand RPC (folders)."""
        for worker in self._workers:
            listener = worker.get_listener()
            if worker.is_connected() and listener is not None:
                return listener
        return None

    async def load_history(self, chat_id: int, user_id: int):
        """Trigger history load for a specific chat."""
        for worker in self._workers:
            if worker.is_connected() and worker.user_id == user_id:
                logger.info(f"Loading history for chat {chat_id} on worker {worker.session_string[-4:]}")
                await worker.load_history_for_chat(chat_id)
                return
        logger.warning(f"No connected worker found to load history for chat {chat_id}")
