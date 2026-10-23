"""Bearer auth for the MCP port: middleware + scope resolution.

The middleware is the single place that checks the token; tools only
read the contextvar. Telegram is never on the token -> rights path:
resolve is one SQL over chats (owner requirement — an unavailable
Telegram session must not cut access to already stored data).
"""
import json
import logging
from contextvars import ContextVar, Token
from typing import Optional

from sqlalchemy import text

from src.db import AuthTokenRepository, get_session_factory
from src.db.models import AuthToken

logger = logging.getLogger(__name__)

_allowed_ids: ContextVar[Optional[frozenset]] = ContextVar("allowed_ids", default=None)


def set_allowed_ids(ids: Optional[frozenset[int]]) -> Token:
    """None = no authorization happened (scope not applied); frozenset() =
    a token without rights (everything forbidden). None ≠ ∅."""
    return _allowed_ids.set(ids)


def get_allowed_ids() -> Optional[frozenset[int]]:
    return _allowed_ids.get()


async def lookup_token(token: str) -> Optional[AuthToken]:
    """Indexed SELECT per request, no cache: a deleted token → 401 right away."""
    async with get_session_factory()() as session:
        return await AuthTokenRepository(session).get_by_token(token)


# in_folders (jsonb int list) ∩ folder_ids OR chat_id ∈ chat_ids → internal ids.
# - watching = TRUE: spec «Резолв прав» п.2 — not watched silently drops.
# - jsonb_typeof guard: a scalar/corrupt in_folders cell would kill the resolve
#   for EVERY token (precedent: add_topic in src/db/repositories.py).
# - Regex guard: a non-numeric element (DB garbage) must silently drop, not 500.
# NOTE: no ::casts on the binds — text() in SQLAlchemy 2.1 does not parse
# ":name::type" as a bind at all; asyncpg infers the array types from context.
_RESOLVE_SQL = text("""
    SELECT id
    FROM chats
    WHERE chats.watching = TRUE
      AND (chat_id = ANY(:chat_ids)
           OR EXISTS (
                SELECT 1
                FROM jsonb_array_elements_text(
                    CASE WHEN jsonb_typeof(in_folders) = 'array'
                         THEN in_folders
                         ELSE '[]'::jsonb END
                ) AS f
                WHERE f.value ~ '^-?[0-9]+$'
                  AND f.value::bigint = ANY(:folder_ids)
           ))
""")


def _clean_ints(values) -> set[int]:
    """Non-int trash (bool included) from the UI silently drops (F-5/F-6)."""
    return {v for v in (values or []) if isinstance(v, int) and not isinstance(v, bool)}


async def resolve_allowed_ids(row: AuthToken) -> frozenset[int]:
    """Token rights → internal chat ids, in one SQL. Stale entries (folder
    no longer exists / chat not watched) silently drop; empty rights →
    empty frozenset (fail closed).
    """
    folder_ids = sorted(_clean_ints(row.folder_ids))
    chat_ids = sorted(_clean_ints(row.chat_ids))

    async with get_session_factory()() as session:
        result = await session.execute(
            _RESOLVE_SQL, {"folder_ids": folder_ids, "chat_ids": chat_ids},
        )
        return frozenset(result.scalars().all())


# shortcut: состав in_folders обновляет часовой свип через «первого
# подключённого» аккаунта (src/telegram/folders.py); при смене первого
# аккаунта смысл folder_ids токенов изменится — детерминизация отложена
# владельцем.


class BearerAuthMiddleware:
    """ASGI (any scope type passes through untouched; only http is guarded)."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        token = self._parse_bearer(scope)
        if token is None:
            await self._send_json(send, 401, {"detail": "unauthorized"})
            return

        try:
            row = await lookup_token(token)
            if row is None:
                await self._send_json(send, 401, {"detail": "unauthorized"})
                return
            allowed = await resolve_allowed_ids(row)
        except Exception:
            logger.exception("auth: token lookup/resolve failed")
            # fail closed: a DB error must never let the request through
            await self._send_json(send, 500, {"detail": "internal server error"})
            return

        ctx = set_allowed_ids(allowed)
        try:
            await self.app(scope, receive, send)
        finally:
            _allowed_ids.reset(ctx)

    @staticmethod
    def _parse_bearer(scope) -> Optional[str]:
        for name, value in scope.get("headers") or []:
            if name == b"authorization":
                parts = value.decode("latin-1").strip().split(None, 1)
                if len(parts) == 2 and parts[0].lower() == "bearer":
                    return parts[1].strip()
                return None
        return None

    @staticmethod
    async def _send_json(send, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        await send({
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
            ],
        })
        await send({"type": "http.response.body", "body": body})
