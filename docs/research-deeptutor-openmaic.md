# 参考项目调研：DeepTutor 与 OpenMAIC

调研日期：**2026-09-14**。来源只有三处，均可点开核对：

- DeepTutor README（`main` 分支，抓取时最新版本 **v1.6.7**）：<https://github.com/HKUDS/DeepTutor>
- DeepTutor 论文摘要页：*DeepTutor: Towards Agentic Personalized Tutoring*，arXiv:2604.26962 — <https://arxiv.org/abs/2604.26962>
- OpenMAIC README（`main` 分支，抓取时最新版本 **v1.0.0**，2026-08-27 发布）：<https://github.com/THU-MAIC/OpenMAIC>；中文 README 停留在 v0.3.2，本文以英文 README 为准

规矩：**本文只写核实过的事实**；道听途说或记忆中的说法一律进 §4「待核实」，不允许被设计文档引用。
设计取舍见 [education-agent-vision.md](./education-agent-vision.md) §5，落地映射见 [education-agent-roadmap.md](./education-agent-roadmap.md) 附录。

---

## 0. 一页速览

| 维度 | DeepTutor（HKUDS） | OpenMAIC（THU-MAIC） |
|---|---|---|
| 定位 | 终身个性化辅导 / agent-native 学习工作区 | 多智能体互动课堂（MOOC 的继任形态） |
| 许可证 | Apache-2.0（README 徽章） | MIT（v0.3.0 起，由 AGPL-3.0 改换；可免费商用） |
| 形态 | Python 包 + CLI（`pip install -U deeptutor`）+ Next.js 前端 + Docker | Next.js Web 应用 + 可选服务端持久化 + Docker / Vercel |
| 最新版本 | v1.6.7（抓取时）；v1.6.6 = 2026-09-08 | v1.0.0 = 2026-08-27 |
| 内核强项 | **分层记忆（L1/L2/L3 + Memory Graph）**、引用溯源辅导、难度校准出题、Mastery Path | **两阶段课堂生成**、LangGraph 多智能体编排、动作引擎与白板、导出与离线播放 |
| 评估 | TutorBench + profile-driven student simulator（论文） | 论文 *From MOOC to MAIC*（JCST 2026）；README 未给公开榜单数据 |
| 技能生态 | EduHub 社区技能、MCP servers、CLI apps、Partners/外接 agent harness | `SKILL.md` 技能包（OpenClaw / Codex / DeepSeek / WorkBuddy）+ v1.0.0 内置 20 个技能 |
| 对我们的价值 | 学习者记忆与掌握度路径的工程范式 | 课堂 DSL、导演图编排、服务端会话健壮性的工程范式 |

---

## 1. DeepTutor

### 1.1 基本信息与时间线（README「News」）

| 时间 | 事件 |
|---|---|
| 2025-12-29 | 正式开源发布 |
| 2026-02-06 | 39 天 10k stars |
| 2026-04-10 | 论文上线 arXiv（2604.26962） |
| 2026-04-19 | 111 天 20k stars |
| 2026-05-22 | 官方文档站 <https://deeptutor.info/> |
| 2026-05-21/22 | v1.4.0：Auto Mode、**三层 Memory**、agentic Deep Research / Solve / Question、LlamaIndex RAG 重构、可重启恢复的轮次运行时 |
| 2026-09-06 | v1.6.5：内容工作区可指向任意文件夹、统一 `exec` 工具、**Mastery Path 模式可门控其工具**、Settings 评估就绪度 |
| 2026-09-08 | v1.6.6：修复无法提交的答案、复制按钮状态、Partners 的知识库连通、Docker 内 Codex 登录 |

### 1.2 论文主张（摘要原文要点）

- 统一了两件事：**引用溯源的题目辅导**（citation-grounded problem tutoring）与**难度校准的题目生成**（difficulty-calibrated question generation）。
- **混合个性化引擎**：静态知识锚定（RAG）+ 动态学习者记忆耦合，逐次交互适配学习者演化中的需求。
- 同一套个性化基座进一步支撑：自适应学习工作流、互动书籍（interactive books）、**主动式多通道辅导 agent**。
- 提出 **TutorBench**：交互式基准，含基于五个学科大学课程定制的学习者画像。
- 提出 **LLM 第一人称交互式评测协议**：由画像驱动的 student simulator 完成测评。
- 结果（摘要给出的数字）：个性化指标平均 **+10.8%**；在五个 backbone 模型上通用 agentic 推理 **+29.4%**；含人类对齐与消融研究。

> 对我们的启示：**评测协议本身就是产品能力**。没有 student simulator，教学策略无法回归；
> 这正是 roadmap 里 P0 就要有 `evals/learner_sim/` 骨架的原因。

### 1.3 官方 Key Features（逐条 + 解读）

| 原文要点 | 事实 | 我们的解读 |
|---|---|---|
| One runtime for every mode | Chat、Ask Questions、Quiz、Research、Visualize、Solve、Course Study、Mastery Path、Immersive Reading、Immersive Watching 共享一套 capability runtime 与 session context，但各自保留专用循环与流水线 | 「一套运行时 + 多种模式」优于「每种模式一个 agent」；我们的 tutor / classroom / evaluator 子图应共享 pedagogy 内核与 context |
| Connected learning context | 知识库、书籍、Co-Writer 草稿、笔记本、题库、persona、Memory 可跨工作流复用，受账号授权与学习策略约束 | 学习资产必须跨场景复用，且**受权限约束**（我们已有 RBAC，天然契合） |
| Immersive video learning | 粘贴 YouTube 链接：隐私增强播放、字幕同步、**按时间戳锚定的辅导**、可续播进度；管理员可切换到自托管 Invidious | 「时间戳锚定」是把外部内容变成 Atom 的好范式（P3 视频→Atom 抽取） |
| Subagents and Partners | 从 Chat 里调用活的 agent harness（Claude Code、Codex、Antigravity、Kimi、opencode、MiMo、Hermes、OpenClaw、DeepSeek）、导入历史会话、在同一「大脑」上跑常驻 IM 伙伴 | 通道扩张不是我们的优先级（vision §5 不做清单）；但「同一内核多个外壳」值得学 |
| Multi-engine knowledge | 版本化 RAG 库：LlamaIndex、PageIndex、GraphRAG、LightRAG、远端 LightRAG Server、自托管 WeKnora、腾讯 IMA、MarginNote 4、关联 Obsidian 库；解析器可插拔 | 检索引擎要**接口化**（我们 `app/knowledge/retrieval.py`），先内置最小实现，后期可替换 |
| Extensible tools and skills | 内置工具、MCP servers、CLI apps、图像/视频/语音生成模型、来自 **EduHub** 的可安装社区技能 | 技能生态是教育产品的护城河之一；我们的 skills 子系统已具备同构能力 |
| Inspectable memory | **L1 traces、L2 surface summaries、L3 synthesis**；Memory Graph 把 L2 事实连到 L1 证据、把 L3 综合连到贡献它的 surface | 记忆必须**可查看、可编辑**；这正是 architecture §5「画像每句话都能点回证据」的来源 |

### 1.4 记忆与 Mastery Path 的版本演进（可核对的发布说明）

- v1.4.0-beta（2026-05-21）：三层记忆工作台（L1/L2/L3），所有聊天能力重建在单一 agentic 引擎上，LlamaIndex-only RAG。
- v1.4.9（2026-06-19）：**Mastery Path 的判分题目会流入 Question Bank**（诊断结果变成资产）。
- v1.6.4（2026-09-03）：source-complete Mastery paths 与 Chat 之间的 hand-off、可控的 Book 生成、可恢复会话。
- v1.6.5（2026-09-06）：**Mastery Path 模式门控其工具**（mode gates its tools）。

> 「掌握度门控工具」这条与我们的现状高度同构：本仓库 `SkillMiddleware` 已经在用
> `request.override(tools=...)` 按权限收敛工具子集 —— 把「权限」换成「掌握度」即可（roadmap P1 #5）。

### 1.5 工程形态（部署与配置）

- 四种安装路径共享一套 runtime-home 布局：私有配置在启动目录下的 `data/user/settings/`，
  或用 `DEEPTUTOR_HOME` / `deeptutor start --home` 指定。
- 推荐流程：选 runtime-home 目录 → 安装 → `deeptutor init` → `deeptutor start`。
- PyPI 路径：`pip install -U deeptutor`；`deeptutor init` 会问后端端口（默认 **8001**）、前端端口（默认 **3782**）、
  LLM provider / base URL / key / model、可选 embedding provider（知识库/RAG）、可选 search provider；
  需要 **Python 3.11–3.14** 与 **Node.js 20+**（打包好的 Next.js standalone server 由 `deeptutor start` 拉起）。
- 跳过 `init` 也能起（默认端口 + 空模型配置，之后在 Settings → Models 里配）。
- 源码路径：`deeptutor init` + `deeptutor start --dev`（Next.js HMR）；`deeptutor start` 会构建一次 `web/` 并复用。
- CLI-only：`deeptutor init --cli` 跳过端口询问，写 `system.json`、`auth.json`、`integrations.json`、
  `interface.json`、`model_catalog.json`、`main.yaml`、`agents.yaml`；`deeptutor doctor` 做体检。
- Docker：浏览器只与前端通信；Linux 可用 `--network=host`（需注意端口冲突，可用 `BACKEND_HOST` / `FRONTEND_HOST` 绑回 loopback）。

> 对我们的启示：**本地优先 + 单命令启动 + 自带体检（doctor）**是可开源教育产品的标配。
> 我们已有 compose 一键起，可补一个 `make doctor` 式的健康自检（P3）。

---

## 2. OpenMAIC

### 2.1 基本信息

- **OpenMAIC**（Open Multi-Agent Interactive Classroom）：开源 AI 互动课堂平台，
  把任意主题或文档转成可互动的学习体验；由多智能体编排驱动，生成幻灯片、测验、交互模拟与 PBL 活动，
  由 **AI 老师与 AI 同学**用语音讲解、白板绘图并与学习者实时讨论。
- 在线体验：<https://open.maic.chat/>；许可证 **MIT**（v0.3.0 由 AGPL-3.0 改换，可免费商用）；
  商业合作邮箱 `thu_maic@mail.tsinghua.edu.cn`。
- 引用论文（README「Citation」原文 bibtex）：*From MOOC to MAIC: Reimagine Online Teaching and Learning
  through LLM-driven Agents*，Journal of Computer Science and Technology, 2026，
  DOI `10.1007/s11390-025-6000-0`（作者含 Ji-Fan Yu、Daniel Zhang-Li … Juan-Zi Li、Zhi-Yuan Liu、Mao-Song Sun）。
- 第三方子包不受根 MIT 覆盖：`packages/mathml2omml`（LGPL-3.0-or-later）、`packages/pptxgenjs`（MIT）。
- 版本时间线：v0.1.0（2026-03-26）→ v0.2.0（2026-04-20，深度交互模式）→ v0.3.0（2026-06-28，PBL v2 + SDK 上 npm + 改 MIT）
  → v0.3.1（2026-07-21，MP4 导出 + 服务端课堂存储 + SSRF 加固）→ v0.3.2（2026-08-14，视频导出加固 + 资产注册中心）
  → **v1.0.0（2026-08-27，Agent Workbench）**。

### 2.2 v1.0.0 的转向：Agent Workbench（最值得我们研究的一段）

一句话：**「一个 prompt 出一整门课」升级为「可以边聊边改的建课 agent」。**

| 能力 | README 原文要点 |
|---|---|
| Agent workbench | chat-first 工作区：规划课程、逐页构建与修订、直接使用学习者的材料 |
| Durable sessions | 服务端支撑的会话可在 worker 重启后存活；可取消、可恢复、可随时插入新指令（steering）；事件历史可回放并流式推给聊天界面 |
| Session materials | 上传文档/音频/视频，或从 web search 拉取，agent 基于这些材料建课 |
| Course tools + 20 built-in skills | 幻灯片、测验、交互、PBL、图像、视频、语音、`.pptx` 导入；20 个内置技能覆盖课程规划、深度研究、交互、讲授、工作坊、职业教学等风格 |
| Neutral by design | 自带模型、媒体、搜索 provider 与存储后端；服务端 provider-neutral 解析 LLM/媒体/ASR/TTS/搜索配置 |

工程细节（对我们 P2/P3 直接有用）：

- **显式且受校验的工具，而不是改不透明 blob**：读/搜舞台 DSL、原子化 patch 单个 scene、
  生成/复制/插入/删除/重排页面、编辑旁白与 deck 结构。
- **用户自建技能按 owner 存储**，可通过同一运行时增删改查（与我们的 skills + RBAC 思路一致）。
- **权限模型**：stage ID 作为「读取未删除课程」的 capability，但**修改仍限 owner**；
  owner-scoped 文件夹路由 + per-viewer 舞台元数据 sidecar（所有权、发布状态、生成完成态）。
- **会话健壮性**：agent 会话入库，带 lease、heartbeat、崩溃恢复、取消、后续 steering；
  数据库维护 revision counter，保证 per-stage / per-scene 新鲜度**单调**，前端只重取变化的 scene。
- **安全纪律**：凭证永不下发到浏览器；统一 `<CAP>_<PREFIX>_ENABLED=false` 开关可强制关闭任何服务能力；
  启动校验对错误模型配置告警；**未解析的模型路由直接响亮失败，而不是猜一个 vendor**。
- 材料上传契约：先存源字节，再用 lease 围栏的抽取记录派生文本与图片；媒体抽取可选 AliDocMind 或本地 ffmpeg/ffprobe。

### 2.3 生成流水线与课堂组件

- **两阶段**：Outline（分析输入 → 结构化课程大纲）→ Scenes（每个大纲条目变成富场景：幻灯片 / 测验 / 交互模块 / PBL）。
- **材料入口**：PDF、Word、PowerPoint、表格、文本、图片、音频、视频都能进材料流水线，由配置的抽取器转成生成用内容。
- **课堂组件**：
  - Slides：AI 老师配语音旁白、聚光灯效果、激光笔动画；
  - Quiz：单选 / 多选 / 简答，**AI 实时判分与反馈**；
  - Interactive Simulation：基于 HTML 的交互实验（物理模拟器、流程图等）；
  - PBL：选一个角色，与 AI 智能体协作完成带里程碑与交付物的结构化项目。
- **多智能体互动**：课堂讨论（agent 主动发起话题，学习者可随时加入或被点名）、
  圆桌辩论（多人设 agent 围绕话题讨论 + 白板图解）、自由问答（老师用幻灯片/图表/白板作答）、
  白板（实时绘图：逐步推导方程、画流程图、可视化概念）。

### 2.4 深度交互模式与呈现层

- 五种交互界面：**3D 可视化、模拟实验、游戏、思维导图、在线编程**（v0.2.0 引入）。
- **AI 老师可主动操作界面**引导学习者：高亮关键区域、设置条件、给提示、在恰当时机引导注意力。
- 全部生成界面响应式（桌面 / 平板 / 手机）。
- 更完整的专业教育 UI 生成见姊妹项目 [MAIC-UI](https://github.com/THU-MAIC/MAIC-UI)。
- 导出：`.pptx`（可编辑，含图片、图表、LaTeX 公式）、自包含交互 HTML、课堂 ZIP（`.maic.zip`）、MP4（render-service）。
  **离线/内网**：导出时把 KaTeX、Three.js（含 `three/addons`）、Tailwind CDN、Google Fonts、图片以 `data:` URI 内联，
  导入内网实例后可完全离线播放；抓取失败的资源（如开启 CORS 限制的图床）记录日志并保留原 URL。

### 2.5 架构与代码组织（README「Project Structure / Core Architecture」）

| 模块 | 职责（原文） |
|---|---|
| `app/api/`（26 个端点组） | `generate/`（场景生成流水线：大纲、内容、图片、TTS…）、`generate-classroom/`（异步提交与轮询）、`chat/`（多智能体讨论，SSE）、`pbl/`、`persistence/`（Runtime/Document Store HTTP 契约）、`export-video/`，以及 `quiz-grade`、`parse-pdf`、`web-search`、`transcription` 等 |
| `lib/generation/` | 两阶段课堂生成流水线（对应 `@openmaic/generation`） |
| `lib/orchestration/` | **LangGraph 多智能体编排（导演图）**：管理智能体轮次与讨论 |
| `lib/playback/` | 回放状态机（idle → playing → live） |
| `lib/action/` | 动作执行引擎：**28+ 种动作类型**（语音、白板绘图/文字/形状/图表、聚光灯、激光笔…） |
| `lib/store/`、`lib/hooks/` | Zustand 状态管理；55+ React hooks |
| `components/slide-renderer/` | 基于 Canvas 的幻灯片编辑器与渲染器（元素渲染器：文本、图片、形状、表格、图表…） |
| `components/whiteboard/` | 基于 SVG 的白板绘图 |
| `packages/@openmaic/` | SDK 系列（已发 npm）：`dsl`（课程 DSL 与资产清单）、`generation`、`renderer`、`importer`、`editor`、`storage` |
| `render-service/` | MP4 视频导出（Chromium + FFmpeg，独立容器） |
| `skills/openmaic/` | 标准 `SKILL.md` 技能包：轻量路由层 + 确认规则，`references/` 按需加载 SOP 分段 |

### 2.6 存储与运维

- **默认可无数据库运行**：课程文档、学习者运行时记录、设备/账号 KV、资产都走浏览器存储；
  `@openmaic/storage` 定义可替换存储原语，并提供 PostgreSQL 支持的文档、学习者运行时、资产、
  **持久 agent 会话**、会话材料与用户技能；HTTP 客户端把浏览器接到内嵌持久化端点；服务端资产层可放 PostgreSQL 或 S3。
- **资产注册中心 + 回收器**：删除/替换只移除注册中心条目，字节由离线回收器清理；
  默认每 `ASSET_COLLECTION_INTERVAL_MS`（15 分钟）跑一轮，清理已解引用超过 `ASSET_COLLECTION_GRACE_MS`（1 小时）的字节；
  grace period 就是用户删除字节的实际保留窗口；`ASSET_COLLECTION_ENABLED=0` 可关闭；
  多实例可各自开启（每个 blob 清理前加锁复查，并发回收器串行化而非竞争）。
- 部署：`pnpm dev`（:3000）/ `pnpm build && pnpm start`；`ACCESS_CODE` 站点级口令（同时保护所有 API 路由）；
  Vercel 一键部署；Docker（含中国大陆构建加速）；可选 MP4 render-service、MinerU 文档解析、VoxCPM2 自托管 TTS（音色克隆）。
- Provider 覆盖面极广：OpenAI、Azure OpenAI、Anthropic、Amazon Bedrock、Google Gemini、DeepSeek、通义千问、Kimi、
  MiniMax、Grok、OpenRouter、豆包、腾讯混元/TokenHub、小米 MiMo、智谱 GLM、Ollama、Lemonade（本地 LLM/图像/TTS/ASR）、
  FunASR（本地 ASR）以及任何 OpenAI 兼容服务；推荐 Gemini 3 Flash（效果/速度平衡），最高质量 Gemini 3.1 Pro。
- i18n：11 种语言 / 12 个区域设置；暗色模式。

---

## 3. 两个项目的共同趋势（= 对我们的启示）

| 趋势 | DeepTutor 的证据 | OpenMAIC 的证据 | 我们怎么做 |
|---|---|---|---|
| 收敛到「agent workbench + 技能包」 | EduHub 社区技能、MCP、CLI apps、Partners | v1.0.0 Agent Workbench + 20 内置技能 + 用户自建技能按 owner 存储 + `SKILL.md` 分发 | 现有 skills 子系统（三级披露 + RBAC + 热加载）正在这条主线上，直接长成「教学法/学科技能库」 |
| 会话必须可恢复、可插话 | recoverable sessions、restart-safe turn runtime | durable sessions：lease + heartbeat + 崩溃恢复 + 取消 + steering + 可回放事件历史 | P2 课堂流按此标准设计（`classroom_event` 单调 seq + 幂等重试） |
| 服务端纪律：凭证不下发、能力开关化、失败要响亮 | 管理员可切换 Invidious；settings 分层 | provider-neutral 解析、`<CAP>_*_ENABLED=false`、未解析模型路由响亮失败 | 我们已有 `config.py` + PII 加密 + `/ready` 探针，补「能力开关 + 启动校验」即可 |
| 诊断结果沉淀为资产 | Mastery Path 判分题流入 Question Bank | quiz 作答状态持久化、课程完成页 | `learner_fact`（误解库）+ `mastery_ledger`（账本）：错题与卡点必须回流 |
| 存储可插拔、默认可轻跑 | 四种安装路径、本地 runtime-home | 默认浏览器存储，Postgres/S3 可选 | 沿用 checkpointer 的 sqlite/postgres 双后端模式，新表同理 |
| 呈现层重投入 | Book / Immersive Reading / Visualize / 视频学习 | Canvas 编辑器、28+ 动作引擎、3D/游戏、MP4 渲染服务 | **明确后置**（vision §5 不做清单）：先文本 DSL + Markdown 导出 |

**两个项目共同的空白**（也是我们的切入点）：
「学习者是否真的重建了知识」缺少硬证据层 —— DeepTutor 有分层记忆与 Mastery Path（最接近），
但辅导质量仍以对话与答题为间接信号；OpenMAIC 侧重生成与呈现，课堂是一次性产物。
我们要做的 **费曼循环状态机 + 四类卡点判分 + Atom 图掌握度账本**，正是把这一层变成系统的主控制流
（推导见 [vision §2](./education-agent-vision.md)，规格见 [pedagogy-core.md](./pedagogy-core.md)）。

---

## 4. 待核实（**不要**在设计文档里引用）

| 说法 | 状态 | 处理 |
|---|---|---|
| OpenMAIC 有 AIME（AI-MOOC Exam）/ MAIC-Bench 等公开评测榜单成绩 | **未核实**：抓取到的中英文 README 均未出现该名称与成绩 | 已从 vision/roadmap 中删除；如需引用，先查 JCST 论文正文 |
| DeepTutor 使用「dual-loop reasoning（双循环推理）」 | **未核实**：README 未出现该表述（可能来自论文正文或第三方介绍） | 不写入设计文档；需要时读 arXiv:2604.26962 正文 |
| DeepTutor 使用「graph-enhanced retrieval」作为统一检索方案 | **部分核实**：README 确认支持 GraphRAG / LightRAG / PageIndex 等多引擎，但未使用「graph-enhanced retrieval」这一措辞 | 设计文档只写「多引擎 RAG 可插拔」 |
| 两个项目的用户规模 / 留存 / 教学效果数据 | **未核实**：README 只有 star 数与论文自报指标 | 不作为立项依据 |

> 维护约定：本文每次更新都要重跑一遍核实（README 变动很快 —— OpenMAIC 中文 README 就落后英文版一个大版本），
> 并把「待核实」里被证实的条目上移到正文、注明来源版本与日期。
