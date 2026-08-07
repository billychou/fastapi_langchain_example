# Agent 会话元数据管理（agent_threads 表）实施计划

## Summary

新增 `agent_threads` 表持久化 Agent 会话元数据（thread_id / account_id / title / last_message / created_at / updated_at），提供会话 CRUD API，并把 `/api/chat` 与会话元数据打通（首轮自动建会话并以首条用户消息截断生成标题，每轮结束更新 `last_message`/`updated_at`）。前端侧栏会话列表从本地 mock 切换为后端持久化：列表加载、新建、重命名、删除，以及切换会话时通过 LangGraph checkpoint 恢复历史消息（不新增消息表）。

已确认的决策：用户字段用 `account_id BIGINT UNSIGNED` 外键（跟随仓库约定）；标题取首条用户消息截断（不调 LLM）；历史恢复只读 checkpoint（重启后为空，与现状一致）；**后端冒烟直连本地 MySQL + Redis，配置读取 `backend/.env`，不使用 docker**。

## Execution Steps（执行顺序）

1. **将本计划写入仓库根目录 `implementation_plan.md`**，与用户确认设计无误后再动代码。
2. 后端：migration + ORM model + schemas + service + `/api/v1/threads` 路由 + `/api/chat` 打通。
3. 后端冒烟：将 `003_agent_threads.sql` 手动应用到本地 MySQL（按 `backend/.env` 的 `DATABASE_URL` 连接），本地 Redis 保持运行即可；起 uvicorn（自动读取 `backend/.env`），curl 验证信封与鉴权。
4. 前端：`lib/api.ts` 增加 `threadApi`，改造 `app/page.tsx`，清理 mock 数据，`pnpm lint`。
5. 手动端到端验证（见 Test Plan），更新 `backend/README.md` 接口表。
6. 新建分支 `codex/feat-agent-threads`，按仓库提交规范分逻辑提交（后端 / 前端 / 文档）。

## Key Changes

### 后端 — 数据库与模型
- `backend/migrations/003_agent_threads.sql`：沿用 001/002 风格（`USE fastapi_langchain_example;` + `CREATE TABLE IF NOT EXISTS`）。表结构：
  - `thread_id VARCHAR(64)` 主键（服务端生成 uuid4，与 LangGraph `thread_id` 一致）
  - `account_id BIGINT UNSIGNED NOT NULL` + `fk_threads_account → account(id) ON DELETE CASCADE`
  - `title VARCHAR(255) NOT NULL DEFAULT '新会话'`、`last_message TEXT NULL`
  - `created_at`/`updated_at` 为 `DATETIME`（仓库约定，非 TIMESTAMP），`updated_at` 带 `ON UPDATE CURRENT_TIMESTAMP`
  - 索引：`KEY idx_threads_account_updated (account_id, updated_at DESC)`
- `backend/app/models/agent_thread.py`：`AgentThread` ORM（风格对齐 `models/account.py`，`server_default=func.now()`），并在 `models/__init__.py` 注册导出。

### 后端 — 服务与 API（`{code,message,data}` 信封，全部 `Depends(get_current)`）
- `backend/app/services/thread_service.py`：`list`（按 `updated_at DESC`，limit 100）/ `get`（含属主校验）/ `create` / `rename` / `delete` / `ensure`（不存在则建）/ `record_exchange`（更新 `last_message`=助手回复截断 200 字、显式 `updated_at=now()`；若 `last_message IS NULL` 视为首轮，同时写 `title`=首条用户消息截断 50 字）。DB 写入失败只记日志、不影响聊天主链路。
- `backend/app/api/v1/threads.py`（prefix `/threads`，tags `["threads"]`，注册进 `api/v1/router.py`）：
  - `GET /api/v1/threads` → `ThreadItem[]`（`thread_id,title,last_message,created_at,updated_at`）
  - `POST /api/v1/threads` → 建会话（body 可选 `title`，默认「新会话」），返回 `ThreadItem`
  - `PATCH /api/v1/threads/{thread_id}` → 重命名（1–255 字）
  - `DELETE /api/v1/threads/{thread_id}` → 删行 + best-effort `checkpointer.adelete_thread()`（try/except 包裹）
  - `GET /api/v1/threads/{thread_id}/messages` → `agent.aget_state(config)` 读 checkpoint，映射为 `[{role, content}]`：Human→user；AI→assistant（跳过空内容的纯 tool-call 消息）；跳过 System/Tool 消息；无 checkpoint 返回 `[]`
  - 访问他人/不存在的 thread 一律 404（BizCode.NOT_FOUND，不泄露存在性）
- `backend/app/main.py` 与 `app/deps.py`：
  - 新增 `get_current_optional` 依赖：`CHAT_REQUIRE_AUTH=true` 时等价 `get_current`，否则返回 `None`；`/api/chat` 改用它注入 `ctx`（替代现有 `dependencies` 列表，语义不变）。
  - `chat_events`：流式前若 `ctx` 存在则 `ensure` 会话行（`conversation_id` 长度 >64 → 400）；流式过程中累积助手全文，成功结束后调 `record_exchange`（首条用户消息取 `request.messages` 中最后一个 user 消息）。匿名/降级模式不做任何元数据写入。

### 前端
- `frontend/src/lib/api.ts`：新增 `ThreadItem`/`ThreadMessage` 类型与 `threadApi`（list/create/rename/remove/messages），复用现有 `request<T>` 信封封装。
- `frontend/src/app/page.tsx`：
  - 会话列表改为挂载后 `threadApi.list()` 加载（`key=thread_id, label=title`，后端已按 `updated_at DESC` 排序，去掉 group）；列表为空时自动 `create()` 一个会话并激活，保证始终有 activeKey。
  - 「新建会话」按钮改为先 `threadApi.create()` 再 `addConversation`（key 用返回的 `thread_id`）。
  - 发送链路（利用 x-sdk `transformParams` 会把 `onRequest` 入参合入 body 的机制）：`onRequest({ messages: [最新一条 user 消息], conversation_id: activeKey })`；并在 provider 的 `middlewares.onRequest` 里把 body 中 `messages` 只保留最后一条、强制注入 `conversation_id`（provider 按 conversationKey 缓存，闭包捕获 key）。**这同时修复了现状「全量历史 + checkpointer 导致记忆重复累积」的问题。**
  - 历史恢复：`useXChat` 的 `defaultMessages` 传异步函数 `({conversationKey}) => threadApi.messages(...)` 映射为 `{id, message:{role,content}, status:'success'}`，加载期间用 `isDefaultMessagesRequesting` 显示 Spin；移除 `historyMessageFactory`/静态 `HISTORY_MESSAGES` 用法。
  - 重命名：补全菜单项 onClick，弹一个小 Modal（Input + 确认）调 `threadApi.rename`，成功后 `setConversations` 更新 label。
  - 删除：现有 onClick 内追加 `threadApi.remove()`；删空后自动新建一个会话。
  - 首轮发送后若当前会话 label 仍为「新会话」，本地乐观更新为「用户消息截断 50 字」（与服务端标题规则一致），避免额外轮询。
  - 清理 `frontend/src/app/_utils/config.tsx` 中不再使用的 `DEFAULT_CONVERSATIONS_ITEMS`/`HISTORY_MESSAGES`。

### 文档
- `backend/README.md` 接口速览表补充 5 个 threads 接口；提示已有 MySQL 实例需手动执行 `003_agent_threads.sql`。

## Test Plan（无测试套件，按 AGENTS.md 手动验证）

1. 将 `003_agent_threads.sql` 应用到本地 MySQL（按 `backend/.env` 的 `DATABASE_URL` 连接）；`SHOW TABLES` 含 `agent_threads`。
2. curl：未带 token `GET /api/v1/threads` → 401 信封；注册登录后 CRUD 全流程；访问他人 thread_id → 404。
3. UI 登录后：新账号自动出现一个会话；发送「现在几点了」mock 工具链路正常；DB 中该行 `title`=消息截断、`last_message`=回复预览、`updated_at` 更新。
4. 同会话连续多轮：上下文正确且无重复累积；新建会话互不串扰；切换会话历史消息从 checkpoint 恢复。
5. 重命名/删除生效且刷新页面后保持；删除当前会话后自动切换/新建。
6. 重启后端：会话列表仍在；打开旧会话历史为空（InMemorySaver 已清，符合预期）。
7. `pnpm lint` 通过；`CHAT_REQUIRE_AUTH=false` 时 `/api/chat` 仍可匿名使用且不写元数据。

## Assumptions

1. 交付物文件为仓库根目录 `/Users/songchuan.zhou/Src/fastapi_langchain_example/implementation_plan.md`，内容与本计划一致。
2. 表名定为 `agent_threads`（交互流程图中的 "sessions" 仅为草稿笔误）。
3. LangGraph checkpointer 维持 `InMemorySaver` 不变；「重启后对话记忆丢失、仅元数据留存」为既有行为，不在本次范围（未来可换 MySQL checkpointer）。
4. 后端冒烟不使用 docker：直连本地 MySQL 与 Redis，连接配置以 `backend/.env` 的 `DATABASE_URL` / `REDIS_URL` 为准；新 migration 需手动执行到本地库（docker-compose 的 entrypoint 自动执行机制不适用）。
5. `last_message` 每轮都写「助手最新回复」预览；`updated_at` 在每轮成功回复后更新（失败轮次不更新）。
6. 不做分页（limit 100 足够 v1）、不做置顶/分组、不做消息表。
