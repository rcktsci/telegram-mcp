"""Idempotent insert + edit-freshness batch upsert (Go backport).

The realtime worker and the history loader can race on the same
(chat_id, message_id). ON CONFLICT DO NOTHING keeps the original
row for the single-insert path and must return None instead of raising.
The batch path adds an edit-freshness policy: only a strictly newer
edit_date refreshes a stored row.
"""


from src.db import MessageRepository, get_session_factory


async def test_duplicate_create_returns_none(db):
    async with get_session_factory()() as session:
        repo = MessageRepository(session)
        first = await repo.create(chat_id=300, message_id=1, text="original")
        assert first is not None

        second = await repo.create(chat_id=300, message_id=1, text="dup")
        assert second is None

    async with get_session_factory()() as session:
        kept = await MessageRepository(session).get_by_chat_and_message_id(300, 1)
    assert kept is not None
    assert kept.text == "original"


async def test_batch_overwrites_row_on_newer_edit_date(db):
    """A batch copy that carries a NEWER edit_date refreshes the stored row.

    Covers the lost-edit window: message stored before an edit, watcher
    off at edit time, sweep refetches the page — DO NOTHING would keep
    the stale text forever.
    """
    async with get_session_factory()() as session:
        repo = MessageRepository(session)
        first = await repo.create(
            chat_id=400, message_id=1, text="before edit",
            raw_json={"text": "before edit", "date": "2026-10-20T15:00:00+00:00"},
        )
        assert first is not None

        saved = await repo.create_batch([
            {
                "chat_id": 400,
                "message_id": 1,
                "text": "after edit",
                "raw_json": {
                    "text": "after edit",
                    "edit_date": "2026-10-20T15:30:34+00:00",
                },
            },
        ], session=session)
        assert saved == 1

    async with get_session_factory()() as session:
        kept = await MessageRepository(session).get_by_chat_and_message_id(400, 1)
    assert kept is not None
    assert kept.text == "after edit"
    assert kept.is_edited is True
    assert kept.raw_json["edit_date"] == "2026-10-20T15:30:34+00:00"


async def test_batch_without_edit_date_keeps_original(db):
    """No edit_date in the incoming copy — first writer wins, untouched."""
    async with get_session_factory()() as session:
        repo = MessageRepository(session)
        first = await repo.create(chat_id=401, message_id=2, text="original")
        assert first is not None

        saved = await repo.create_batch([
            {"chat_id": 401, "message_id": 2, "text": "newer copy", "raw_json": {}},
        ], session=session)
        assert saved == 0

    async with get_session_factory()() as session:
        kept = await MessageRepository(session).get_by_chat_and_message_id(401, 2)
    assert kept.text == "original"
    assert kept.is_edited is False


async def test_batch_equal_edit_date_no_overwrite(db):
    """Same edit_date both sides — identical state, no churn."""
    edit_ts = "2026-10-20T15:30:34+00:00"
    async with get_session_factory()() as session:
        repo = MessageRepository(session)
        await repo.create_batch([
            {
                "chat_id": 402, "message_id": 3, "text": "state A",
                "raw_json": {"edit_date": edit_ts, "text": "state A"},
            },
        ], session=session)

        saved = await repo.create_batch([
            {
                "chat_id": 402, "message_id": 3, "text": "state B",
                "raw_json": {"edit_date": edit_ts, "text": "state B"},
            },
        ], session=session)
        assert saved == 0

    async with get_session_factory()() as session:
        kept = await MessageRepository(session).get_by_chat_and_message_id(402, 3)
    assert kept.text == "state A"


async def test_batch_older_edit_date_no_overwrite(db):
    """Monotonic edits: an older edit copy must not clobber a newer one."""
    async with get_session_factory()() as session:
        repo = MessageRepository(session)
        await repo.create_batch([
            {
                "chat_id": 403, "message_id": 4, "text": "newer",
                "raw_json": {"edit_date": "2026-10-21T10:00:00+00:00"},
            },
        ], session=session)

        saved = await repo.create_batch([
            {
                "chat_id": 403, "message_id": 4, "text": "older",
                "raw_json": {"edit_date": "2026-10-20T08:00:00+00:00"},
            },
        ], session=session)
        assert saved == 0

    async with get_session_factory()() as session:
        kept = await MessageRepository(session).get_by_chat_and_message_id(403, 4)
    assert kept.text == "newer"


async def test_batch_refresh_never_resurrects_deleted_row(db):
    """A deleted (is_deleted) row must stay deleted after a batch refresh.

    The set_ whitelist touches only text/raw_json/is_edited — never
    is_deleted, so a freshness-update cannot resurrect a deleted message.
    """
    async with get_session_factory()() as session:
        repo = MessageRepository(session)
        await repo.create(chat_id=405, message_id=9, text="alive", raw_json={"text": "alive"})
        await repo.mark_deleted(405, 9)

        saved = await repo.create_batch([
            {
                "chat_id": 405, "message_id": 9, "text": "edited again",
                "raw_json": {"text": "edited again",
                             "edit_date": "2026-10-20T15:30:34+00:00"},
            },
        ], session=session)
        assert saved == 1

    async with get_session_factory()() as session:
        kept = await MessageRepository(session).get_by_chat_and_message_id(405, 9)
    assert kept.is_deleted is True
    assert kept.text == "edited again"
    assert kept.is_edited is True


async def test_batch_mixed_new_and_freshened_rows_counted(db):
    """One batch, two fates: brand-new insert + freshness-update.

    RETURNING covers both paths, so the returned count is inserts+updates —
    the documented side effect the sweep's counters rely on.
    """
    async with get_session_factory()() as session:
        repo = MessageRepository(session)
        await repo.create(
            chat_id=404, message_id=5, text="stale",
            raw_json={"text": "stale"},
        )

        saved = await repo.create_batch([
            {
                "chat_id": 404, "message_id": 5, "text": "refreshed",
                "raw_json": {"text": "refreshed",
                             "edit_date": "2026-10-21T09:00:00+00:00"},
            },
            {
                "chat_id": 404, "message_id": 6, "text": "brand new",
                "raw_json": {"text": "brand new"},
            },
        ], session=session)
        assert saved == 2

    async with get_session_factory()() as session:
        refreshed = await MessageRepository(session).get_by_chat_and_message_id(404, 5)
        fresh = await MessageRepository(session).get_by_chat_and_message_id(404, 6)
    assert refreshed.text == "refreshed"
    assert refreshed.is_edited is True
    assert fresh.text == "brand new"
    assert fresh.is_edited is False


async def test_all_fields_persist_on_insert(db):
    from datetime import datetime, timezone

    async with get_session_factory()() as session:
        repo = MessageRepository(session)
        saved = await repo.create(
            chat_id=301,
            message_id=2,
            sender_id=9,
            sender_username="u9",
            sender_name="User Nine",
            text="text",
            date=datetime.now(timezone.utc),
            raw_json={"a": 1},
            topic_id=5,
            topic_name="General",
        )
        assert saved is not None
        assert saved.sender_id == 9
        assert saved.raw_json == {"a": 1}
        assert saved.topic_id == 5
