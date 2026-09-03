# Repository Guidelines

FastAPI + LangChain agent backend (SSE chat, dual-token JWT auth + RBAC) with a Next.js 16 + Ant Design X chat UI.

## Project Structure & Module Organization

- `backend/app/` — FastAPI source: `main.py`, `agent.py`, `tools.py`, `config.py`, plus `core/`, `services/`, `models/`, `schemas/`, `api/v1/`, `db/`, `middleware/`.
- `backend/migrations/` — fresh-database bootstrap SQL (DDL + RBAC seed + alembic version stamp). `backend/alembic/` — Alembic migrations; all schema changes after bootstrap go here (`uv run alembic revision --autogenerate`, then `uv run alembic upgrade head`). `backend/docker-compose.yml` — local MySQL 8 + Redis 7. `backend/docs/architecture.md` — diagrams and token flows.
- `frontend/src/app/` — App Router pages: `page.tsx` (chat), `login/`, `register/`, `users/`. `frontend/src/lib/` — auth context, API client. `frontend/public/` — static assets.

## Build, Test, and Development Commands

Backend (Python 3.13, `uv`, from `backend/`):

- `uv sync` — install dependencies; `cp .env.example .env` — then set LLM keys and `JWT_SECRET_KEY`.
- `docker compose up -d` — MySQL + Redis; migrations auto-run on first boot.
- `uv run uvicorn app.main:app --reload --port 5001` — run the API (`/docs` for Swagger).

Frontend (`pnpm` 10, from `frontend/`):

- `pnpm install` — install; `pnpm dev` — http://localhost:3000 (backend must be running first); `pnpm build` — production build.

## Coding Style & Naming Conventions

- Frontend: Biome (`frontend/biome.json`) — 2-space indent, recommended rules with Next.js/React domains, organized imports. Run `pnpm lint` / `pnpm format` before committing. Tailwind CSS 4 via PostCSS.
- Backend: no linter configured; follow existing conventions — async SQLAlchemy, Pydantic settings in `config.py`, responses in the `{code, message, data}` envelope.
- snake_case for Python, camelCase for TypeScript.

## Testing Guidelines

Backend: `uv run pytest -q` (69 tests; `tests/conftest.py` freezes MySQL/Redis/LLM to mocks/dead ports, no external deps needed) + `uv run ruff check .`. CI (`.github/workflows/ci.yml`) runs backend ruff+pytest and frontend lint+build, and rejects mirror URLs in `uv.lock` (regenerate with `UV_DEFAULT_INDEX=https://pypi.org/simple/ uv lock`). Frontend has no test runner — verify with `pnpm lint` + `pnpm build`, then run both apps and exercise the chat flow — try "现在几点了" to test the tool-call loop (and the tool-chain thought-chain UI) in mock mode. Check the active model via `provider` on `GET /api/health`.

## Commit & Pull Request Guidelines

Commit subjects are imperative, capitalized, unprefixed (e.g. `Add login and registration pages`, `Fix CORS default for dev server`). Branches follow `feat/`, `refactor/`, `chore/`, `docs/` patterns and merge via pull request; PRs should describe the change and how it was verified.

## Security & Configuration Notes

- Backend reads `backend/.env`, caching settings at startup — restart after edits. Never commit real keys; `.env` is gitignored.
- Without an LLM key the server falls back to `MockChatModel`, so the agent loop works credential-free. Conversation memory persists via the LangGraph checkpointer: `CHECKPOINT_BACKEND=sqlite` (default, local file) or `postgres` (requires `CHECKPOINT_DATABASE_URL`; the root `docker-compose.yml` ships a Postgres 16 service for it).
- Keep `CORS_ORIGINS` in sync with the frontend port. The chat URL (`http://127.0.0.1:5001/api/chat`) is hardcoded in `frontend/src/app/page.tsx` — change it there.
- This dev machine exports `UV_DEFAULT_INDEX` pointing at a China mirror. Any `uv` command (`uv sync`, `uv lock`, and also `uv run`, which re-locks on the fly) will silently rewrite `backend/uv.lock` URLs to the mirror unless prefixed with `UV_DEFAULT_INDEX=https://pypi.org/simple/`. CI rejects mirror-polluted lockfiles, so always run uv with that prefix here.

## Agent-Specific Instructions

- `frontend/AGENTS.md` warns that this repo's Next.js 16 has breaking changes — consult `frontend/node_modules/next/dist/docs/` before writing Next.js code, and don't delete its auto-regenerated block.
- See `backend/README.md` for the full auth API table.
