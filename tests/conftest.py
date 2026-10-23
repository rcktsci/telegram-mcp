"""Test fixtures: isolated PostgreSQL schema + rebound session factory.

The suite runs inside Docker (see Dockerfile.tests) against a disposable
postgres container. TEST_DATABASE_URL must point at it.
"""

import os

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine, AsyncSession

from src.db import Base
from src.db import connection as db_connection
from src.db.fts import FTS_STATEMENTS

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://telegram_mcp:your_password@postgres:5432/telegram_mcp",
)


@pytest.fixture()
async def db():
    """Fresh schema per test; all get_session_factory() calls hit the test DB."""
    engine = create_async_engine(TEST_DATABASE_URL, echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
        for statement in FTS_STATEMENTS:  # create_all не знает про функции/индексы FTS
            await conn.execute(text(statement))

    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    original_engine = db_connection._engine
    original_factory = db_connection._session_factory
    db_connection._engine = engine
    db_connection._session_factory = factory

    yield factory

    db_connection._engine = original_engine
    db_connection._session_factory = original_factory
    await engine.dispose()
