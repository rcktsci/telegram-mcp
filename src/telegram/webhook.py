import asyncio
import logging
from typing import Optional

import aiohttp
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import get_settings
from src.db.models import Chat
from src.db.connection import get_session_factory


logger = logging.getLogger(__name__)


class WebhookRunner:
    def __init__(self):
        self.settings = get_settings().webhook
        self._last_run: Optional[float] = None
        self._running = False
        self._session: Optional[aiohttp.ClientSession] = None

    async def start(self):
        if not self.settings.enabled:
            logger.info("Webhook disabled")
            return

        if not self.settings.url:
            logger.warning("Webhook URL not configured")
            return

        logger.info(f"Starting webhook runner: {self.settings.url} (mode={self.settings.mode}, interval={self.settings.interval_seconds}s)")
        self._running = True
        asyncio.create_task(self._run_loop())

    async def stop(self):
        self._running = False
        if self._session:
            await self._session.close()
            self._session = None

    async def _run_loop(self):
        while self._running:
            try:
                await self._run()
            except Exception as e:
                logger.error(f"Webhook error: {e}")
            
            await asyncio.sleep(self.settings.interval_seconds)

    async def _run(self):
        if self._last_run and (asyncio.get_event_loop().time() - self._last_run) < self.settings.interval_seconds:
            return

        self._last_run = asyncio.get_event_loop().time()

        async with get_session_factory()() as session:
            chats = await self._get_chats_with_updates(session)

            if not chats:
                return

            if self.settings.mode == "batch":
                delivered = await self._send_batch(chats)
                if not delivered:
                    logger.warning("Batch webhook delivery failed; checkpoints kept")
                    return
            else:
                delivered_ids = await self._send_single(chats)
                chats = [c for c in chats if c.id in delivered_ids]
                if not chats:
                    logger.warning("No chat delivered in single mode; checkpoints kept")
                    return

            await self._update_last_message_ids(session, chats)

    async def _get_chats_with_updates(self, session: AsyncSession):
        result = await session.execute(
            select(Chat)
            .where(Chat.watching == True)
            .where(Chat.max_message_id.isnot(None))
            .where(Chat.last_webhook_message_id.is_(None))
        )
        chats_without_checkpoint = result.scalars().all()

        result = await session.execute(
            select(Chat)
            .where(Chat.watching == True)
            .where(Chat.last_webhook_message_id.is_not(None))
            .where(Chat.max_message_id > Chat.last_webhook_message_id)
        )
        chats_with_updates = result.scalars().all()

        return chats_without_checkpoint + chats_with_updates

    async def _send_batch(self, chats: list[Chat]) -> bool:
        payload = {
            "chats": [
                {
                    "chat_id": chat.id,
                    "next_unread_message_id": (chat.last_webhook_message_id or 0) + 1
                }
                for chat in chats
            ]
        }

        return await self._post(payload)

    async def _send_single(self, chats: list[Chat]) -> set:
        """Deliver per-chat payloads; return ids of chats delivered successfully."""
        delivered: set = set()
        for chat in chats:
            payload = {
                "chat_id": chat.id,
                "next_unread_message_id": (chat.last_webhook_message_id or 0) + 1
            }
            if await self._post(payload):
                delivered.add(chat.id)
        return delivered

    async def _post(self, payload: dict) -> bool:
        if self._session is None:
            self._session = aiohttp.ClientSession()
        try:
            async with self._session.post(self.settings.url, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status < 400:
                    logger.info(f"Webhook sent successfully: {payload}")
                    return True
                logger.warning(f"Webhook failed: {resp.status}")
                return False
        except Exception as e:
            logger.error(f"Webhook request failed: {e}")
            return False

    async def _update_last_message_ids(self, session: AsyncSession, chats: list[Chat]):
        try:
            for chat in chats:
                if chat.max_message_id:
                    await session.execute(
                        update(Chat)
                        .where(Chat.id == chat.id)
                        .values(last_webhook_message_id=chat.max_message_id)
                    )
            await session.commit()
            logger.info(f"Webhook checkpoint updated for {len(chats)} chats")
        except Exception as e:
            logger.error(f"Failed to commit webhook checkpoint: {e}")
            await session.rollback()
            raise
