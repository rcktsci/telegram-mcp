import asyncio
import signal
import sys
import logging
import structlog
import os

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from sqlalchemy import text
import uvicorn

from src.config import get_settings
from src.db import init_db, get_engine
from src.runtime import set_worker_manager, get_worker_manager
from src.worker.manager import WorkerManager
from src.mcp.server import mcp_server
from src.mcp.auth import BearerAuthMiddleware
from src.telegram.webhook import WebhookRunner
from src.telegram.status import get_status_checker
from src.telegram.folders import refresh_folders
from src.api.ui import ui_app
from src.api.auth import MasterTokenMiddleware
from src.telegram.client import MEDIA_CACHE_DIR, cleanup_media_tmp_files


async def run_until_shutdown(shutdown_event: asyncio.Event, stop_callbacks, *service_tasks: asyncio.Task) -> None:
    """Wait until shutdown is requested or any service exits, then stop everything.

    Uvicorn installs its own SIGTERM/SIGINT handlers while serving, so a signal
    often arrives as "a service task completed" rather than our event firing.
    Therefore stop_callbacks (webhook, workers) run on EVERY exit path.
    Callback errors are logged and do not prevent cancelling the services.
    """
    logger = logging.getLogger(__name__)
    shutdown_task = asyncio.create_task(shutdown_event.wait())

    done, _ = await asyncio.wait({shutdown_task, *service_tasks}, return_when=asyncio.FIRST_COMPLETED)

    if shutdown_task in done:
        logger.info("Shutdown requested; stopping services")
    else:
        logger.warning("A service exited on its own; stopping everything")

    if not shutdown_task.done():
        shutdown_task.cancel()

    for cb in stop_callbacks:
        try:
            await cb()
        except Exception as e:
            logger.error(f"Shutdown callback {getattr(cb, '__name__', cb)} failed: {e}")

    for task in service_tasks:
        task.cancel()
    await asyncio.gather(*service_tasks, return_exceptions=True)
    logger.info("All services stopped")


def setup_logging():
    settings = get_settings()

    logging.basicConfig(
        level=getattr(logging, settings.logging.level),
        format="%(message)s",
    )
    
    structlog.configure(
        processors=[
            structlog.stdlib.filter_by_level,
            structlog.stdlib.add_logger_name,
            structlog.stdlib.add_log_level,
            structlog.stdlib.PositionalArgumentsFormatter(),
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.UnicodeDecoder(),
            structlog.processors.JSONRenderer() if settings.logging.format == "json"
                else structlog.dev.ConsoleRenderer(),
        ],
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )


async def main():
    setup_logging()
    logger = logging.getLogger(__name__)
    
    logger.info("Starting Telegram MCP Server")
    
    settings = get_settings()
    logger.info(f"Loaded {len(settings.telegram.sessions)} sessions from config")
    
    await init_db()
    logger.info("Database initialized")

    # Кэш вложений для get_media_link. БЕЗ volume mount (решение владельца):
    # пересоздание контейнера сбрасывает кэш, строки в БД живут дольше.
    os.makedirs(MEDIA_CACHE_DIR, exist_ok=True)
    stale_tmp = cleanup_media_tmp_files()
    if stale_tmp:
        logger.info(f"Removed {stale_tmp} leftover .tmp file(s) from media cache")

    worker_manager = WorkerManager()
    set_worker_manager(worker_manager)
    webhook_runner = WebhookRunner()
    shutdown_event = asyncio.Event()
    
    async def shutdown():
        shutdown_event.set()
    
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, lambda s=sig: asyncio.create_task(shutdown()))
    
    await worker_manager.start()
    await webhook_runner.start()

    async def _startup_folders_refresh(timeout_seconds: float = 30.0) -> None:
        """Первичная загрузка состава папок для скоупинга токенов.

        Ограниченно ждём первого подключённого воркера (bounded-поллинг,
        без спавна клиентов — путь рефрешера ходит только в подключённых),
        рефрешним один раз; не дождались — тихо выходим, состав загрузит
        первый пасс свипа.
        """
        waited = 0.0
        while waited < timeout_seconds:
            wm = get_worker_manager()
            listener = wm.get_connected_listener() if wm else None
            # get_connected_listener() is true before the handshake finishes
            # (worker.is_connected only checks the client object) — wait_ready
            # guards the single prime from dying on a half-open client.
            if listener is not None and await listener.wait_ready(timeout=5.0):
                await refresh_folders()
                return
            await asyncio.sleep(1.0)
            waited += 1.0
        logger.info("Startup folders refresh skipped: no connected worker in time")

    _startup_folders_refresh_task = asyncio.create_task(_startup_folders_refresh(), name="refresh-folders-startup")

    # MCP-порт 8000 под Bearer-авторизацией (PLAN_AUTH_TOKENS): без валидного
    # токена — 401 до MCP-приложения; флаги stateless/json переехали из
    # run_streamable_http_async в builder приложения (mcp 2.x).
    # host обязателен: дефолт SDK "127.0.0.1" включает DNS-rebinding защиту с
    # allowed_hosts только на localhost, и запросы по IP отбрасываются с 421.
    mcp_app = BearerAuthMiddleware(
        mcp_server.streamable_http_app(stateless_http=True, json_response=True, host="0.0.0.0")
    )
    mcp_task = asyncio.create_task(
        uvicorn.Server(uvicorn.Config(mcp_app, host="0.0.0.0", port=8000, log_level="info")).serve()
    )

    # Запускаем health check сервер на порту 8081
    health_app = FastAPI()

    @health_app.get("/health")
    async def health():
        """Проверить здоровье сервиса: БД и соединение с Telegram."""
        errors = []

        # Проверка БД
        try:
            engine = get_engine()
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
            db_ok = True
        except Exception as e:
            db_ok = False
            errors.append(f"DB: {str(e)}")

        # Проверка Telegram через WorkerManager
        worker_mgr = get_worker_manager()
        if worker_mgr and worker_mgr.is_running:
            session_statuses = worker_mgr.get_sessions_status()
            all_connected = all(s["connected"] for s in session_statuses) if session_statuses else False
        else:
            session_statuses = []
            all_connected = False
            errors.append("Telegram: workers not running")

        if db_ok and all_connected:
            return {"status": "ok", "db": "connected", "telegram": "connected", "sessions": session_statuses}
        else:
            return JSONResponse(
                {"status": "degraded", "db": "connected" if db_ok else "disconnected", "telegram": "connected" if all_connected else "disconnected", "sessions": session_statuses, "errors": errors},
                status_code=503
            )

    @health_app.get("/")
    async def root():
        return {"service": "telegram-mcp", "version": "1.0.0"}

    # Запускаем health сервер на порту 8081
    health_config = uvicorn.Config(health_app, host="0.0.0.0", port=8081, log_level="info")
    health_server = uvicorn.Server(health_config)
    health_task = asyncio.create_task(health_server.serve())

    # Запускаем UI сервер на порту 8080: REST /api/* за мастер-токеном
    # (PLAN_UI_MASTER_TOKEN); HTML-шелл открыт.
    ui_config = uvicorn.Config(MasterTokenMiddleware(ui_app), host="0.0.0.0", port=8080, log_level="info")
    ui_server = uvicorn.Server(ui_config)
    ui_task = asyncio.create_task(ui_server.serve())

    # Блокируемся до SIGINT/SIGTERM (или самостоятельного завершения UI),
    # затем останавливаем workers, webhook и фоновые серверы.
    await run_until_shutdown(
        shutdown_event,
        [webhook_runner.stop, worker_manager.stop],
        ui_task,
        mcp_task,
        health_task,
    )
    logger.info("Telegram MCP Server stopped")


if __name__ == "__main__":
    asyncio.run(main())
