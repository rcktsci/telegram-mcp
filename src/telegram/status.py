import logging
from typing import Optional

from src.telegram.client import TelegramListener
from src.config import get_settings

logger = logging.getLogger(__name__)


class TelegramStatusChecker:
    """Проверка статуса соединения с Telegram."""

    def __init__(self):
        self._client: Optional[TelegramListener] = None
        self._initialized = False

    async def _get_client(self) -> Optional[TelegramListener]:
        """Получить клиента для первой сессии."""
        settings = get_settings()

        if not settings.telegram.sessions:
            return None

        session = settings.telegram.sessions[0]
        if not session.session_string:
            return None

        if not self._client:
            self._client = TelegramListener(
                session_string=session.session_string,
                api_id=settings.telegram.api_id,
                api_hash=settings.telegram.api_hash,
                mtproxy_host=settings.telegram.mtproxy_host,
                mtproxy_port=settings.telegram.mtproxy_port,
                mtproxy_secret=settings.telegram.mtproxy_secret,
            )
            try:
                await self._client.start()
                self._initialized = True
                logger.info("Status checker: Telegram client initialized")
            except Exception as e:
                logger.error(f"Status checker: Failed to initialize client: {e}")
                return None

        return self._client

    async def get_first_client(self) -> Optional[TelegramListener]:
        """Public access to the lazily started first-session client.

        Used by UI fallbacks (folder fetch) when no worker is around.
        """
        return await self._get_client()

    async def is_connected(self) -> bool:
        """Проверить соединение с Telegram."""
        try:
            client = await self._get_client()
            if not client:
                return False

            # Проверяем что клиент подключён
            if not client._client:
                return False

            # Telethon не имеет явного is_connected(), проверяем через get_me()
            me = await client._client.get_me()
            return me is not None
        except Exception as e:
            logger.error(f"Status checker: Connection check failed: {e}")
            return False

    async def get_dialogs_count(self) -> int:
        """Получить количество диалогов."""
        try:
            client = await self._get_client()
            if not client:
                return 0

            dialogs = await client.get_dialogs_list()
            return len(dialogs)
        except Exception as e:
            logger.error(f"Status checker: Failed to get dialogs: {e}")
            return 0

    async def close(self):
        """Закрыть соединение."""
        if self._client:
            await self._client.stop()
            self._initialized = False
            logger.info("Status checker: client closed")


# Глобальный инстанс
_status_checker: Optional[TelegramStatusChecker] = None


def get_status_checker() -> TelegramStatusChecker:
    global _status_checker
    if _status_checker is None:
        _status_checker = TelegramStatusChecker()
    return _status_checker
