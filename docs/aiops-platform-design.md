# AI 运维智能平台 — 平台设计(第一期: 外壳 + 智能体注册表 + 多智能体运行时)

> 状态: **第一期已落地**(2026-10-16)。本文描述**已实现的平台骨架与其设计契约**,
> 以及下一阶段的演进规划; 与 `docs/` 下教育智能体系列文档并行, 互不替代
> (教学内核作为 `edu-agent` 执行智能体挂在本文的多智能体运行时之下)。

## 1. 定位与模块地图

把「单页聊天演示」升级为企业级 AI 运维智能平台: 统一的平台外壳(侧边栏七大模块)
+ 数据库驱动的智能体注册表 + LangGraph supervisor→executor 多智能体调度。
第一期只实现 **外壳、智能体管理、AI 对话助手** 三个模块, 其余模块路由与菜单先就位、
页面占位(`PlatformPlaceholder`), 按第 8 节路线图分阶段落地。

| 模块 | 路由 | 第一期状态 |
|---|---|---|
| 仪表盘 | `/dashboard` | 占位(平台运行总览, 下一阶段) |
| AI 对话助手 | `/chat` | 已实现(原首页聊天迁入, 含调度链可视化) |
| 智能体 | `/agents` | 已实现(卡片管理页, 见 §2/§7) |
| 资产中心-本地工具 | `/assets/tools` | 占位(工具目录已存在于后端 `ALL_TOOLS`) |
| 资产中心-MCP 远端工具 | `/assets/mcp` | 占位(本期仅 `mcp_refs` 建模与计数) |
| 资产中心-知识库 | `/assets/knowledge` | 占位(本期仅 `rag_refs` 建模与计数) |
| 资产中心-技能 | `/assets/skills` | 占位(技能子系统已存在, 管理界面下一阶段) |
| 任务中心 | `/tasks` · `/tasks/scheduled` | 占位 |
| 可观测中心 | `/observability` | 占位 |
| 配置中心-用户管理 | `/settings/users` | 已实现(原 `/users` 迁入) |
| 配置中心-个人设置 | `/settings/profile` | 已实现(原 `/settings` 迁入) |
| 配置中心-模型/凭证 | `/settings/models` · `/settings/credentials` | 占位(当前经 `backend/.env`) |
| 登录 / 注册 | `/login` · `/register` | 壳外, 品牌化卡片, JWT 流程不变 |

外壳实现: `frontend/src/app/(platform)/layout.tsx`(固定侧边栏 + 右上账号菜单),
菜单按角色 gating(用户管理仅 admin; 模型/凭证配置 admin+operator),
品牌色 `#5b5bd6` 经 `ConfigProvider` 注入; 根路由 `/` 重定向到 `/dashboard`。

## 2. 智能体注册表(MySQL `agent` 表)

| 列 | 类型 | 说明 |
|---|---|---|
| `id` | BIGINT PK | 自增 |
| `agent_key` | VARCHAR(64) UNIQUE | 机器标识, `^[a-z0-9][a-z0-9_-]{0,63}$`, 不可改 |
| `name` | VARCHAR(64) | 展示名(卡片标题) |
| `role` | ENUM(`supervisor`,`executor`) | 总管 / 执行; 创建后不可改 |
| `status` | ENUM(`active`,`paused`) | 启停; 暂停的执行智能体不获得 handoff 工具 |
| `description` | VARCHAR(500) | 卡片描述 |
| `system_prompt` | TEXT | 子图系统提示词 |
| `tool_refs` / `skill_refs` / `mcp_refs` / `rag_refs` | JSON 数组 | 本地工具 / 技能 / MCP / RAG 引用; 后两者本期仅计数 |
| `sort_order` | INT | 卡片排序 |
| `created_at` / `updated_at` | DATETIME | 审计时间 |

迁移双轨(沿用 `005_learning.sql` 先例): Alembic revision `e7f9a3b5c1d6` +
fresh-DB  bootstrap `backend/migrations/006_agents.sql`(DDL + 智能体种子 + 权限种子 +
alembic stamp), 两者由 `tests/test_migrations.py` 对账防漂移。

种子智能体(与设计稿一致; **计数由 refs 长度算出, 不硬编码**):

| agent_key | role | status | refs 来源 |
|---|---|---|---|
| `supervisor_main` | supervisor | active | 本地工具 + 可见技能(总管自身能力) |
| `k8s-agent` | executor | active | K8s/Prometheus/Loki 连接器组合 |
| `edu-agent` | executor | **paused** | 教学技能 + 本地工具(教学内核载体) |
| `rag-agent` | executor | active | `rag_refs` 占位引用 |
| `metric-agent` | executor | active | 连接器组合(指标分析) |
| `log-agent` | executor | active | 连接器组合(日志分析) |

RBAC: `agent:read`(全部角色) / `agent:write`(admin 通配 + operator)。
API(`backend/app/api/v1/agents.py`, 信封 `{code,message,data}`):

| 方法 | 路径 | 权限 | 说明 |
|---|---|---|---|
| GET | `/api/v1/agents?search&status` | read | 名称/ID 模糊搜索 + 状态筛选; 返回 `counts` |
| GET | `/api/v1/agents/catalog` | read | 表单目录: 本地工具全量 + 调用者可见技能 |
| POST | `/api/v1/agents` | write | 新建; 未知 tool/skill 引用 → 422 |
| GET/PATCH | `/api/v1/agents/{agent_key}` | read / write | 详情 / 编辑(role、key 不可变) |
| POST | `/api/v1/agents/{agent_key}/status` | write | 启停; `supervisor_main` 暂停 → 409 |
| DELETE | `/api/v1/agents/{agent_key}` | write | 删除; `supervisor_main` → 409 |

写操作落审计日志(`agent_create/agent_update/agent_status/agent_delete`),
并调用 `invalidate_runtime()` 使启停/增删**即时生效**于下一次调度。

## 3. 多智能体运行时(supervisor → executor)

`backend/app/agent.py` 启动/首次请求时从 DB 构建运行时并缓存
(`get_runtime(session)`, 每事件循环一把构建锁; DB 不可用或空表回退
`app/agent_registry.py` 的 `DEFAULT_AGENT_SPECS`, 保证无凭证/无库也能跑):

- 每个 **active executor** → `create_agent(model, tools=tool_refs 解析结果,
  system_prompt, middleware=[SkillMiddleware(skill_refs)])`;
  `edu-agent` 额外挂 `PedagogyMiddleware` 并使用**派生 thread**
  `f"{thread_id}::edu-agent"` 的 checkpointer, 使费曼教学循环跨轮存活;
  其余 executor 每次调用无状态。
- **supervisor** → `create_agent(model, tools=自身 tool_refs + 每个 active executor
  一个 handoff 工具, middleware=[SkillMiddleware, DispatchMiddleware],
  checkpointer=主线程)`。handoff 采用 **handoff-as-tools** 模式(不引新依赖):
  动态生成 `call_<agent_key>(task)` 工具, 内部跑 executor 子图并返回其最终文本。
- 暂停的 executor **不生成** handoff 工具; `supervisor_main` 恒为入口
  (`POST /api/chat` 只与总管对话)。
- 教学路由: `DispatchMiddleware` 在教学回合往总管系统提示追加调度指令
  (含机器标记 `[dispatch:call_edu-agent]`, `MockChatModel` 据此确定性复现调度,
  真实模型读自然语言部分); 非教学回合零改动。

## 4. 调度时序

```
用户 ── POST /api/chat ──▶ chat.py ──▶ supervisor(主线程 checkpointer)
   │                                    │  model 决定 call_k8s-agent(task)
   │                                    ├─ emit agent_dispatch{agent_key,name,task}
   │                                    ├─ handoff 工具内跑 executor 子图
   │                                    │     └─ executor token 经 custom lane
   │                                    │        (executor_text) 转发为正文 delta
   │                                    ├─ emit agent_return{...,status=success|error}
   │                                    └─  supervisor 汇总 → 正文 delta
   ◀── SSE: delta / agent(tool_call|tool_result|agent_dispatch|agent_return) / learn ──
```

关键约束: 带 checkpointer 的子图 token 会渗进父图 messages lane, 路由在
`handoff_open`(dispatch 与 return 之间)期间**抑制父图 messages 文本**,
执行智能体的正文只走 custom lane, 前端不会看到重复内容。

## 5. SSE 事件协议

信封不变: `data: {"choices":[{"delta":{...}}], "agent": {...}, "learn": {...}}`,
结束 `data: [DONE]`; 只读 `delta` 的老客户端自动忽略扩展字段。

`agent` 字段事件:

| type | 字段 | 前端渲染 |
|---|---|---|
| `tool_call` / `tool_result` | `id,name,args,result` | 工具节点 loading→success(既有) |
| `agent_dispatch` | `agent_key,name,task` | 「调度 → <name>」节点, loading, task 可展开 |
| `agent_return` | `agent_key,name,task,status` | 同节点转 success / error |

`learn` 字段(教学证据流)契约不变, 见 `docs/pedagogy-core.md`。

## 6. 运维连接器(`backend/app/tools_ops.py`)

| 工具 | 真实端点(env 配置) | 协议 |
|---|---|---|
| `prometheus_query(promql, minutes)` | `PROMETHEUS_URL` + 可选 `PROMETHEUS_TOKEN` | `GET /api/v1/query?query=&time=` |
| `loki_query(logql, limit)` | `LOKI_URL` + 可选 `LOKI_TOKEN` | `GET /loki/api/v1/query_range` |
| `k8s_list_resources(kind, namespace)` | `K8S_API_URL` + 可选 `K8S_API_TOKEN`, `K8S_VERIFY_TLS` | `GET /api/v1/...` 核心组资源列表 |

**配置驱动 + mock 回退**: env 未配置、或请求失败/超时, 返回结构化 mock 数据并带
`source: "mock"` 字段(与 `MockChatModel` 无凭证哲学一致); 配置齐全则走 httpx 真实解析。
三个连接器与 `get_current_time/calculate/get_weather` 一起构成 `ALL_TOOLS`
(本地工具目录共 6 个, 即 agents catalog 的可选工具)。

## 7. 前端架构要点

- 路由组 `frontend/src/app/(platform)/`: 外壳 layout + 各模块页; 壳外仅 login/register。
- `src/lib/api.ts` 新增 `agentApi`(list/catalog/create/get/update/setStatus/remove)
  与 `AgentSummary/AgentDetail/AgentCatalog` 类型, 复用双 Token 请求层。
- `/agents` 页: 搜索(名称、ID, 300ms 防抖) + 状态筛选 + 「新建智能体」(仅 `agent:write`);
  三列响应式卡片(角色徽标 总管紫/执行蓝、等宽 ID、状态 Tag 活动绿/暂停金、
  两行截断描述、四色计数 chips、「…」菜单 编辑/暂停/删除, `supervisor_main` 禁删禁停);
  新建/编辑 Modal Form(key 仅新建、role 仅新建、mcp/rag 用 Form.List 登记名称)。
- `/chat` 页: `ToolChainChatProvider` 累积 `agent` 事件; 思维链按事件到达顺序渲染
  调度节点与工具节点(调度 loading→success/error); 对话入口固定 `supervisor_main`。

## 8. 下一阶段规划(未实现)

1. **MCP 接入**: `mcp_refs` → 真实 MCP client(工具发现/授权/健康检查), 资产中心管理页。
2. **RAG 接入**: `rag_refs` → 向量库检索工具(文档上传/切片/索引), 知识库管理页。
3. **任务中心**: 调度记录落表(`agent_runs`: 谁调度谁、耗时、token、状态), 定时巡检任务。
4. **可观测中心**: 暴露 Prometheus 自监控指标 + trace 视图, 对接 §6 连接器回读平台自身。
5. **配置中心**: 模型/凭证在线配置(替代 `.env` 重启), 密钥托管与轮换。
6. **仪表盘**: 聚合 §3/§4 的运行数据(调度量、成功率、活跃智能体)。

## 9. 测试与验收

- `backend/tests/test_agents_api.py` — CRUD/权限/筛选/计数/409/422;
- `backend/tests/test_ops_connectors.py` — 无 env mock、monkeypatch httpx 真实解析、异常回退;
- `backend/tests/test_multi_agent.py` — 脚本化 tool_calls 验证 分发→返回→汇总、
  paused 无 handoff、SSE 含 `agent_dispatch`、edu-agent 派生 thread 跨轮存活;
- `tests/test_migrations.py` — Alembic 与 bootstrap SQL 双轨对账。
- 前端无测试 runner: `pnpm lint` + `pnpm build`, 手动走查 /agents 卡片与新建/暂停流、
  /chat 发「现在几点了」与一条运维问题核对调度链 UI。
