"""Resumable bottom-up history sweep (PLAN_HISTORY_VERIFY_FILL §5.2).

One pass walks a chat from its stored checkpoint (or from the very bottom when
the checkpoint is NULL) up to the top. Every page is saved immediately, then the
checkpoint advances monotonically. The only terminal is a clean empty page:
a page error aborts the pass without touching the checkpoint, so the next pass
resumes exactly where this one stopped.
"""

import asyncio
import logging
from typing import TYPE_CHECKING, Awaitable, Callable, Optional

from src.db import ChatRepository, get_session_factory

if TYPE_CHECKING:
    from src.telegram.client import TelegramMessage

logger = logging.getLogger(__name__)


async def sweep_chat(
    chat_id: int,
    fetch_page: Callable[[Optional[int]], Awaitable[list["TelegramMessage"]]],
    save_batch: Callable[[int, list["TelegramMessage"]], Awaitable[int]],
    pause_seconds: float = 0.5,
) -> None:
    """Sweep one chat bottom-up. Returns normally once the top was reached.

    ``fetch_page`` must raise on RPC errors (it is the non-swallowing path):
    that exception propagates and aborts the pass with the checkpoint intact.
    """
    async with get_session_factory()() as session:
        offset = await ChatRepository(session).get_history_sweep_upto(chat_id)

    logger.info(f"History sweep for chat {chat_id}: starting from id={offset}")
    pages = 0
    saved_total = 0

    while True:
        batch = await fetch_page(offset)  # raises on failure -> ABORT, no marker move
        if not batch:
            logger.info(
                f"History sweep for chat {chat_id}: top reached after "
                f"{pages} pages ({saved_total} new messages)"
            )
            return

        last_id = batch[-1].message_id
        saved_total += await save_batch(chat_id, batch)

        async with get_session_factory()() as session:
            await ChatRepository(session).update_history_sweep_upto(chat_id, last_id)

        pages += 1
        logger.info(f"History sweep for chat {chat_id}: saved page up to id={last_id}")
        offset = last_id

        if pause_seconds:
            await asyncio.sleep(pause_seconds)
