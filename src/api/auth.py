"""Master token gate for the UI REST plane (PLAN_UI_MASTER_TOKEN).

Only /api/* paths are guarded; the HTML shell and non-api paths pass
through untouched. An empty ui_master_token in settings means EVERY
/api/* is 401 (fail closed — no header can match). Agent tokens
(auth_tokens) live on the MCP plane and never satisfy this gate: this
middleware compares against Settings.ui_master_token only.

Settings are read per request via get_settings() (lru_cache) so tests
can stub or cache_clear them.
"""
import secrets

from src.config import get_settings

_UNAUTHORIZED = {"detail": "unauthorized"}


class MasterTokenMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not scope.get("path", "").startswith("/api/"):
            await self.app(scope, receive, send)
            return

        master = get_settings().ui_master_token
        supplied = self._parse_bearer(scope)
        # bytes, not str: compare_digest(str, str) raises TypeError on
        # non-ASCII (latin-1 headers) → an unauthenticated client would get 500.
        if (
            master
            and supplied
            and secrets.compare_digest(master.encode(), supplied.encode())
        ):
            await self.app(scope, receive, send)
            return
        await self._send_json(send, 401, _UNAUTHORIZED)

    @staticmethod
    def _parse_bearer(scope) -> "str | None":
        # Deliberately a local copy, not an import from the MCP plane:
        # the planes are separate security boundaries (spec, Не-цели).
        for name, value in scope.get("headers") or []:
            if name == b"authorization":
                parts = value.decode("latin-1").strip().split(None, 1)
                if len(parts) == 2 and parts[0].lower() == "bearer":
                    return parts[1].strip()
                return None
        return None

    @staticmethod
    async def _send_json(send, status: int, payload: dict) -> None:
        import json

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
