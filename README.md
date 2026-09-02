# FastAPI LangChain Example

A small full-stack demo: a FastAPI + LangChain agent backend that streams replies over SSE, paired with a Next.js + Ant Design X chat UI that consumes them.

## Layout

- `backend/` — FastAPI + LangChain 1.x / LangGraph agent (SSE chat at `POST /api/chat`) **plus an integrated enterprise user & auth system** (multi-identity accounts, dual-token JWT, RBAC, rate limiting, PII encryption) under `/api/v1`.
- `frontend/` — Next.js 16 + React 19 + Ant Design X chat UI, wired to the backend via `@ant-design/x-sdk`'s `DeepSeekChatProvider`.

## Quick start

### Backend

```bash
cd backend
uv sync
cp .env.example .env   # then edit LLM_PROVIDER / API keys + JWT_SECRET_KEY
docker compose up -d   # MySQL 8 + Redis 7 (migrations auto-run on first boot)
uv run uvicorn app.main:app --reload --port 5001
```

Without any LLM API key the server falls back to a deterministic `MockChatModel` — canned replies plus demo tool calls — so the full agent loop (model → tool → model) works out of the box. Try `现在几点了`, `北京天气怎么样`, `计算 12 * (3 + 4)`.

Chat requires login by default (`CHAT_REQUIRE_AUTH=true`): register an account via `POST /api/v1/auth/register` (or the frontend `/register` page) first. Set it to `false` to allow anonymous chat.

### Frontend

```bash
cd frontend
pnpm install
pnpm dev   # http://localhost:3000
```

The chat posts to `http://127.0.0.1:5001/api/chat` (override with `NEXT_PUBLIC_API_BASE`). Start the backend first.

Login is required: `/login` and `/register` handle authentication (dual-token JWT with automatic silent refresh); `/users` is the admin user-management page (account list + role assignment). The chat sidebar avatar menu shows the current user and logout.

### Full-stack Docker deployment

Run everything (MySQL for auth, Postgres for conversation memory, Redis, backend, frontend) with one command:

```bash
docker compose up -d --build   # backend on :5001, frontend on :3000
```

Defaults to `APP_ENV=dev` with demo secrets so it works out of the box. For production, inject real secrets (`JWT_SECRET_KEY`, `PII_KEYS`, `PII_BLIND_INDEX_KEY`, LLM keys) via environment variables and set `APP_ENV=production` — the backend refuses to boot on weak or placeholder keys. MySQL/Redis here are only reachable inside the compose network, so this stack can run alongside the dev-oriented `backend/docker-compose.yml`, which exposes 3306/6379 to the host. The frontend image inlines `NEXT_PUBLIC_API_BASE` at build time (default `http://localhost:5001`).

## Configuration

Backend reads `backend/.env` (see `.env.example`). Key vars:

| Var | Values | Notes |
|-----|--------|-------|
| `LLM_PROVIDER` | `openai` / `anthropic` / `mock` | Auto-falls back to `mock` if the chosen provider's key is missing |
| `LLM_MODEL` | e.g. `gpt-4o-mini`, `claude-sonnet-4-20250514` | Ignored when provider is `mock` |
| `OPENAI_API_KEY` / `OPENAI_BASE_URL` | OpenAI or any OpenAI-compatible endpoint (DeepSeek, Moonshot, Ollama) | Leave `BASE_URL` empty for `api.openai.com` |
| `ANTHROPIC_API_KEY` | | |
| `SYSTEM_PROMPT` | | |
| `CORS_ORIGINS` | comma-separated origins | Default includes `localhost:3000` and `localhost:5173` |
| `CHECKPOINT_BACKEND` | `sqlite` / `postgres` | Conversation-memory store (LangGraph checkpointer). `sqlite` (default) keeps a local file via `CHECKPOINT_DB_PATH`; `postgres` for production / multi-replica |
| `CHECKPOINT_DATABASE_URL` | `postgres://user:pass@host:5432/db` | Required when `CHECKPOINT_BACKEND=postgres`; `checkpoint_*` tables are created automatically on first boot |

Settings are lru-cached at startup — restart after editing `.env`.

### Schema migrations (Alembic)

`backend/migrations/` only bootstraps a **fresh** database (DDL + RBAC seed + an `alembic_version` stamp). Every later schema change is an Alembic migration under `backend/alembic/`:

```bash
cd backend
uv run alembic revision --autogenerate -m "Add xxx"   # review the generated file
uv run alembic upgrade head                           # apply
```

In the docker-compose stack, run migrations from the backend container: `docker compose exec backend alembic upgrade head`.

## API

### `GET /api/health`
```json
{ "status": "ok", "provider": "openai", "model": "gpt-4o-mini" }
```

`GET /live` (liveness: always 200 if the process serves) and `GET /ready` (readiness: probes MySQL + Redis with a 2s cap; 503 with the failing dependency list when degraded) are provided for orchestrators. The compose backend image uses `/ready` as its Docker HEALTHCHECK.

### `POST /api/chat`
Requires `Authorization: Bearer <access_token>` when `CHAT_REQUIRE_AUTH=true` (default). Request body (OpenAI-style):
```json
{
  "messages": [{"role": "user", "content": "你是谁"}],
  "conversation_id": "default"
}
```

Response: `text/event-stream`. Each line is `data: {"choices":[{"delta":{"content":"..."}}]}`, terminated by `data: [DONE]`. If the agent raises mid-stream, a **sanitized** notice (request-id only; the raw exception stays in server logs) is appended as a final content delta. Conversation memory is keyed by `conversation_id` (LangGraph `thread_id`).

Hardening (all tunable via env):

- Input caps: `CHAT_MAX_MESSAGES` (default 100) and `CHAT_MAX_MESSAGE_CHARS` (default 32000); `conversation_id` is limited to 64 chars.
- Rate limiting: token bucket per account (logged in) or per client IP (anonymous) — `RL_CHAT_CAPACITY` / `RL_CHAT_REFILL_PER_MIN`. Requires Redis; skipped with a warning when Redis is down.
- Anonymous isolation: with `CHAT_REQUIRE_AUTH=false` and no explicit `conversation_id`, each request gets a random one-off thread id so strangers never share checkpoint memory. An explicitly provided `conversation_id` is honored (intentional shared session).
- Behind a reverse proxy set `TRUSTED_PROXY_HOPS=1` so rate limiting/audit resolve the real client IP from `X-Forwarded-For`.

### User & auth (`/api/v1`)

`POST /api/v1/auth/register` · `POST /api/v1/auth/login` (dual-token) · `POST /api/v1/auth/refresh` (rotation + reuse detection) · `POST /api/v1/auth/logout` · `GET /api/v1/account/me` · `GET /api/v1/admin/accounts` · `POST /api/v1/admin/accounts/{uuid}/roles` — see `backend/README.md` for the full table and `backend/docs/architecture.md` for sequence diagrams. Responses use a `{code, message, data}` envelope.

## Architecture notes

- **Agent** — `backend/app/agent.py` `get_agent()` (lru-cached): `create_agent(model, tools=ALL_TOOLS, system_prompt, checkpointer=InMemorySaver())`. Multi-turn memory lives in-process; restart clears it.
- **Mock model** — `MockChatModel` streams canned replies and emits demo tool calls for time/weather/math. Lets the UI run end-to-end with no credentials. Check `GET /api/health` `provider` field to confirm which model is active.
- **Tools** (`backend/app/tools.py`) — `get_current_time`, `calculate` (AST-based safe eval, not `eval`), `get_weather` (mock data).
- **SSE format** — OpenAI-style chunks so `@ant-design/x-sdk`'s `DeepSeekChatProvider` parses them directly. Tool calls/results from the agent are not surfaced in the stream; the agent emits a final text message after any tool loop, which is what the UI renders.
- **Frontend provider** — One `DeepSeekChatProvider` per conversation key, cached in `providerCaches`. `useXChat` manages streaming state, retry, abort.
- **Next.js 16 caveat** — This repo's Next.js has breaking changes vs. prior versions; see `frontend/AGENTS.md` before touching Next.js-specific code.

- **User & auth** — merged into the backend: account/credential/profile split, Redis session registry for refresh tokens (instant revocation on logout/kick/password change), Argon2id hashing, token-bucket rate limiting, RBAC dependencies (`require_permissions`). See `backend/docs/architecture.md`.

See `CLAUDE.md` for the full SSE contract and architecture details.
