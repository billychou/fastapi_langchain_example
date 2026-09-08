# Backend

FastAPI 一体化后端：LangChain Agent 聊天（SSE）+ 企业级用户与认证系统。

## 功能

- **聊天**：`POST /api/chat` SSE 流式对话（默认要求登录，`CHAT_REQUIRE_AUTH` 控制），
  兼容 OpenAI 风格 `choices[].delta` 协议，并通过顶层 `agent` 字段扩展
  工具链事件（`tool_call`/`tool_result`），前端据此渲染思维链
- **Agent 技能(skills)**：`backend/skills/<name>/SKILL.md` 即一个技能(声明式 frontmatter + 说明书正文 + 可选附件)，
  三级渐进披露(目录常驻系统提示 → `load_skill` 取正文 → `read_skill_file` 取附件)，
  可见性复用 RBAC(`public`/`auth`/`permission`)，磁盘 mtime 指纹热加载(加技能不用重启)，
  详见 `docs/skills-design.md`
- **多身份账号**：account / auth_credential / user_profile 三表解耦，
  单账号可绑定手机号 / 邮箱 / 用户名 / 微信(OAuth2)
- **双 Token 无感续期**：Access(15min) + Refresh(30d, Redis 会话绑定)，
  刷新即轮换 + 重放检测，登出/踢人/改密即时撤销
- **多端策略**：multi_device / single_device(单端互踢)
- **RBAC**：用户 ↔ 角色 ↔ API权限/菜单，`require_permissions(...)` 依赖注入鉴权
- **安全防护**：Argon2id、Redis 令牌桶限流(Lua)、登录失败指数退避锁定、
  PII AES-256-GCM 加密 + HMAC 盲索引 + 脱敏、登录/操作审计日志

## SSE 协议(聊天)

`POST /api/chat` 每行 `data:` 均为 OpenAI 风格 JSON:

```jsonc
// 文本增量(兼容 DeepSeekChatProvider/OpenAI 客户端)
{"choices": [{"delta": {"content": "..."}}]}

// 工具链事件(本项目扩展; 只读 choices[].delta 的客户端会自动忽略)
{"choices": [{"delta": {}}],
 "agent": {"type": "tool_call", "id": "...", "name": "get_current_time", "args": {}}}
{"choices": [{"delta": {}}],
 "agent": {"type": "tool_result", "id": "...", "name": "get_current_time", "result": "…(≤2000字符)"}}
```

流以 `data: [DONE]` 结束; 工具事件按 `id` 配对, 顺序为
`tool_call`(模型节点完成) → `tool_result`(工具节点完成) → 文本增量。

## 日志与可观测性

- 结构化日志: `LOG_FORMAT=auto` 时 dev 输出人类可读格式、production 输出
  单行 JSON(便于 Loki/ELK 采集);
- request-id 贯穿: `X-Request-Id` 请求头(缺省自动生成)经 `contextvars`
  注入同一请求内的所有日志(含 agent/LLM 调用链), 响应头原样回传,
  客户端错误提示亦携带, 便于全链路定位;
- 探针: `GET /live`(存活) 与 `GET /ready`(就绪, 503 附失败依赖清单)。

## 目录结构

```
backend/
├── app/
│   ├── main.py          # 应用工厂: 聊天 + 认证路由 + 中间件/异常信封
│   ├── agent.py         # LangChain Agent(单例: 工具全量注册 + SkillMiddleware + ChatContext)
│   ├── context.py       # ChatContext: 按请求传入的身份/权限(单例 agent 差异化唯一通道)
│   ├── skills/          # 技能子系统: schema/loader/security/registry/tools
│   ├── config.py        # 统一配置(LLM + 认证)
│   ├── deps.py          # get_current / require_permissions
│   ├── db/              # 异步引擎 / Redis 客户端
│   ├── models/          # account / credential / rbac / audit
│   ├── core/            # Argon2id / JWT / AES-GCM+盲索引 / 令牌桶
│   ├── services/        # auth / 会话仓库 / RBAC / 审计
│   ├── middleware/      # RequestId / 安全头 / 全局RBAC(演示) / agent_skills(技能目录注入+工具收敛)
│   ├── api/v1/          # auth / account / admin / skills 路由
│   └── schemas/         # chat / auth / 统一信封
├── skills/              # 技能内容(数据而非代码): trip-planner(公开) / expense-report(权限门禁)
├── migrations/          # 初始建库: MySQL DDL + RBAC 种子 + alembic 版本标记
├── alembic/             # 模式迁移的唯一事实来源(增量变更走这里)
├── docs/architecture.md # 架构图 + 双 Token 时序图
├── docs/skills-design.md # 技能子系统设计(渐进披露/RBAC/安全边界/如何加技能)
└── docker-compose.yml   # 本地 MySQL 8.0 + Redis 7
```

## 快速开始

```bash
cd backend
uv sync
cp .env.example .env       # 填入 LLM key 与 JWT_SECRET_KEY 等

docker compose up -d       # MySQL + Redis(首次启动自动执行 migrations)

uv run uvicorn app.main:app --reload --port 5001
# 开发环境: http://127.0.0.1:5001/docs
```

> 已有 MySQL 实例时需手动执行 `migrations/` 下的 001–004 脚本（004 用于打上
> alembic 版本标记），并按 `.env.example` 调整 `DATABASE_URL`。
>
> **模式变更一律通过 Alembic 增量迁移**（初始建库脚本不再修改）：
>
> ```bash
> uv run alembic revision --autogenerate -m "Add xxx"  # 生成迁移, 人工复核
> uv run alembic upgrade head                          # 应用到数据库
> uv run alembic current                               # 查看当前版本
> ```

## 接口速览

| Method | Path | 说明 | 鉴权 |
|---|---|---|---|
| GET | /api/health | 健康检查 | 匿名 |
| POST | /api/chat | SSE 聊天(自动建档/更新会话元数据) | Access Token |
| GET | /api/v1/threads | 会话列表(按最后对话倒序) | Access Token |
| POST | /api/v1/threads | 新建会话 | Access Token |
| PATCH | /api/v1/threads/{thread_id} | 重命名会话 | Access Token |
| DELETE | /api/v1/threads/{thread_id} | 删除会话(含 checkpoint 清理) | Access Token |
| GET | /api/v1/threads/{thread_id}/messages | 会话历史消息(checkpoint) | Access Token |
| GET | /api/v1/skills | 当前账号可见的技能目录(`?scope=all` 看全量) | Access Token(`all` 需 skill:admin) |
| GET | /api/v1/skills/{name} | 技能详情(body 与 `load_skill` 交给模型的内容一致) | Access Token(按可见性) |
| GET | /api/v1/skills/{name}/files/{path} | 技能附件文本(路径穿越/软链接一律 404) | Access Token(按可见性) |
| POST | /api/v1/skills/reload | 强制热重载技能目录(返回加载失败原因) | skill:admin |
| POST | /api/v1/auth/register | 注册(手机号/邮箱+密码) | 匿名(限流) |
| POST | /api/v1/auth/login | 登录, 返回双 Token | 匿名(限流+锁定) |
| POST | /api/v1/auth/refresh | 无感续期(轮换+重放检测) | Refresh Token |
| POST | /api/v1/auth/logout | 登出(可选 all_devices) | Access |
| POST | /api/v1/auth/password/change | 改密(全端下线) | Access |
| POST | /api/v1/auth/sms/send | 发送验证码 | 匿名(限流) |
| GET | /api/v1/account/me | 当前资料(PII 脱敏) | Access |
| PATCH | /api/v1/account/profile | 更新个人资料(昵称/头像 URL) | Access |
| GET | /api/v1/account/sessions | 在线会话列表 | Access |
| DELETE | /api/v1/account/sessions/{sid} | 下线指定设备 | Access |
| PUT | /api/v1/account/login-policy | 切换登录策略(JSON body: `policy`) | Access |
| GET | /api/v1/admin/accounts | 账号分页列表 | account:read |
| GET | /api/v1/admin/roles | 角色列表 | rbac:read |
| POST | /api/v1/admin/accounts/{uuid}/roles | 全量分配角色 | rbac:assign |

安全上线清单与架构细节见 `docs/architecture.md`。
