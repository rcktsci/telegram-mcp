"""Token management API (/api/tokens) — CRUD contract (PLAN_AUTH_TOKENS).

POST generates the UUID4 server-side; PATCH is a FULL replacement of
comment/folder_ids/chat_ids; unknown id → 404; DELETE → {"status":"ok"}.
"""

import httpx

from src.api.ui import ui_app
from src.db import AuthTokenRepository, get_session_factory


async def _request(method: str, url: str, json=None):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=ui_app), base_url="http://test") as client:
        return await client.request(method, url, json=json)


async def _create(comment="ci", folder_ids=None, chat_ids=None):
    async with get_session_factory()() as session:
        return await AuthTokenRepository(session).create(
            comment=comment, folder_ids=folder_ids or [], chat_ids=chat_ids or [],
        )


async def test_post_creates_token_201_and_shape(db):
    resp = await _request(
        "POST", "/api/tokens", {"comment": "agent", "folder_ids": [7], "chat_ids": [1501]},
    )

    assert resp.status_code == 201
    body = resp.json()
    assert set(body) == {"id", "token", "comment", "folder_ids", "chat_ids", "created_at"}
    assert len(body["token"]) == 36
    assert body["comment"] == "agent"
    assert body["folder_ids"] == [7]
    assert body["chat_ids"] == [1501]
    assert body["created_at"]


async def test_get_after_post_contains_token(db):
    created = (
        await _request("POST", "/api/tokens", {"comment": "lookup", "folder_ids": [], "chat_ids": []})
    ).json()

    resp = await _request("GET", "/api/tokens")

    assert resp.status_code == 200
    tokens = resp.json()
    assert any(
        t["id"] == created["id"] and t["token"] == created["token"] and t["comment"] == "lookup"
        for t in tokens
    )


async def test_patch_replaces_all_three_fields(db):
    created = await _create(comment="before", folder_ids=[1], chat_ids=[2])

    resp = await _request(
        "PATCH", f"/api/tokens/{created.id}",
        {"comment": "after", "folder_ids": [10, 20], "chat_ids": [30]},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["comment"] == "after"
    assert body["folder_ids"] == [10, 20]
    assert body["chat_ids"] == [30]
    assert body["token"] == created.token, "the credential itself is immutable"


async def test_patch_unknown_id_404(db):
    resp = await _request(
        "PATCH", "/api/tokens/999999", {"comment": "x", "folder_ids": [], "chat_ids": []},
    )

    assert resp.status_code == 404


async def test_delete_unknown_id_404(db):
    resp = await _request("DELETE", "/api/tokens/999999")

    assert resp.status_code == 404


async def test_delete_then_get_not_contains(db):
    created = await _create(comment="gone")

    resp = await _request("DELETE", f"/api/tokens/{created.id}")

    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}

    tokens = (await _request("GET", "/api/tokens")).json()
    assert all(t["id"] != created.id for t in tokens)
