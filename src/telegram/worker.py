import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional

from src.db import get_session_factory, MessageRepository, ChatRepository
from src.telegram.client import TelegramListener

logger = logging.getLogger(__name__)


class TelegramWorker:
    def __init__(
        self,
        session_string: str,
        api_id: int,
        api_hash: str,
        poll_interval: int = 5,
        mtproxy_host: Optional[str] = None,
        mtproxy_port: int = 443,
        mtproxy_secret: str = "",
    ):
        self.session_string = session_string
        self.api_id = api_id
        self.api_hash = api_hash
        self.user_id: Optional[int] = None
        self.poll_interval = poll_interval
        self._running = False
        self._listener: Optional[TelegramListener] = None
        self._mtproxy_host = mtproxy_host
        self._mtproxy_port = mtproxy_port
        self._mtproxy_secret = mtproxy_secret

    def is_connected(self) -> bool:
        return self._listener is not None and self._listener._client is not None

    def get_listener(self) -> Optional[TelegramListener]:
        """Live listener (None while the worker has not started yet)."""
        return self._listener

    async def start(self):
        self._listener = TelegramListener(
            self.session_string,
            self.api_id,
            self.api_hash,
            self._mtproxy_host,
            self._mtproxy_port,
            self._mtproxy_secret,
        )
        await self._listener.start()
        await self._listener.wait_ready()
        me = await self._listener._client.get_me()
        self.user_id = me.id
        self._running = True
        logger.info(f"Worker started for user {me.username or me.id} (user_id: {self.user_id})")

    async def stop(self):
        self._running = False
        if self._listener:
            await self._listener.stop()
        logger.info(f"Worker stopped for session ending in {self.session_string[-4:]}")

    async def load_history_for_chat(self, chat_id: int, full_history: bool = False):
        """Load history for a specific chat (triggered when watching is enabled)."""
        if not self._listener:
            logger.warning("Cannot load history: listener not initialized")
            return
        if not self.user_id:
            logger.warning("Cannot load history: user_id not set")
            return
        await self._listener.load_history_for_chat(chat_id, self.user_id, full_history)

    async def run(self):
        await self.start()
        
        await self._bind_chats_to_user()
        
        logger.info("Worker entering main message loop")
        
        sync_interval = 4 * 60 * 60  # 4 hours
        last_sync = datetime.now(timezone.utc)
        
        while self._running:
            try:
                async for message in self._listener.listen():
                    await self._process_message(message)
                    
                    if (datetime.now(timezone.utc) - last_sync).total_seconds() >= sync_interval:
                        await self._sync_dialogs()
                        last_sync = datetime.now(timezone.utc)
            except asyncio.CancelledError:
                logger.info("Worker cancelled, exiting gracefully")
                break
            except Exception as e:
                logger.error(f"Error in worker: {e}")
                await asyncio.sleep(self.poll_interval)

    async def _sync_dialogs(self):
        if not self._listener:
            return
        
        try:
            dialogs = await self._listener.get_dialogs_list()
            if dialogs:
                session_factory = get_session_factory()
                async with session_factory() as session:
                    chat_repo = ChatRepository(session)
                    await chat_repo.upsert_chats(dialogs, user_id=self.user_id)
                    logger.info(f"Synced {len(dialogs)} dialogs to database")
        except Exception as e:
            logger.error(f"Failed to sync dialogs: {e}")

    async def _bind_chats_to_user(self):
        """Привязать чаты без user_id к текущему пользователю."""
        if not self.user_id:
            logger.warning("Cannot bind chats: user_id not set")
            return
        from sqlalchemy import text
        session_factory = get_session_factory()
        async with session_factory() as session:
            total_bound = 0
            while True:
                result = await session.execute(text("""
                    UPDATE chats 
                    SET user_id = :user_id, updated_at = NOW()
                    WHERE id IN (
                        SELECT id FROM chats 
                        WHERE watching = TRUE AND user_id IS NULL
                        LIMIT 500
                    )
                """), {"user_id": self.user_id})
                await session.commit()
                bound = result.rowcount
                total_bound += bound
                if bound == 0:
                    break
                logger.info(f"Привязано {bound} чатов (всего: {total_bound})")
                await asyncio.sleep(0.5)
            
            if total_bound > 0:
                logger.info(f"Привязка завершена: {total_bound} чатов к user_id {self.user_id}")

    async def _process_message(self, message):
        session_factory = get_session_factory()
        async with session_factory() as session:
            from sqlalchemy import select, or_
            from src.db.models import Chat
            result = await session.execute(
                select(Chat).where(
                    Chat.chat_id == message.chat_id,
                    Chat.watching == True,
                    or_(Chat.user_id == None, Chat.user_id == self.user_id)
                )
            )
            chat = result.scalar_one_or_none()
            
            if not chat:
                return  # чат не watching или привязан к другому пользователю
            
            repo = MessageRepository(session)
            
            existing = await repo.get_by_chat_and_message_id(
                message.chat_id,
                message.message_id
            )
            
            if existing:
                return
            
            await repo.create(
                chat_id=message.chat_id,
                message_id=message.message_id,
                sender_id=message.sender_id,
                sender_username=message.sender_username,
                sender_name=message.sender_name,
                text=message.text,
                date=message.date,
                raw_json=message.raw,
                topic_id=message.topic_id,
                topic_name=message.topic_name,
            )

            # Keep chat bounds in sync with real-time traffic, otherwise
            # webhook (keyed on max_message_id) never fires for live messages.
            chat_repo = ChatRepository(session)
            await chat_repo.update_message_bounds(
                message.chat_id,
                message.message_id,
                message.message_id,
                message.date,
                message.date,
            )

            logger.info(f"Saved message {message.message_id} from chat {message.chat_id} (topic_id={message.topic_id})")
