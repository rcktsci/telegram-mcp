"""Bearer auth middleware + scope resolution (pure DB, no Telegram RPC).

401 matrix: missing/non-bearer/garbage/unknown → 401 JSON; the scheme is
case-insensitive with stray spaces stripped; a deleted token → 401 right
away (lookup per request, no cache). resolve_allowed_ids = one SQL over
chats: in_folders ∩ token.folder_ids OR chat_id ∈ token.chat_ids →
internal ids; stale/garbage entries drop silently. DB failure → 500
(fail closed), non-http scopes pass through untouched.
"""

import uuid

import httpx

from src.db import AuthTokenRepository, Chat, get_session_factory
from src.mcp.auth import BearerAuthMiddleware, get_allowed_ids, resolve_allowed_ids


def _recording_echo_app(seen: list):
    async def app(scope, receive, send):
        seen.append(get_allowed_ids())
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    return app


async def _request(app, headers: dict):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        return await client.get("/", headers=headers)


async def _make_token(folder_ids=None, chat_ids=None):
    async with get_session_factory()() as session:
        return await AuthTokenRepository(session).create(
            comment="t", folder_ids=folder_ids or [], chat_ids=chat_ids or [],
        )


async def _delete_token(token_id: int) -> None:
    async with get_session_factory()() as session:
        await AuthTokenRepository(session).delete(token_id)


async def _seed_chat(telegram_chat_id: int, in_folders: list, watching: bool = True) -> int:
    async with get_session_factory()() as session:
        chat = Chat(chat_id=telegram_chat_id, title="t", watching=watching, in_folders=in_folders)
        session.add(chat)
        await session.commit()
        return chat.id


async def test_missing_header_401():
    seen = []
    resp = await _request(BearerAuthMiddleware(_recording_echo_app(seen)), {})

    assert resp.status_code == 401
    assert resp.json() == {"detail": "unauthorized"}
    assert seen == [], "the app must not run without a valid token"


async def test_non_bearer_401():
    seen = []
    resp = await _request(BearerAuthMiddleware(_recording_echo_app(seen)), {"Authorization": "Basic dXNlcjpwd2Q="})

    assert resp.status_code == 401
    assert seen == []


async def test_garbage_token_401(db):
    resp = await _request(BearerAuthMiddleware(_recording_echo_app([])), {"Authorization": "Bearer not-a-uuid"})

    assert resp.status_code == 401


async def test_unknown_uuid_401(db):
    unknown = str(uuid.uuid4())
    resp = await _request(BearerAuthMiddleware(_recording_echo_app([])), {"Authorization": f"Bearer {unknown}"})

    assert resp.status_code == 401


async def test_bearer_case_insensitive(db):
    token = await _make_token()
    seen = []
    resp = await _request(
        BearerAuthMiddleware(_recording_echo_app(seen)),
        {"Authorization": f"bearer   {token.token}  "},
    )

    assert resp.status_code == 200
    assert isinstance(seen[-1], frozenset), "the app must see the resolved scope"


async def test_deleted_token_gets_401(db):
    token = await _make_token()
    seen = []
    mw = BearerAuthMiddleware(_recording_echo_app(seen))

    ok = await _request(mw, {"Authorization": f"Bearer {token.token}"})
    assert ok.status_code == 200

    await _delete_token(token.id)

    gone = await _request(mw, {"Authorization": f"Bearer {token.token}"})
    assert gone.status_code == 401


async def test_db_error_gives_500_fail_closed(db, monkeypatch):
    async def broken_lookup(token):
        raise RuntimeError("db down")

    monkeypatch.setattr("src.mcp.auth.lookup_token", broken_lookup)

    resp = await _request(
        BearerAuthMiddleware(_recording_echo_app([])),
        {"Authorization": f"Bearer {uuid.uuid4()}"},
    )

    assert resp.status_code == 500


async def test_non_http_scope_passes_through():
    seen_scopes = []

    async def app(scope, receive, send):
        seen_scopes.append(scope)

    await BearerAuthMiddleware(app)({"type": "lifespan"}, None, None)

    assert seen_scopes == [{"type": "lifespan"}]


async def test_resolve_union_folders_and_chats(db):
    a_id = await _seed_chat(1401, [7])
    b_id = await _seed_chat(1402, [])
    await _seed_chat(1403, [])  # outside the rights
    token = await _make_token(folder_ids=[7], chat_ids=[1402])

    resolved = await resolve_allowed_ids(token)

    assert resolved == {a_id, b_id}


async def test_stale_folder_id_drops_silently(db):
    d_id = await _seed_chat(1411, [])
    token = await _make_token(folder_ids=[999], chat_ids=[1411])

    resolved = await resolve_allowed_ids(token)

    assert resolved == {d_id}


async def test_non_int_garbage_drops_silently(db):
    """F-5/F-6 carried: non-int trash from the UI must not 500 on a cast."""
    a_id = await _seed_chat(1421, [7])
    token = await _make_token(
        folder_ids=[7, "x", None, True],
        chat_ids=[1421, "y", None, False],
    )

    resolved = await resolve_allowed_ids(token)

    assert resolved == {a_id}


async def test_resolve_pure_db_no_rpc(db, monkeypatch):
    async def broken_fetch():
        raise RuntimeError("telegram down")

    monkeypatch.setattr("src.telegram.folders.fetch_folders", broken_fetch)

    a_id = await _seed_chat(1431, [7])
    token = await _make_token(folder_ids=[7])

    resolved = await resolve_allowed_ids(token)

    assert resolved == {a_id}


async def test_resolve_skips_not_watching_chats(db):
    """Spec «Резолв прав» п.2: not watched — silently dropped."""
    watched_id = await _seed_chat(1441, [7])
    not_watched_id = await _seed_chat(1442, [7], watching=False)
    token = await _make_token(folder_ids=[7])

    resolved = await resolve_allowed_ids(token)

    assert resolved == {watched_id}
    assert not_watched_id not in resolved


async def test_contextvar_reset_after_request(db):
    """Without the finally-reset the request scope leaks into the caller."""
    token = await _make_token()

    await _request(
        BearerAuthMiddleware(_recording_echo_app([])),
        {"Authorization": f"Bearer {token.token}"},
    )

    assert get_allowed_ids() is None
