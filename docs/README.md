# docs/ — 产品演变方向：通用教育智能体

一句话方向：把当前这套「FastAPI + LangChain 企业级对话 Agent 示例（SSE 聊天 + 双 Token JWT/RBAC + Skills 子系统）」
演变成 **以费曼学习法为主控制流、以第一性原理为知识结构** 的通用教育智能体 ——
一个「证据优先」的学习操作系统（Evidence-first Learning OS）。

| 文档 | 回答的问题 |
|---|---|
| [aiops-platform-design.md](./aiops-platform-design.md) | 企业级 AI 运维智能平台第一期: 模块地图、智能体注册表、多智能体调度、连接器协议与下阶段规划(**已落地**) |
| [education-agent-vision.md](./education-agent-vision.md) | 为什么做、做成什么、和 DeepTutor / OpenMAIC 的差异在哪、怎么衡量成功 |
| [pedagogy-core.md](./pedagogy-core.md) | 费曼循环与第一性原理下探如何变成机器可执行的状态机、数据结构与事件契约 |
| [education-agent-architecture.md](./education-agent-architecture.md) | 目标架构长什么样，与现有代码/表/中间件如何一一对应 |
| [education-agent-roadmap.md](./education-agent-roadmap.md) | 分几个阶段落地，每阶段的交付物、验收标准与退出条件 |
| [research-deeptutor-openmaic.md](./research-deeptutor-openmaic.md) | 两个参考项目的事实调研笔记（含链接与借鉴清单） |

建议阅读顺序：vision → pedagogy-core → architecture → roadmap；research 是支撑材料，可随时查阅。
平台化方向（外壳 + 智能体注册表 + 多智能体运行时）见 aiops-platform-design.md；
教学内核在平台中的位置是 `edu-agent` 执行智能体，两条线共用同一运行时。

写作基线：`codex/feat-agent-skills` 分支（技能子系统已合入，后端 179 个测试全绿），日期 2026-09-08；
其中 [research-deeptutor-openmaic.md](./research-deeptutor-openmaic.md) 的调研日期为 2026-09-14，
两个参考项目版本迭代很快，引用前请按该文 §4 的维护约定重新核实。
文档主体描述的是**方向与设计**；已经落地的部分就地加了
「**落地状态（P0 #N，已实现）**」小节（写明实现与草案的差异、以及守卫它的测试文件），
草案本身保持原样以便对照。每个阶段落地时请按 `AGENTS.md` 的
「测试 → 检查 → 单独提交」流程走，并回写这里的设计文档。

当前进度（P0 教学内核 MVP）：#1 状态机 · #2 卡点判分 · #3 掌握度 · #4 学习表与迁移 ·
#5 四个 persona 技能 · #6 中间件注入教学状态 + `learn` SSE 已提交；
#7 学习 API、#8 mock 费曼脚本、#9 前端证据面板、#10 learner simulator 骨架待做。
