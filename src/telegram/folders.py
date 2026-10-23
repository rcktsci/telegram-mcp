"""Telegram folder (dialog filter) helpers: live fetch + DB persistence.

refresh_folders() is the sweeper hook that persists folder membership
into chats.in_folders; token scope (auth tokens) resolves against that
column and never against the live RPC.
"""
import json
import logging
from typing import Optional

from sqlalchemy import text

from src.db import get_session_factory

logger = logging.getLogger(__name__)


async def fetch_folders_connected() -> Optional[list[dict]]:
    """Папки только от уже подключённого воркера; клиентов не создаём.

    Путь рефрешера: fallback на status-checker здесь недостижим — тот
    создаёт второго живого клиента с тем же session_string и гоняет
    коннект с воркером №0 на каждом старте сервиса.
    """
    from src.runtime import get_worker_manager

    wm = get_worker_manager()
    listener = wm.get_connected_listener() if wm else None
    if listener is None:
        return None
    return await listener.get_folder_overview()


async def fetch_folders() -> Optional[list[dict]]:
    """Получить папки из живого клиента: сначала воркеры, потом status-checker.

    Живой маршрут UI (/api/folders): fallback может СОЗДАТЬ клиента по
    первой сессии — доковое поведение маршрута; рефрешеру сюда нельзя
    (fetch_folders_connected).
    """
    from src.runtime import get_worker_manager

    wm = get_worker_manager()
    listener = wm.get_connected_listener() if wm else None
    if listener is None:
        from src.telegram.status import get_status_checker
        listener = await get_status_checker().get_first_client()
    if listener is None:
        return None
    return await listener.get_folder_overview()


async def refresh_folders() -> None:
    """Persist folder membership into chats.in_folders (best-effort).

    A member keeps its folder ids in overview order; every other chat gets
    []. None from the fetcher means "no connected client", not "no folders"
    — stored data stays. Fetch or DB failure logs and leaves data as is,
    so token scope never changes silently on a Telegram hiccup.
    """
    try:
        overview = await fetch_folders_connected()
        if overview is None:
            logger.warning("refresh_folders: no connected worker for folder overview; keeping stored in_folders")
            return

        membership: dict[int, list[int]] = {}
        for folder in overview:
            for chat_id in folder.get("chat_ids", []):
                membership.setdefault(chat_id, []).append(folder["folder_id"])

        async with get_session_factory()() as session:
            # text() SQL like the other repo updaters: ORM bulk UPDATE cannot
            # executemany without PK values in the params (bupq error).
            await session.execute(text("UPDATE chats SET in_folders = '[]'::jsonb"))
            if membership:
                await session.execute(
                    text("UPDATE chats SET in_folders = CAST(:fids AS jsonb) WHERE chat_id = :cid"),
                    [{"cid": cid, "fids": json.dumps(fids)} for cid, fids in membership.items()],
                )
            await session.commit()
        logger.info(f"refresh_folders: stored folder membership for {len(membership)} chats")
    except Exception as e:
        logger.warning(f"refresh_folders failed; keeping stored in_folders: {e}", exc_info=True)

# shortcut: состав in_folders обновляется через «первого подключённого»
# аккаунта (get_connected_listener); смена первого аккаунта в конфиге меняет
# смысл folder_ids у токенов — детерминизация отложена владельцем.
