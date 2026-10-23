# Telegram MCP Server

MCP server that mirrors Telegram chats into PostgreSQL via Telethon user sessions
and exposes them to AI agents over Streamable HTTP (stateless JSON-RPC, JSON responses).

## Features

- Multi-account Telegram monitoring (Telethon user sessions) into PostgreSQL
- Hourly history sweep with resume checkpoints (`history_sweep_upto` per chat)
- Live updates via webhooks + on-demand reads
- 7 MCP tools: list chats, read/search/count messages, single message, chat members, file download
- Cross-chat full-text search (Russian + English morphology) and regex filtering
- Per-token access scoping for agents: Bearer tokens bound to Telegram folders/chats,
  stored in PostgreSQL
- Master-token protection for the web UI and the REST management API (fail closed
  when `UI_MASTER_TOKEN` is unset)
- Web UI: chat list with folder filter, watch toggle, bulk watch, history cleanup,
  agent-token management (create/edit/delete), folder-based message counts

## MCP Tools

| Tool             | Purpose                                                                    |
|------------------|----------------------------------------------------------------------------|
| `list_chats`     | List all monitored chats with internal IDs, titles and topics              |
| `read_messages`  | Fetch messages of one chat with filters (dates, regex, FTS, media, topic)  |
| `search_messages`| Cross-chat search over all watched chats (newest first)                    |
| `count_messages` | Message counts per chat for a period (respects token scope)                |
| `get_message`    | Single message by ID                                                       |
| `get_chat_members`| Chat members via Telegram API                                             |
| `get_file`       | Download photo/document/voice from a message                               |

`chat_id` arguments are **internal IDs** from `list_chats`, not Telegram chat IDs.

## Transport and auth

- MCP: `http://localhost:8001/mcp` — Streamable HTTP, stateless, JSON responses.
  Every call requires `Authorization: Bearer <agent token>`. Agent tokens are minted
  in the web UI and scoped to Telegram folders and/or individual chats: chats outside
  the scope return `forbidden` instead of data.
- UI/REST: `http://localhost:8080/` — login with `UI_MASTER_TOKEN`. Every `/api/*`
  request requires the same token as `Authorization: Bearer`. If `UI_MASTER_TOKEN`
  is unset, every `/api/*` request is rejected (fail closed).
- Health: `http://localhost:8081/health` (unauthenticated, internal).

## Quickstart

```bash
cp config.yml.example config.yml   # fill in api_id, api_hash, session_string
cp .env.example .env               # set UI_MASTER_TOKEN
docker compose up -d
```

Migrations run automatically on container start. Open `http://localhost:8080`,
log in with the master token, create an agent token (scoped to folders), and point
your MCP client at `http://localhost:8001/mcp` with `Authorization: Bearer <agent token>`.

## Configuration

`config.yml` (see `config.yml.example`):

| Key                       | Purpose                                            |
|---------------------------|----------------------------------------------------|
| `postgres.*`              | Database connection (host/port/database/user/pass) |
| `telegram.api_id`         | Telegram API ID (my.telegram.org)                  |
| `telegram.api_hash`       | Telegram API hash                                  |
| `telegram.sessions[]`     | Telethon session strings, one per account          |
| `history_sweep.interval_hours` | History sweep period (default 6.0)            |

Environment overrides: `DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER`, `DB_PASSWORD`,
`UI_MASTER_TOKEN`.

## Security notes

- Agent tokens are UUID4 bearer secrets scoped to folders/chats; a token that is not
  allowed to read a chat gets `forbidden`, and counts/searches never leak out-of-scope data.
- The master token is compared constant-time; an unset `UI_MASTER_TOKEN` locks the
  whole REST surface.
- Serve the UI behind TLS if it is reachable outside your network.

## Development

```bash
docker build -f Dockerfile.tests -t tgmcreview-tests .
docker network create tgmcreview-net
docker run --rm --network tgmcreview-net `
  -e TEST_DATABASE_URL=postgresql+asyncpg://telegram_mcp:your_password@tgmcreview-pg:5432/telegram_mcp `
  -v "${PWD}/tests:/app/tests" -v "${PWD}/src:/app/src" `
  -v "${PWD}/main.py:/app/main.py" -v "${PWD}/pytest.ini:/app/pytest.ini" `
  tgmcreview-tests python -m pytest tests -q
```

Pushes to `main`/`master` run the test suite (PostgreSQL service container) and publish
the image to `ghcr.io/rcktsci/telegram-mcp:YYYY-MM-DD`.
