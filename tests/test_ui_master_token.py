"""Master token gate for the UI REST plane (PLAN_UI_MASTER_TOKEN).

Only /api/* is guarded; the HTML shell and non-api paths pass through.
Empty ui_master_token → every /api/* is 401 (fail closed), nothing to
compare against. Agent tokens (auth_tokens) live on the MCP plane and
never satisfy this gate. Settings are stubbed per test (the middleware
reads get_settings() per request).
"""

import uuid
from types import SimpleNamespace

import httpx

from src.api.auth import MasterTokenMiddleware
from src.api.ui import ui_app
from src.config import get_settings
from src.db import AuthTokenRepository, get_session_factory

MASTER = "master-secret-123"


def _use_master(monkeypatch, token: str) -> None:
    monkeypatch.setattr(
        "src.api.auth.get_settings",
        lambda: SimpleNamespace(ui_master_token=token),
    )


async def _request(app, method: str, url: str, headers: dict = None):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        return await client.request(method, url, headers=headers or {})


async def test_missing_header_401(db, monkeypatch):
    _use_master(monkeypatch, MASTER)
    resp = await _request(MasterTokenMiddleware(ui_app), "GET", "/api/tokens")

    assert resp.status_code == 401
    assert resp.json() == {"detail": "unauthorized"}


async def test_wrong_token_401(db, monkeypatch):
    _use_master(monkeypatch, MASTER)
    resp = await _request(
        MasterTokenMiddleware(ui_app), "GET", "/api/tokens",
        {"Authorization": f"Bearer {uuid.uuid4()}"},
    )

    assert resp.status_code == 401


async def test_correct_token_200(db, monkeypatch):
    _use_master(monkeypatch, MASTER)
    resp = await _request(
        MasterTokenMiddleware(ui_app), "GET", "/api/tokens",
        {"Authorization": f"Bearer {MASTER}"},
    )

    assert resp.status_code == 200
    assert resp.json() == []


async def test_master_case_insensitive(db, monkeypatch):
    _use_master(monkeypatch, MASTER)
    resp = await _request(
        MasterTokenMiddleware(ui_app), "GET", "/api/tokens",
        {"Authorization": f"bearer   {MASTER}  "},
    )

    assert resp.status_code == 200


async def test_unset_env_denies_everything(db, monkeypatch):
    _use_master(monkeypatch, "")
    for headers in ({}, {"Authorization": f"Bearer {uuid.uuid4()}"}):
        resp = await _request(MasterTokenMiddleware(ui_app), "GET", "/api/tokens", headers)
        assert resp.status_code == 401


async def test_agent_token_not_valid_for_rest(db, monkeypatch):
    _use_master(monkeypatch, MASTER)
    async with get_session_factory()() as session:
        agent = await AuthTokenRepository(session).create(
            comment="agent", folder_ids=[], chat_ids=[],
        )

    resp = await _request(
        MasterTokenMiddleware(ui_app), "GET", "/api/tokens",
        {"Authorization": f"Bearer {agent.token}"},
    )

    assert resp.status_code == 401


async def test_non_ascii_token_401_not_500(db, monkeypatch):
    """M-2: header bytes decode to latin-1 and can carry non-ASCII — the
    unauthenticated client must get 401, never a TypeError-500."""
    _use_master(monkeypatch, MASTER)
    resp = await _request(
        MasterTokenMiddleware(ui_app), "GET", "/api/tokens",
        {"Authorization": "Bearer caf\u00e9-token-non-ascii".encode("latin-1")},
    )

    assert resp.status_code == 401
    assert resp.json() == {"detail": "unauthorized"}


async def test_shell_open_without_token(monkeypatch):
    _use_master(monkeypatch, MASTER)
    resp = await _request(MasterTokenMiddleware(ui_app), "GET", "/")

    assert resp.status_code == 200


async def test_api_paths_gated_not_shell(monkeypatch):
    _use_master(monkeypatch, MASTER)
    docs = await _request(MasterTokenMiddleware(ui_app), "GET", "/docs")
    root = await _request(MasterTokenMiddleware(ui_app), "GET", "/")

    assert docs.status_code == 200
    assert root.status_code == 200


def test_settings_read_env_var(monkeypatch):
    """The pydantic-settings trap: ui_master_token must come from OS-env/.env
    through the field itself — get_settings() must NOT bypass it."""
    monkeypatch.setenv("UI_MASTER_TOKEN", "from-env")
    get_settings.cache_clear()
    try:
        assert get_settings().ui_master_token == "from-env"
    finally:
        get_settings.cache_clear()


def test_settings_read_dotenv_file(tmp_path, monkeypatch):
    """M-3: the spec names .env as THE source (compose interpolation feeds it).
    A get_settings() rewrite with os.environ.get would silently kill this
    path — the pin keeps it alive."""
    monkeypatch.delenv("UI_MASTER_TOKEN", raising=False)
    (tmp_path / ".env").write_text("UI_MASTER_TOKEN=test-value-123\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    get_settings.cache_clear()
    try:
        assert get_settings().ui_master_token == "test-value-123"
    finally:
        get_settings.cache_clear()
