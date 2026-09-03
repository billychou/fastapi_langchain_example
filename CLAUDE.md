# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Repository layout

Two independent apps that talk to each other over HTTP:

- `backend/` — Python 3.13 FastAPI + LangChain 1.x + LangGraph 1.x agent that streams replies over SSE.
- `frontend/` — Next.js 16 + React 19 + Ant Design X (`@ant-design/x`, `x-markdown`, `x-sdk`) chat UI.

Each app has its own toolchain and is run from its own directory. There is no monorepo build — the frontend hits the backend directly at `http://127.0.0.1:5001/api/chat` (hardcoded in `frontend/src/app/page.tsx`).

## Backend

Toolchain: `uv` for env/dependency management. Python 3.13 (see `backend/.python-version`).

```bash
cd backend
uv sync                  # install deps from uv.lock
cp .env.example .env     # then edit .env to set LLM_PROVIDER / API keys
uv run uvicorn app.main:app --reload --port 5001   # start dev server
# or: uv run python -m app.main  (runs main.py which calls uvicorn on 0.0.0.0:5001)
```

Tests: `uv run pytest -q` (pytest + httpx TestClient; external deps are frozen to dead ports/mocks in `tests/conftest.py`, so no MySQL/Redis/LLM needed). Lint: `uv run ruff check .`. CI (`.github/workflows/ci.yml`) runs ruff + pytest for the backend and lint + build for the frontend; it rejects mirror URLs in `uv.lock` — if you touch dependencies, regenerate the lock with `UV_DEFAULT_INDEX=https://pypi.org/simple/ uv lock`.

### User & auth service (merged into backend)

`backend/` also hosts an enterprise user & auth system (formerly standalone `auth_service/`): multi-identity accounts (phone/email/username/OAuth credentials decoupled from the account master table), dual-token JWT (15min Access + 30d Refresh with Redis-backed session registry, rotation + reuse detection), RBAC (user ↔ role ↔ permission/menu, `require_permissions(...)` dependencies), Argon2id password hashing, Redis token-bucket rate limiting, exponential login lockout, and PII field encryption (AES-256-GCM + HMAC blind indexes).

- Auth routes live under `/api/v1` (auth / account / admin); chat (`/api/chat`) requires a valid Access Token when `CHAT_REQUIRE_AUTH=true` (default).
- Local dev stack: `cd backend && docker compose up -d` (MySQL 8 + Redis 7; migrations auto-run on first boot). DDL and RBAC seeds are in `backend/migrations/`.
- Config: `backend/app/config.py` merges LLM + auth settings; auth keys (`JWT_SECRET_KEY`, `PII_KEYS`, `PII_BLIND_INDEX_KEY`) are required for login to work — see `backend/.env.example`.

### Backend architecture

`backend/app/main.py` is the FastAPI entrypoint. Key endpoints:
- `GET /api/health` — returns provider + model.
- `GET /live` / `GET /ready` — liveness / readiness probes (ready returns 503 with the failing-dependency list; also used by the Docker HEALTHCHECK).
- `POST /api/chat` — `StreamingResponse` with `media_type="text/event-stream"`.

The SSE stream emits OpenAI-style chunks so the frontend's `DeepSeekChatProvider` (`@ant-design/x-sdk`) can parse them directly: each `data:` line is `{"choices": [{"delta": {"content": "..."}}]}`, terminated by `data: [DONE]`. On top of `choices[].delta`, events may carry a top-level `agent` field with tool-chain events — `{"type": "tool_call"|"tool_result", "id", "name", "args"|"result"}` (result truncated at 2000 chars); the frontend renders them as a thought chain, and clients that only read deltas ignore the field. If the agent raises mid-stream, a sanitized notice (with request-id) is appended as a final content delta so it shows up in the chat bubble.

`backend/app/agent.py` builds the agent once via `get_agent()` (lru-cached). Construction order: `build_chat_model` (or `MockChatModel` fallback) → `create_agent(model, tools=ALL_TOOLS, system_prompt, checkpointer)`. The checkpointer is SQLite by default (`CHECKPOINT_BACKEND=sqlite`) or PostgreSQL (`CHECKPOINT_BACKEND=postgres` + `CHECKPOINT_DATABASE_URL`, LangGraph's official `AsyncPostgresSaver`; tables auto-created on first boot) so conversation memory survives restarts and works across replicas. Memory is keyed by `thread_id` from `ChatRequest.conversation_id` (passed through LangGraph's `configurable.thread_id`).

**Mock model fallback**: when `LLM_PROVIDER` is `openai`/`anthropic` but the corresponding API key is missing, `_resolve_model` logs a warning and swaps in `MockChatModel` — a deterministic fake that streams canned replies and emits demo tool calls for time/weather/math queries. This lets the UI run end-to-end with no credentials. Don't assume a real LLM is in use; check `GET /api/health` `provider` field.

`backend/app/tools.py` defines `ALL_TOOLS` (`get_current_time`, `calculate`, `get_weather`). The calculator uses AST-based safe eval (`_safe_eval`) — extend `_ALLOWED_OPERATORS` to add operators; do not switch to `eval`. Tool docstrings are sent to the LLM, so keep them accurate.

`backend/app/config.py` uses `pydantic-settings` reading from `backend/.env`. `get_settings()` is lru-cached, so env changes require a restart. `CORS_ORIGINS` is comma-separated; the default list includes `localhost:3000` (Next.js) and `localhost:5173`.

`backend/app/schemas.py`: `ChatRequest` has `messages: list[MessageType]` (OpenAI-style: each `MessageType` has `role` and `content`) and `conversation_id` (defaults to `"default"`). This shape matches what `@ant-design/x-sdk`'s `DeepSeekChatProvider` sends — the frontend's `onRequest({ messages: [{ role: 'user', content: val }] })` lands in the schema directly. `main.py:_to_langchain_messages` maps each item by `role` → `HumanMessage` / `AIMessage` / `SystemMessage` (default `HumanMessage`) before passing to the agent.

## Frontend

Toolchain: `pnpm` 10 + Next.js 16 + Biome. **Next.js 16 in this repo has breaking changes vs. what you may know** — see `frontend/AGENTS.md`: read the relevant guide under `frontend/node_modules/next/dist/docs/` before writing Next.js code, and don't remove the `AGENTS.md` block (it's auto-regenerated by `next dev`).

```bash
cd frontend
pnpm install
pnpm dev       # http://localhost:3000
pnpm build
pnpm lint      # biome check
pnpm format   # biome format --write
```

### Frontend architecture

Single-page chat UI in `frontend/src/app/page.tsx` (client component). Built on Ant Design X:
- `useXChat` (`@ant-design/x-sdk`) manages messages, streaming, retry, abort.
- `useXConversations` manages the sidebar conversation list.
- `ToolChainChatProvider` (a `DeepSeekChatProvider` subclass that also accumulates the backend's `agent` tool-chain events into `extraInfo.agentEvents`) + `XRequest('http://127.0.0.1:5001/api/chat', { manual: true })` adapt the backend's SSE stream into the X message protocol. One provider per conversation key, cached in `providerCaches`. Assistant bubble headers render the accumulated tool steps as a `ThoughtChain` (tool_call → loading, matching tool_result → success with collapsible result).
- Assistant bubbles render markdown via `@ant-design/x-markdown`'s `XMarkdown` with streaming animation tied to `status === 'updating'`.
- `ThinkComponent` is mapped to the `think` markdown tag.

The backend's SSE events (OpenAI-style deltas plus `agent` tool-chain events) are translated by the provider — the frontend code treats `messages`/`isRequesting`/`abort`/`onReload`/`setMessage` as the API surface, not the raw SSE events.

`frontend/src/app/layout.tsx` wraps everything in `AntdRegistry` (Next.js App Router SSR for Ant Design). `frontend/src/x-markdown/demo/_utils.ts` contains demo helpers (mock fetch stream, markdown theme hook) used by `page.tsx`. `frontend/src/app/_utils/local.ts` holds the locale strings used throughout the UI.

Biome config (`frontend/biome.json`): 2-space indent, recommended lint rules + Next.js/React domains, organizes imports. Tailwind CSS 4 is wired through `postcss.config.mjs` (Tailwind directives parsed by Biome).

## Cross-cutting notes

- Backend changes: `uv run ruff check .` + `uv run pytest -q` must pass. Frontend: `pnpm lint` + `pnpm build` (no frontend test runner). Then exercise the chat flow manually (try "现在几点了" to test the tool-call loop in mock mode).
- CORS: backend default allows `localhost:3000` and `localhost:5173`. If you change the frontend port, update `CORS_ORIGINS` in `backend/.env` and restart.
- The frontend chat endpoint URL (`127.0.0.1:5001/api/chat`) is hardcoded in `page.tsx` — change it there, not in the backend.
- `frontend/CLAUDE.md` is just `@AGENTS.md` — the Next.js 16 caveat lives there.
