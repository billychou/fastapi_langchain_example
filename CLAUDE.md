# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Repository layout

Two independent apps that talk to each other over HTTP:

- `backend/` — Python 3.13 FastAPI + LangChain 1.x + LangGraph 1.x agent that streams replies over SSE.
- `frontend/` — Next.js 16 + React 19 + Ant Design X (`@ant-design/x`, `x-markdown`, `x-sdk`) platform UI: sidebar shell with 仪表盘/AI对话助手/智能体/资产中心/任务中心/可观测中心/配置中心 under `frontend/src/app/(platform)/`.

Each app has its own toolchain and is run from its own directory. There is no monorepo build — the frontend hits the backend directly at `http://127.0.0.1:5001/api/chat` (hardcoded in `frontend/src/app/(platform)/chat/page.tsx`).

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

The SSE stream emits OpenAI-style chunks so the frontend's `DeepSeekChatProvider` (`@ant-design/x-sdk`) can parse them directly: each `data:` line is `{"choices": [{"delta": {"content": "..."}}]}`, terminated by `data: [DONE]`. On top of `choices[].delta`, events may carry a top-level `agent` field with tool-chain events — `{"type": "tool_call"|"tool_result", "id", "name", "args"|"result"}` (result truncated at 2000 chars) — and, since the multi-agent runtime, dispatch events `{"type": "agent_dispatch"|"agent_return", "agent_key", "name", "task", "status"}` emitted around each supervisor→executor handoff; the frontend renders both as a thought chain (「调度 → <agent>」 nodes), and clients that only read deltas ignore the field. Teaching turns add a second top-level field `learn` (same `{"choices": [{"delta": {}}], ...}` envelope) carrying the pedagogy kernel's evidence stream: `{"type": "state"|"gap"|"guard"|"attachment"|"note", "state", "loop_index", "data": {...}}`. `state`/`gap` are emitted **before** the first model token (the route resolves them from the learner's own message), `guard`/`attachment`/`note` after the model finishes. Guardrails run post-stream, so `guard.data.replacement` carries the **full rewritten text** and the client is expected to swap the bubble content — by then the original tokens have already left the server and cannot be recalled (the `agent_threads` preview records the **streamed original**, while the checkpointer stores the rewritten message — the two are deliberately allowed to differ; see `backend/app/api/chat.py`). `learn` only appears for authenticated requests when `PEDAGOGY_ENABLED` is on and the turn is classified as a learning session; anonymous and chit-chat streams contain zero `learn` frames. If the agent raises mid-stream, a sanitized notice (with request-id) is appended as a final content delta so it shows up in the chat bubble.

`backend/app/agent.py` builds a **supervisor→executor multi-agent runtime** from the MySQL `agent` registry via `get_runtime(session)` (cached per process, `invalidate_runtime()` after agent CRUD; falls back to `DEFAULT_AGENT_SPECS` in `app/agent_registry.py` when the DB is dead/empty). Each active executor row becomes `create_agent(model, tools=<resolved tool_refs>, system_prompt, middleware=[SkillMiddleware(skill_refs)])` — `edu-agent` additionally mounts `PedagogyMiddleware` and runs on the derived checkpointer thread `f"{thread_id}::edu-agent"` so the Feynman loop survives across turns; the supervisor (`supervisor_main`) is `create_agent(model, tools=own tool_refs + one handoff-as-tool call_<agent_key>(task) per active executor, middleware=[SkillMiddleware(), DispatchMiddleware()], context_schema=ChatContext, checkpointer=main thread)`. Handoff tools run the executor subgraph and return its final text; executor tokens reach the client only via the `custom` stream lane (`executor_text`) because checkpointer-backed subgraphs leak tokens into the parent messages lane (chat.py suppresses parent text while a handoff is open). `PedagogyMiddleware` (`backend/app/middleware/agent_pedagogy.py`) declares the `pedagogy` LangGraph channel via `state_schema` (`LoopCarrier` serialized to JSON) — no new table, key or TTL. `DispatchMiddleware` (`backend/app/middleware/agent_dispatch.py`) appends a dispatch hint (with a `[dispatch:call_<key>]` machine marker for `MockChatModel`) to the supervisor system prompt on teaching turns only. Both hooks no-op entirely when their `ChatContext` sink/state is absent. The checkpointer is SQLite by default (`CHECKPOINT_BACKEND=sqlite`) or PostgreSQL (`CHECKPOINT_BACKEND=postgres` + `CHECKPOINT_DATABASE_URL`, LangGraph's official `AsyncPostgresSaver`; tables auto-created on first boot) so conversation memory survives restarts and works across replicas. Memory is keyed by `thread_id` from `ChatRequest.conversation_id` (passed through LangGraph's `configurable.thread_id`).

**Mock model fallback**: when `LLM_PROVIDER` is `openai`/`anthropic` but the corresponding API key is missing, `_resolve_model` logs a warning and swaps in `MockChatModel` — a deterministic fake that streams canned replies and emits demo tool calls for time/weather/math queries, plus a `load_skill` call when the message mentions 技能/skill/会做什么. This lets the UI run end-to-end with no credentials. Don't assume a real LLM is in use; check `GET /api/health` `provider` field.

`backend/app/tools.py` defines the base tools (`get_current_time`, `calculate`, `get_weather`); the calculator uses AST-based safe eval (`_safe_eval`) — extend `_ALLOWED_OPERATORS` to add operators; do not switch to `eval`. `backend/app/tools_ops.py` adds the ops connectors `prometheus_query` / `loki_query` / `k8s_list_resources` (httpx against `PROMETHEUS_URL` / `LOKI_URL` / `K8S_API_URL`; unset env or request failure → structured mock with `source: "mock"`). Together they form `ALL_TOOLS` (6 entries), which backs the agents catalog. Tool docstrings are sent to the LLM, so keep them accurate.

### Agent skills

A **skill** is a directory under `backend/skills/` containing `SKILL.md` (YAML frontmatter + Markdown body) plus optional `references/*` attachments — instructions for the model, not executable code. `backend/app/skills/` implements the subsystem: `schema.py` (`SkillManifest`, and `allows()`, the single RBAC gate), `loader.py` (frontmatter parse; a broken skill is skipped with a warning, never fatal at startup), `security.py` (path-traversal/symlink guards, size clamping, `wrap_untrusted` injection boundary), `registry.py` (scan cache + lazy mtime-fingerprint rescan, so new skills need no restart), `tools.py` (`list_skills` / `load_skill` / `read_skill_file`).

Content is delivered in three levels of progressive disclosure: **L1** the catalog (name + description) is injected into the system prompt per request by `SkillMiddleware.awrap_model_call`; **L2** the `SKILL.md` body comes back from `load_skill`; **L3** attachments from `read_skill_file`. Budgets are `SKILL_MAX_CATALOG_CHARS` / `SKILL_MAX_BODY_CHARS` / `SKILL_MAX_FILE_BYTES`.

Because `get_agent()` is a process-wide singleton, per-user differences cannot be baked into the agent: skill tools are registered **in full** at construction (LangChain 1.x forbids middleware adding unregistered tools — `request.override(tools=...)` only accepts a subset), then filtered per request, and identity travels via `context_schema=ChatContext` (`app/context.py`, passed as `context=` from `app/api/chat.py`). Visibility is `public` / `auth` / `permission` (`requires_permissions` are existing RBAC permission codes, e.g. `skill:expense-report:use`, seeded by Alembic migration `9c4a1f7b2d65`). Unknown and forbidden skills are indistinguishable (tool error message / HTTP 404) to prevent name enumeration. Read-only HTTP surface: `GET /api/v1/skills` (`?scope=all` needs `skill:admin`), `GET /api/v1/skills/{name}`, `GET /api/v1/skills/{name}/files/{path}`, `POST /api/v1/skills/reload`. See `backend/docs/skills-design.md` before changing any of this.

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

Platform shell in `frontend/src/app/(platform)/layout.tsx` (role-gated sidebar menu, brand color `#5b5bd6` via ConfigProvider); the chat UI lives in `frontend/src/app/(platform)/chat/page.tsx` (client component) and the agent management page in `frontend/src/app/(platform)/agents/page.tsx` (cards + editor modal over `agentApi` in `src/lib/api.ts`). Built on Ant Design X:
- `useXChat` (`@ant-design/x-sdk`) manages messages, streaming, retry, abort.
- `useXConversations` manages the sidebar conversation list.
- `ToolChainChatProvider` (a `DeepSeekChatProvider` subclass that also accumulates the backend's `agent` tool-chain events into `extraInfo.agentEvents`) + `XRequest('http://127.0.0.1:5001/api/chat', { manual: true })` adapt the backend's SSE stream into the X message protocol. One provider per conversation key, cached in `providerCaches`. Assistant bubble headers render the accumulated steps as a `ThoughtChain` in arrival order (tool_call → loading, matching tool_result → success with collapsible result; agent_dispatch → 「调度 → <name>」 loading, agent_return → success/error).
- Assistant bubbles render markdown via `@ant-design/x-markdown`'s `XMarkdown` with streaming animation tied to `status === 'updating'`.
- `ThinkComponent` is mapped to the `think` markdown tag.

The backend's SSE events (OpenAI-style deltas plus `agent` tool-chain events) are translated by the provider — the frontend code treats `messages`/`isRequesting`/`abort`/`onReload`/`setMessage` as the API surface, not the raw SSE events.

`frontend/src/app/layout.tsx` wraps everything in `AntdRegistry` (Next.js App Router SSR for Ant Design) and brands metadata as AI运维智能平台. `frontend/src/x-markdown/demo/_utils.ts` contains demo helpers (mock fetch stream, markdown theme hook) used by the chat page. `frontend/src/app/(platform)/chat/_utils/local.ts` holds the chat locale strings; unbuilt modules render `PlatformPlaceholder` (`(platform)/_components/`).

Biome config (`frontend/biome.json`): 2-space indent, recommended lint rules + Next.js/React domains, organizes imports. Tailwind CSS 4 is wired through `postcss.config.mjs` (Tailwind directives parsed by Biome).

## Cross-cutting notes

- Backend changes: `uv run ruff check .` + `uv run pytest -q` must pass. Frontend: `pnpm lint` + `pnpm build` (no frontend test runner). Then exercise the chat flow manually (try "现在几点了" for the tool-call loop and "你有什么技能" for the skill tools in mock mode).
- CORS: backend default allows `localhost:3000` and `localhost:5173`. If you change the frontend port, update `CORS_ORIGINS` in `backend/.env` and restart.
- The frontend chat endpoint URL (`127.0.0.1:5001/api/chat`) is hardcoded in `(platform)/chat/page.tsx` — change it there, not in the backend.
- `frontend/CLAUDE.md` is just `@AGENTS.md` — the Next.js 16 caveat lives there.
