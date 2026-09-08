# docs/ — 产品演变方向：通用教育智能体

一句话方向：把当前这套「FastAPI + LangChain 企业级对话 Agent 示例（SSE 聊天 + 双 Token JWT/RBAC + Skills 子系统）」
演变成 **以费曼学习法为主控制流、以第一性原理为知识结构** 的通用教育智能体 ——
一个「证据优先」的学习操作系统（Evidence-first Learning OS）。

| 文档 | 回答的问题 |
|---|---|
| [education-agent-vision.md](./education-agent-vision.md) | 为什么做、做成什么、和 DeepTutor / OpenMAIC 的差异在哪、怎么衡量成功 |
| [pedagogy-core.md](./pedagogy-core.md) | 费曼循环与第一性原理下探如何变成机器可执行的状态机、数据结构与事件契约 |
| [education-agent-architecture.md](./education-agent-architecture.md) | 目标架构长什么样，与现有代码/表/中间件如何一一对应 |
| [education-agent-roadmap.md](./education-agent-roadmap.md) | 分几个阶段落地，每阶段的交付物、验收标准与退出条件 |
| [research-deeptutor-openmaic.md](./research-deeptutor-openmaic.md) | 两个参考项目的事实调研笔记（含链接与借鉴清单） |

建议阅读顺序：vision → pedagogy-core → architecture → roadmap；research 是支撑材料，可随时查阅。

写作基线：`codex/feat-agent-skills` 分支（技能子系统已合入，后端 179 个测试全绿），日期 2026-09-08。
文档描述的是**方向与设计**，不是已实现的功能；每个阶段落地时请按 `AGENTS.md` 的
「测试 → 检查 → 单独提交」流程走，并回写这里的设计文档。
