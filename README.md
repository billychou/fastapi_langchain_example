# FastAPI LangChain Example

A small full-stack demo: a FastAPI + LangChain agent backend that streams replies over SSE, paired with a Next.js + Ant Design X chat UI that consumes them.

## Layout

- `backend/` — FastAPI + LangChain 1.x / LangGraph agent. Streams OpenAI-style SSE chunks from `POST /api/chat`.
- `frontend/` — Next.js 16 + React 19 + Ant Design X chat UI, wired to the backend via `@ant-design/x-sdk`'s `DeepSeekChatProvider`.

## Quick start

### Backend

```bash
cd backend
uv sync
cp .env.example .env   # then edit LLM_PROVIDER / API keys
uv run uvicorn app.main:app --reload --port 5001
```

Without any API key the server falls back to a deterministic `MockChatModel` — canned replies plus demo tool calls — so the full agent loop (model → tool → model) works out of the box. Try `现在几点了`, `北京天气怎么样`, `计算 12 * (3 + 4)`.

### Frontend

```bash
cd frontend
pnpm install
pnpm dev   # http://localhost:3000
```

The chat posts to `http://127.0.0.1:5001/api/chat` (hardcoded in `frontend/src/app/page.tsx`). Start the backend first.

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

Settings are lru-cached at startup — restart after editing `.env`.

## API

### `GET /api/health`
```json
{ "status": "ok", "provider": "openai", "model": "gpt-4o-mini" }
```

### `POST /api/chat`
Request body (OpenAI-style):
```json
{
  "messages": [{"role": "user", "content": "你是谁"}],
  "conversation_id": "default"
}
```

Response: `text/event-stream`. Each line is `data: {"choices":[{"delta":{"content":"..."}}]}`, terminated by `data: [DONE]`. If the agent raises mid-stream, the error message is appended as a final content delta so it surfaces in the chat bubble. Conversation memory is keyed by `conversation_id` (LangGraph `thread_id`).

## Architecture notes

- **Agent** — `backend/app/agent.py` `get_agent()` (lru-cached): `create_agent(model, tools=ALL_TOOLS, system_prompt, checkpointer=InMemorySaver())`. Multi-turn memory lives in-process; restart clears it.
- **Mock model** — `MockChatModel` streams canned replies and emits demo tool calls for time/weather/math. Lets the UI run end-to-end with no credentials. Check `GET /api/health` `provider` field to confirm which model is active.
- **Tools** (`backend/app/tools.py`) — `get_current_time`, `calculate` (AST-based safe eval, not `eval`), `get_weather` (mock data).
- **SSE format** — OpenAI-style chunks so `@ant-design/x-sdk`'s `DeepSeekChatProvider` parses them directly. Tool calls/results from the agent are not surfaced in the stream; the agent emits a final text message after any tool loop, which is what the UI renders.
- **Frontend provider** — One `DeepSeekChatProvider` per conversation key, cached in `providerCaches`. `useXChat` manages streaming state, retry, abort.
- **Next.js 16 caveat** — This repo's Next.js has breaking changes vs. prior versions; see `frontend/AGENTS.md` before touching Next.js-specific code.

See `CLAUDE.md` for the full SSE contract and architecture details.
