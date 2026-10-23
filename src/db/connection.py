from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine, AsyncEngine
from typing import Optional
import asyncio
import logging
import os

_engine: Optional[AsyncEngine] = None
_session_factory: Optional[async_sessionmaker] = None


def get_engine() -> AsyncEngine:
    global _engine, _session_factory
    if _engine is None:
        from src.config import get_settings
        settings = get_settings()
        _engine = create_async_engine(settings.database.url, echo=False, pool_pre_ping=True)
        _session_factory = async_sessionmaker(_engine, class_=AsyncSession, expire_on_commit=False)
    return _engine


async def health_check() -> bool:
    engine = get_engine()
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


def get_session_factory() -> async_sessionmaker:
    get_engine()  # инициализирует если нужно
    return _session_factory


async def init_db():
    engine = get_engine()
    await _run_alembic_migrations(engine)


async def _run_alembic_migrations(engine):
    import logging
    import os
    from alembic.config import Config as AlembicConfig
    from alembic import command
    
    logger = logging.getLogger(__name__)
    
    try:
        async with engine.begin() as conn:
            from sqlalchemy import text
            
            try:
                result = await conn.execute(text("SELECT version_num FROM alembic_version ORDER BY version_num DESC LIMIT 1"))
                row = result.fetchone()
                logger.info(f"Current migration version: {row[0] if row else 'none'}")
            except Exception as e:
                logger.info(f"No alembic version found: {e}")
        
        url = engine.url
        
        # Explicit psycopg2 dialect (SQLAlchemy 2.1 defaults bare
        # "postgresql://" to psycopg3, which is not installed) built via
        # URL.create so credentials with special characters stay escaped.
        from sqlalchemy import URL
        sync_url = URL.create(
            "postgresql+psycopg2",
            username=url.username or "",
            password=url.password or "",
            host=url.host or "localhost",
            port=url.port or 5432,
            database=url.database or "telegram_mcp",
        ).render_as_string(hide_password=False)
        
        alembic_cfg = AlembicConfig()
        alembic_cfg.set_main_option("sqlalchemy.url", sync_url)
        alembic_cfg.set_main_option("script_location", "migrations")
        
        command.upgrade(alembic_cfg, "head")
        logger.info("Alembic migrations completed")
    except Exception as e:
        logger.warning(f"Alembic migration failed: {e}")
        try:
            from src.db.models import Base
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
                logger.info("Tables created via SQLAlchemy create_all (fallback)")
        except Exception as e2:
            logger.error(f"Failed to create tables: {e2}")
