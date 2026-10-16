# FastAPI LangChain Example

A small full-stack demo: a FastAPI + LangChain agent backend that streams replies over SSE, paired with a Next.js + Ant Design X chat UI that consumes them.

## Layout

- `backend/` — FastAPI + LangChain 1.x / LangGraph agent (SSE chat at `POST /api/chat`) **plus an integrated enterprise user & auth system** (multi-identity accounts, dual-token JWT, RBAC, rate limiting, PII encryption) under `/api/v1`.
- `frontend/` — Next.js 16 + React 19 + Ant Design X **platform UI** (sidebar shell: 仪表盘 / AI对话助手 / 智能体 / 资产中心 / 任务中心 / 可观测中心 / 配置中心), wired to the backend via `@ant-design/x-sdk`'s `DeepSeekChatProvider`. Chat lives at `/chat`, agent management at `/agents`.
- `docs/` — 设计文档：[`docs/aiops-platform-design.md`](docs/aiops-platform-design.md) 描述已落地的企业级 AI 运维智能平台第一期（外壳 + 智能体注册表 + 多智能体运行时）；其余为教育智能体演变方向（非已实现功能）。从 [`docs/README.md`](docs/README.md) 开始读。

## Quick start

### Backend

```bash
cd backend
uv sync
cp .env.example .env   # then edit LLM_PROVIDER / API keys + JWT_SECRET_KEY
docker compose up -d   # MySQL 8 + Redis 7 (migrations auto-run on first boot)
uv run uvicorn app.main:app --reload --port 5001
```

Without any LLM API key the server falls back to a deterministic `MockChatModel` — canned replies plus demo tool calls — so the full agent loop (model → tool → model) works out of the box. Try `现在几点了`, `北京天气怎么样`, `计算 12 * (3 + 4)`, `你有什么技能` (the mock model also demos the skill-loading tool chain).

Chat requires login by default (`CHAT_REQUIRE_AUTH=true`): register an account via `POST /api/v1/auth/register` (or the frontend `/register` page) first. Set it to `false` to allow anonymous chat.

### Frontend

```bash
cd frontend
pnpm install
pnpm dev   # http://localhost:3000
```

The chat posts to `http://127.0.0.1:5001/api/chat` (override with `NEXT_PUBLIC_API_BASE`). Start the backend first.

Login is required: `/login` and `/register` handle authentication (dual-token JWT with automatic silent refresh). After login the shell lands on `/dashboard`; the chat is `/chat`, agent management is `/agents` (admin/operator can create, edit, pause and delete agents; `supervisor_main` is protected), user management moved to `/settings/users` and personal settings to `/settings/profile`. The top-right avatar menu shows the current user and logout.

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
| `SKILLS_ENABLED` / `SKILLS_DIR` | `true` / `./skills` | Agent skill directories (see `backend/docs/skills-design.md`). Skills hot-reload on change — no restart needed |
| `SKILL_MAX_CATALOG_CHARS` / `SKILL_MAX_BODY_CHARS` / `SKILL_MAX_FILE_BYTES` | `2000` / `8000` / `262144` | Context budgets for the three disclosure levels of a skill |
| `PROMETHEUS_URL` / `PROMETHEUS_TOKEN` | e.g. `http://prometheus:9090` | Ops connector; empty → structured mock with `source: "mock"` |
| `LOKI_URL` / `LOKI_TOKEN` | e.g. `http://loki:3100` | Ops connector; empty → mock fallback |
| `K8S_API_URL` / `K8S_API_TOKEN` / `K8S_VERIFY_TLS` | e.g. `https://kubernetes.default.svc` | Ops connector; empty → mock fallback |

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

`POST /api/v1/auth/register` · `POST /api/v1/auth/login` (dual-token) · `POST /api/v1/auth/refresh` (rotation + reuse detection) · `POST /api/v1/auth/logout` · `GET /api/v1/account/me` · `PATCH /api/v1/account/profile` · `GET /api/v1/admin/accounts` · `POST /api/v1/admin/accounts/{uuid}/roles` — see `backend/README.md` for the full table and `backend/docs/architecture.md` for sequence diagrams. Responses use a `{code, message, data}` envelope.

### Agents (`/api/v1/agents`)

`GET /api/v1/agents?search&status` (cards with ref counts) · `GET /api/v1/agents/catalog` (form options) · `POST /api/v1/agents` · `GET|PATCH /api/v1/agents/{agent_key}` · `POST /api/v1/agents/{agent_key}/status` · `DELETE /api/v1/agents/{agent_key}` — reads need `agent:read`, writes `agent:write` (admin+operator); `supervisor_main` cannot be paused or deleted (409). See `docs/aiops-platform-design.md` §2.

### Skills (`/api/v1/skills`)

`GET /api/v1/skills` lists the skills visible to the caller (`?scope=all` plus `POST /api/v1/skills/reload` require the `skill:admin` permission), `GET /api/v1/skills/{name}` returns the exact body the model receives from `load_skill`, and `GET /api/v1/skills/{name}/files/{path}` serves skill attachments. Skills the caller cannot see are reported as 404 (never 403) so names cannot be enumerated.

## Architecture notes

- **Multi-agent runtime** — `backend/app/agent.py` `get_runtime(session)` builds (and caches, invalidated by agent CRUD) a supervisor→executor graph from the MySQL `agent` registry (`backend/app/models/agent.py`, seeds in migration `e7f9a3b5c1d6` / `migrations/006_agents.sql`): each active executor becomes a sub-agent (`create_agent` with its `tool_refs`/`skill_refs`/`system_prompt`; `edu-agent` additionally carries `PedagogyMiddleware` and a derived checkpointer thread `f"{thread_id}::edu-agent"`), and the supervisor (`supervisor_main`) gets one handoff-as-tool `call_<agent_key>(task)` per active executor plus its own tools. Paused executors get no handoff tool, so pause/resume takes effect on the next request. DB dead/empty falls back to `DEFAULT_AGENT_SPECS` (`app/agent_registry.py`). Conversation memory is persisted by a LangGraph checkpointer: SQLite by default (`CHECKPOINT_BACKEND=sqlite`), PostgreSQL in compose/production (`CHECKPOINT_BACKEND=postgres`). Design: [`docs/aiops-platform-design.md`](docs/aiops-platform-design.md).
- **Mock model** — `MockChatModel` streams canned replies and emits demo tool calls for time/weather/math plus a `load_skill` call for skill questions. Lets the UI run end-to-end with no credentials. Check `GET /api/health` `provider` field to confirm which model is active.
- **Pedagogy kernel** (`backend/app/pedagogy/`, `backend/app/middleware/agent_pedagogy.py`) — a seven-state Feynman loop with four gap types, guardrails (no direct answer / probe budget / monologue cap / talk ratio / escape hatch) and a mastery + spaced-review model, all pure functions with no DB access. The route assembles each turn (`prepare_turn`), the middleware injects the state block and executes the guardrails, and the loop survives across turns in a `pedagogy` LangGraph channel on the existing checkpointer (no new storage). Design: `docs/pedagogy-core.md`, mapping: `docs/education-agent-architecture.md`.
- **Tools** (`backend/app/tools.py` + `backend/app/tools_ops.py`) — `get_current_time`, `calculate` (AST-based safe eval, not `eval`), `get_weather` (mock data) plus three config-driven ops connectors `prometheus_query` / `loki_query` / `k8s_list_resources` that fall back to structured mock data (`source: "mock"`) when their endpoint env is unset or the request fails.
- **Skills** (`backend/skills/`, `backend/app/skills/`) — a skill is a directory with a `SKILL.md` (YAML frontmatter + instructions) plus optional reference files, delivered to the model in three levels of progressive disclosure (catalog in the system prompt → body via `load_skill` → attachments via `read_skill_file`). Visibility reuses the existing RBAC permissions (`public` / `auth` / `permission`), skills hot-reload from disk without a restart, and the same registry backs both the agent and the `/api/v1/skills` endpoints. See `backend/docs/skills-design.md`.
- **SSE format** — OpenAI-style chunks so `@ant-design/x-sdk`'s `DeepSeekChatProvider` parses them directly. On top of `choices[].delta`, each event may carry an `agent` field — tool-chain events (`tool_call`/`tool_result`) plus multi-agent dispatch events (`agent_dispatch`/`agent_return` with `agent_key,name,task,status`) — that the UI renders as a thought chain (「调度 → <agent>」 nodes); clients that only read deltas ignore it. Teaching turns add a `learn` field (`state`/`gap`/`guard`/`attachment`/`note`) with the pedagogy kernel's evidence stream — authenticated + `PEDAGOGY_ENABLED` + a learning-classified turn only, so anonymous and chit-chat streams carry none.
- **Frontend provider** — One `ToolChainChatProvider` (a `DeepSeekChatProvider` subclass that accumulates the `agent` tool-chain and dispatch events) per conversation key, cached in `providerCaches`; the chat page renders them as ThoughtChain nodes in arrival order. `useXChat` manages streaming state, retry, abort. The platform shell (`frontend/src/app/(platform)/layout.tsx`) gates menus by role and brands the UI with `#5b5bd6`.
- **Next.js 16 caveat** — This repo's Next.js has breaking changes vs. prior versions; see `frontend/AGENTS.md` before touching Next.js-specific code.

- **User & auth** — merged into the backend: account/credential/profile split, Redis session registry for refresh tokens (instant revocation on logout/kick/password change), Argon2id hashing, token-bucket rate limiting, RBAC dependencies (`require_permissions`). See `backend/docs/architecture.md`.

See `CLAUDE.md` for the full SSE contract and architecture details.
