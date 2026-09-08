# Repository Guidelines

FastAPI + LangChain agent backend (SSE chat, dual-token JWT auth + RBAC) with a Next.js 16 + Ant Design X chat UI.

**Non-negotiable delivery rule:** every feature or fix you add must ship with tests, pass the checks below, and land as its own commit before you start the next one. See [Feature Delivery Workflow](#feature-delivery-workflow-test--commit-mandatory).

## Project Structure & Module Organization

- `backend/app/` — FastAPI source: `main.py`, `agent.py`, `tools.py`, `config.py`, plus `core/`, `services/`, `models/`, `schemas/`, `api/v1/`, `db/`, `middleware/`.
- `backend/tests/` — pytest suite (`conftest.py` freezes MySQL/Redis/LLM to mocks and dead ports). New backend behaviour gets a test file here.
- `backend/migrations/` — fresh-database bootstrap SQL (DDL + RBAC seed + alembic version stamp). `backend/alembic/` — Alembic migrations; all schema changes after bootstrap go here (`uv run alembic revision --autogenerate`, then `uv run alembic upgrade head`). `backend/docker-compose.yml` — local MySQL 8 + Redis 7. `backend/docs/architecture.md` — diagrams and token flows.
- `frontend/src/app/` — App Router pages: `page.tsx` (chat), `login/`, `register/`, `users/`. `frontend/src/lib/` — auth context, API client. `frontend/public/` — static assets.

## Build, Test, and Development Commands

Backend (Python 3.13, `uv`, from `backend/`) — on this machine always prefix `uv` with the public index (see Security & Configuration Notes):

- `UV_DEFAULT_INDEX=https://pypi.org/simple/ uv sync` — install dependencies; `cp .env.example .env` — then set LLM keys and `JWT_SECRET_KEY`.
- `docker compose up -d` — MySQL + Redis; migrations auto-run on first boot.
- `uv run uvicorn app.main:app --reload --port 5001` — run the API (`/docs` for Swagger).
- `uv run ruff check .` and `uv run pytest -q` — lint + tests (69 tests today; keep the suite green).

Frontend (`pnpm` 10, from `frontend/`):

- `pnpm install` — install; `pnpm dev` — http://localhost:3000 (backend must be running first); `pnpm build` — production build.
- `pnpm lint` (Biome check) / `pnpm format` — lint and format.

## Feature Delivery Workflow (test → commit, mandatory)

Work in small increments: implement one feature (or one fix), verify it, commit it, then move on. Never batch several features into one commit, and never leave finished work uncommitted while you start something new.

1. **Write the test together with the feature.** Backend: add or extend a case in `backend/tests/` — a feature without a test is not done. Frontend: there is no test runner, so the "test" is `pnpm lint` + `pnpm build` plus a manual pass over the affected flow.
2. **Run the checks for everything you touched** (both sets if you touched both apps):
   - backend, from `backend/`: `uv run ruff check .`, then `uv run pytest -q`
   - frontend, from `frontend/`: `pnpm lint`, then `pnpm build`
3. **Stage only your own files** — `git add <paths>`, never `git add -A`. Keep `.env`, `*.sqlite*` checkpointer files, logs, and `.DS_Store` out; confirm with `git status` and `git diff --cached`.
4. **Commit automatically as soon as the checks are green**, using the repo subject style (imperative, capitalized, unprefixed):

   ```bash
   git add backend/app/tools.py backend/tests/test_tools.py
   git commit -m "Add exchange-rate tool with tests"
   ```

5. **If a check fails: fix it and re-run.** Do not commit red, do not bypass hooks with `--no-verify`, and do not delete or `skip` a failing test just to get past it.
6. **If you changed dependencies**, re-lock with `UV_DEFAULT_INDEX=https://pypi.org/simple/ uv lock` and verify the lockfile is mirror-free (`grep -iE 'aliyun|tsinghua|douban' backend/uv.lock` must print nothing) before committing — CI rejects mirror URLs.
7. **Push and open the PR when the task calls for review**; otherwise leave the commit on the local working branch. Never force-push shared branches and never amend a commit that is already pushed.
8. **Update the docs in the same commit** when the change makes them stale (`README.md`, `backend/README.md`, `backend/docs/architecture.md`, this file).

One working feature = one commit (or a short series of green commits). If you are mid-way through a larger change, commit at each point where the checks pass rather than accumulating a giant diff.

## Coding Style & Naming Conventions

- Frontend: Biome (`frontend/biome.json`) — 2-space indent, recommended rules with Next.js/React domains, organized imports. Run `pnpm lint` / `pnpm format` before committing. Tailwind CSS 4 via PostCSS.
- Backend: Ruff is configured in `backend/pyproject.toml` — `line-length = 100`, rules `E,W,F,I,B,UP`, `E501` ignored, `fastapi.Depends` / `app.deps.require_permissions` whitelisted for B008. Follow existing conventions — async SQLAlchemy, Pydantic settings in `config.py`, responses in the `{code, message, data}` envelope.
- snake_case for Python, camelCase for TypeScript.

## Testing Guidelines

Backend: `uv run pytest -q` (69 tests; `tests/conftest.py` freezes MySQL/Redis/LLM to mocks/dead ports, no external deps needed) + `uv run ruff check .`. Cover new endpoints, tools, and auth/session logic with tests in the matching `backend/tests/test_*.py`; reuse the existing fixtures instead of reaching for real services. CI (`.github/workflows/ci.yml`) runs `ruff check` + `pytest` for the backend and `biome check` for the frontend (the frontend job does **not** run `pnpm build`, so build locally before committing), and rejects mirror URLs in `uv.lock` (regenerate with `UV_DEFAULT_INDEX=https://pypi.org/simple/ uv lock`). Frontend has no test runner — verify with `pnpm lint` + `pnpm build`, then run both apps and exercise the chat flow — try "现在几点了" to test the tool-call loop (and the tool-chain thought-chain UI) in mock mode. Check the active model via `provider` on `GET /api/health`.

## Commit & Pull Request Guidelines

Commit subjects are imperative, capitalized, unprefixed (e.g. `Add login and registration pages`, `Fix CORS default for dev server`); a few recent commits use `type(scope): 中文描述` — prefer the imperative English style unless the user asks otherwise. Use the body for the why, and mention the verification you ran (`ruff`/`pytest`/`lint`/`build`). Branches follow `feat/`, `refactor/`, `chore/`, `docs/` patterns — agent work in this repo lands on `codex/`-prefixed branches — and merge via pull request; PRs should describe the change and how it was verified.

## Security & Configuration Notes

- Backend reads `backend/.env`, caching settings at startup — restart after edits. Never commit real keys; `.env` is gitignored.
- Without an LLM key the server falls back to `MockChatModel`, so the agent loop works credential-free. Conversation memory persists via the LangGraph checkpointer: `CHECKPOINT_BACKEND=sqlite` (default, local file — gitignored, never commit it) or `postgres` (requires `CHECKPOINT_DATABASE_URL`; the root `docker-compose.yml` ships a Postgres 16 service for it).
- Keep `CORS_ORIGINS` in sync with the frontend port. The chat URL (`http://127.0.0.1:5001/api/chat`) is hardcoded in `frontend/src/app/page.tsx` — change it there.
- This dev machine exports `UV_DEFAULT_INDEX` pointing at a China mirror. Any `uv` command (`uv sync`, `uv lock`, and also `uv run`, which re-locks on the fly) will silently rewrite `backend/uv.lock` URLs to the mirror unless prefixed with `UV_DEFAULT_INDEX=https://pypi.org/simple/`. CI rejects mirror-polluted lockfiles, so always run uv with that prefix here — and check `git diff --cached backend/uv.lock` before every commit.

## Agent-Specific Instructions

- `frontend/AGENTS.md` warns that this repo's Next.js 16 has breaking changes — consult `frontend/node_modules/next/dist/docs/` before writing Next.js code, and don't delete its auto-regenerated block.
- `CLAUDE.md` holds the longer architecture walkthrough (SSE event shape, agent/tool internals, auth service); keep it in sync when you change those areas.
- See `backend/README.md` for the full auth API table.
