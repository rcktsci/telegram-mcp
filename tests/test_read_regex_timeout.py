"""C4: statement_timeout must be active while the regex SELECT runs.

The old code did SET -> RESET -> SELECT, so a pathological regex from an LLM
hit Postgres with no timeout at all (ReDoS by another name). This test pins
the observable DB-call order: SET, then SELECT, then RESET.
"""

from datetime import datetime, timezone

import pytest
from sqlalchemy import event, select

from src.db import Chat, Message, get_session_factory
from src.mcp import tools as mcp_tools


async def _seed(chat_id: int) -> int:
    async with get_session_factory()() as session:
        chat = Chat(chat_id=chat_id, title="t", watching=True, user_id=42)
        session.add(chat)
        await session.flush()
        internal_id = chat.id
        session.add(Message(
            chat_id=chat_id,
            message_id=1,
            text="hello world",
            date=datetime.now(timezone.utc),
            raw_json={},
        ))
        await session.commit()
        return internal_id


async def test_regex_select_runs_inside_statement_timeout(db):
    internal_id = await _seed(300)

    engine = db.kw["bind"].sync_engine
    statements: list[str] = []

    def _before(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", _before)
    try:
        out = await mcp_tools.read_messages(internal_id, search_regex="hello")
    finally:
        event.remove(engine, "before_cursor_execute", _before)

    assert len(out["messages"]) == 1

    idx_set = next(i for i, s in enumerate(statements) if "SET statement_timeout" in s)
    idx_reset = next(i for i, s in enumerate(statements) if "RESET statement_timeout" in s)
    idx_select = next(i for i, s in enumerate(statements) if "FROM messages" in s)

    assert idx_set < idx_select < idx_reset, (
        f"SELECT must run while timeout is active; got order: "
        f"SET@{idx_set}, SELECT@{idx_select}, RESET@{idx_reset}"
    )


async def test_regex_timeout_raises_value_error(db):
    internal_id = await _seed(301)

    engine = db.kw["bind"].sync_engine
    real_execute = db_connection_session_execute_original = None  # noqa: F841

    # Simulate a statement timeout raised by the SELECT: patch the session
    # factory to a wrapper whose execute raises on the messages SELECT.
    import src.db.connection as conn_mod

    original_factory = conn_mod._session_factory

    class FailingSession:
        def __init__(self, real):
            self._real = real

        async def __aenter__(self):
            await self._real.__aenter__()
            return self

        async def __aexit__(self, *a):
            return await self._real.__aexit__(*a)

        async def execute(self, stmt, *a, **kw):
            sql = str(stmt)
            if "FROM messages" in sql:
                raise Exception("canceling statement due to statement timeout")
            return await self._real.execute(stmt, *a, **kw)

    def factory():
        return FailingSession(original_factory())

    conn_mod._session_factory = factory
    try:
        with pytest.raises(ValueError, match="timeout"):
            await mcp_tools.read_messages(internal_id, search_regex="hello")
    finally:
        conn_mod._session_factory = original_factory
