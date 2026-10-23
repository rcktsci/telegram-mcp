"""H: pagination must be stable — ORDER BY needs a message_id tie-breaker.

read_messages paginates by min/max_message_id, but orders only by date.
Telethon timestamps have second precision; an ORDER BY that is not a total
order lets same-second messages silently shift across page boundaries.
A total order is only guaranteed when message_id breaks date ties, so the
contract is pinned at the compiled-SQL level (row-order instability is not
reliably reproducible on a small table).
"""

from datetime import datetime, timezone

import re

from sqlalchemy import event

from src.db import Chat, Message, get_session_factory
from src.mcp import tools as mcp_tools


async def _seed_same_second(chat_id: int) -> int:
    same_second = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    async with get_session_factory()() as session:
        chat = Chat(chat_id=chat_id, title="t", watching=True, user_id=42)
        session.add(chat)
        await session.flush()
        internal_id = chat.id
        for mid in (1, 2, 3, 4):
            session.add(Message(
                chat_id=chat_id,
                message_id=mid,
                text=f"msg {mid}",
                date=same_second,
                raw_json={},
            ))
        await session.commit()
        return internal_id


async def _captured_order_by(db, internal_id: int, direction: str) -> str:
    engine = db.kw["bind"].sync_engine
    statements: list[str] = []

    def _before(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", _before)
    try:
        await mcp_tools.read_messages(internal_id, limit=10, fetch_direction=direction)
    finally:
        event.remove(engine, "before_cursor_execute", _before)

    select_stmts = [s for s in statements if "FROM messages" in s]
    assert select_stmts, "no SELECT over messages was issued"
    order_match = re.search(r"ORDER BY([^L]+)LIMIT", select_stmts[-1], re.IGNORECASE | re.DOTALL)
    assert order_match, f"SELECT has no ORDER BY: {select_stmts[-1]}"
    return order_match.group(1)


async def test_old_to_new_order_includes_message_id_tiebreaker(db):
    internal_id = await _seed_same_second(410)
    order_by = await _captured_order_by(db, internal_id, "old_to_new")
    assert "message_id" in order_by.lower(), f"ORDER BY is not a total order: {order_by}"
    # Tie-breaker must match the direction: no bare "message_id DESC" after date ASC.
    assert "desc" not in order_by.lower(), f"old_to_new must be fully ascending: {order_by}"


async def test_new_to_old_order_includes_message_id_tiebreaker(db):
    internal_id = await _seed_same_second(411)
    order_by = await _captured_order_by(db, internal_id, "new_to_old")
    assert "message_id" in order_by.lower(), f"ORDER BY is not a total order: {order_by}"
    # Both keys must be DESC: an ASC message_id inside date DESC clusters makes
    # the "newest id" cursor systematicaly wrong.
    import re as _re
    msg_id_part = [p for p in order_by.lower().split(",") if "message_id" in p][0]
    assert "desc" in msg_id_part, f"new_to_old tie-breaker must be DESC: {order_by}"


async def test_pagination_pages_still_cover_all_messages(db):
    internal_id = await _seed_same_second(412)

    seen: list[int] = []
    page = await mcp_tools.read_messages(internal_id, limit=2, fetch_direction="old_to_new")
    seen.extend(m["message_id"] for m in page["messages"])
    while page["has_more"]:
        boundary = max(seen)
        page = await mcp_tools.read_messages(
            internal_id, limit=2, min_message_id=boundary + 1, fetch_direction="old_to_new"
        )
        seen.extend(m["message_id"] for m in page["messages"])

    assert sorted(seen) == [1, 2, 3, 4]
