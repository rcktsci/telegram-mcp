"""AuthTokenRepository: CRUD over auth_tokens (bearer token store).

The repository generates the UUID4 token itself (36 chars, plaintext by
owner decision); update() is a FULL replacement of comment/folder_ids/
chat_ids; delete() of an unknown id is False.
"""

import uuid

from src.db import AuthTokenRepository, get_session_factory


async def test_create_returns_token_uuid_and_tz_aware_created_at(db):
    async with get_session_factory()() as session:
        created = await AuthTokenRepository(session).create(
            comment="ci agent", folder_ids=[1, 2], chat_ids=[3],
        )

        assert isinstance(created.token, str)
        assert len(created.token) == 36
        assert created.created_at.tzinfo is not None
        assert created.comment == "ci agent"
        assert created.folder_ids == [1, 2]
        assert created.chat_ids == [3]


async def test_get_by_token_finds_created_token(db):
    async with get_session_factory()() as session:
        created = await AuthTokenRepository(session).create(
            comment="lookup", folder_ids=[], chat_ids=[],
        )

    async with get_session_factory()() as session:
        found = await AuthTokenRepository(session).get_by_token(created.token)

    assert found is not None
    assert found.id == created.id
    assert found.comment == "lookup"


async def test_get_all_empty_before_create(db):
    async with get_session_factory()() as session:
        assert await AuthTokenRepository(session).get_all() == []


async def test_update_fully_replaces_fields(db):
    async with get_session_factory()() as session:
        created = await AuthTokenRepository(session).create(
            comment="before", folder_ids=[1], chat_ids=[2],
        )

    async with get_session_factory()() as session:
        updated = await AuthTokenRepository(session).update(
            created.id, comment="after", folder_ids=[10, 20], chat_ids=[30],
        )

        assert updated is not None
        assert updated.comment == "after"
        assert updated.folder_ids == [10, 20]
        assert updated.chat_ids == [30]
        # the credential itself is immutable
        assert updated.token == created.token

    async with get_session_factory()() as session:
        kept = await AuthTokenRepository(session).get_by_token(created.token)

    assert kept.comment == "after"
    assert kept.folder_ids == [10, 20]
    assert kept.chat_ids == [30]


async def test_delete_returns_true_then_false(db):
    async with get_session_factory()() as session:
        created = await AuthTokenRepository(session).create(
            comment="gone", folder_ids=[], chat_ids=[],
        )

    async with get_session_factory()() as session:
        assert await AuthTokenRepository(session).delete(created.id) is True

    async with get_session_factory()() as session:
        assert await AuthTokenRepository(session).delete(created.id) is False
        assert await AuthTokenRepository(session).get_by_token(created.token) is None


async def test_get_by_token_unknown_returns_none(db):
    async with get_session_factory()() as session:
        assert await AuthTokenRepository(session).get_by_token("0" * 36) is None


async def test_get_all_orders_newest_first(db):
    """F-1 (Task 1 review): newest first — the UI table must not jump
    after updates (and POST must surface the fresh token on top)."""
    from datetime import datetime, timezone

    from src.db import AuthToken

    async with get_session_factory()() as session:
        session.add(AuthToken(token="a" * 36, comment="old", created_at=datetime(2026, 1, 1, tzinfo=timezone.utc)))
        session.add(AuthToken(token="b" * 36, comment="new", created_at=datetime(2026, 10, 22, tzinfo=timezone.utc)))
        await session.commit()

    async with get_session_factory()() as session:
        tokens = await AuthTokenRepository(session).get_all()

    assert [t.comment for t in tokens] == ["new", "old"]


async def test_update_unknown_id_returns_none(db):
    async with get_session_factory()() as session:
        assert await AuthTokenRepository(session).update(999999, "x", [], []) is None


async def test_created_token_is_uuid4(db):
    # F-3: a non-random bearer secret would be a security-model hole.
    async with get_session_factory()() as session:
        created = await AuthTokenRepository(session).create(
            comment="uuid4 pin", folder_ids=[], chat_ids=[],
        )

        assert uuid.UUID(created.token).version == 4


async def test_update_with_empty_lists_resets_rights(db):
    """F-4: explicit empty arrays clear the scope (spec: PATCH is a full
    replacement; fail closed must not invert silently into full access).
    """
    async with get_session_factory()() as session:
        created = await AuthTokenRepository(session).create(
            comment="wide", folder_ids=[1, 2], chat_ids=[3],
        )

    async with get_session_factory()() as session:
        updated = await AuthTokenRepository(session).update(
            created.id, comment="wide", folder_ids=[], chat_ids=[],
        )

        assert updated is not None
        assert updated.folder_ids == []
        assert updated.chat_ids == []

    async with get_session_factory()() as session:
        kept = await AuthTokenRepository(session).get_by_token(created.token)
    assert kept.folder_ids == []
    assert kept.chat_ids == []
