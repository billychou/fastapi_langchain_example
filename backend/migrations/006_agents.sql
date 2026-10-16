-- =====================================================================
-- 006 智能体注册表(agent) + 平台首屏种子 + agent:* 权限点
--
-- 与 Alembic 迁移 e7f9a3b5c1d6_add_agents_table.py 等价(新建库走本文件,
-- 既有库走 alembic upgrade head); 一致性由 tests/test_migrations.py 守卫。
-- 设计依据见 docs/aiops-platform-design.md。
-- =====================================================================
USE `auth_service`;

-- ---------------------------------------------------------------------
-- 1. 智能体注册表
--    总管/执行智能体的定义、状态与工具/技能绑定; mcp_refs / rag_refs 为
--    下一阶段 MCP 远端工具与 RAG 检索工具预留(本期只建模与计数)。
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `agent` (
  `id`            BIGINT           NOT NULL AUTO_INCREMENT,
  `agent_key`     VARCHAR(64)      NOT NULL                COMMENT '稳定标识, 如 supervisor_main / k8s-agent',
  `name`          VARCHAR(64)      NOT NULL                COMMENT '展示名, 如「K8s 集群诊断」',
  `role`          VARCHAR(16)      NOT NULL                COMMENT 'supervisor=总管调度 / executor=执行',
  `status`        VARCHAR(16)      NOT NULL                COMMENT 'active / paused',
  `description`   VARCHAR(500)     NOT NULL DEFAULT ''     COMMENT '卡片展示简介',
  `system_prompt` TEXT             NOT NULL                COMMENT '该智能体的系统提示词',
  `tool_refs`     JSON             NOT NULL                COMMENT '本地工具名列表',
  `skill_refs`    JSON             NOT NULL                COMMENT '技能名列表',
  `mcp_refs`      JSON             NOT NULL                COMMENT 'MCP 远端工具(预留)',
  `rag_refs`      JSON             NOT NULL                COMMENT 'RAG 检索工具(预留)',
  `sort_order`    INT              NOT NULL DEFAULT 0      COMMENT '卡片排序, 小在前',
  `created_at`    DATETIME         NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at`    DATETIME         NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_agent_key` (`agent_key`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='智能体注册表: 总管/执行智能体定义与工具绑定';

-- ---------------------------------------------------------------------
-- 2. 平台首屏种子智能体(与迁移 e7f9a3b5c1d6 的 SEED_AGENTS 一致)
--    supervisor_main 为系统内置总管: 接口层禁止删除/暂停。
-- ---------------------------------------------------------------------
INSERT IGNORE INTO `agent` (`agent_key`, `name`, `role`, `status`, `description`, `system_prompt`, `tool_refs`, `skill_refs`, `mcp_refs`, `rag_refs`, `sort_order`) VALUES
  ('supervisor_main', '调度管理者', 'supervisor', 'active',
   '总管调度智能体，拆分复杂任务、分发至执行智能体，汇总多智能体结果，生成最终回复。',
   '你是 AI 运维智能平台的总管调度智能体。职责: 1) 理解用户的运维请求; 2) 拆分任务并调用对应执行智能体; 3) 汇总各执行智能体的结果, 给出结构化的最终回复。不要自行编造监控数据, 一切以执行智能体的返回为准。',
   '["get_current_time"]', '["expense-report"]', '[]', '[]', 0),
  ('k8s-agent', 'K8s 集群诊断', 'executor', 'active',
   'K8s 集群资源查询诊断智能体，获取集群资源信息，分析资源状态与异常事件。',
   '你是 K8s 集群诊断智能体。用 k8s_list_resources 获取集群资源, 结合 Prometheus 指标判断资源状态与异常事件, 输出诊断结论与处置建议。',
   '["k8s_list_resources", "prometheus_query", "loki_query", "get_current_time"]', '[]', '[]', '[]', 1),
  ('edu-agent', '学院业务数据', 'executor', 'paused',
   '学院业务数据查询智能体，通过 MCP 远端工具读取、统计学院相关业务数据。',
   '你是学院业务数据查询智能体。基于 MCP 远端工具返回的数据回答学院教务、学员与课程相关问题; 数据缺失时明确说明, 不要推测。',
   '["calculate"]', '["feynman-listener"]', '["college-bi", "college-sis"]', '[]', 2),
  ('rag-agent', '知识库检索', 'executor', 'active',
   '知识库检索智能体，检索内网知识库文档，提取资料信息，回答业务文档相关问题。',
   '你是知识库检索智能体。检索内网知识库(运维手册、故障复盘), 提取关键信息并注明出处后回答。',
   '[]', '[]', '[]', '["ops-runbook", "fault-postmortem"]', 3),
  ('metric-agent', '指标分析', 'executor', 'active',
   '指标分析智能体，查询 Prometheus 指标，研判趋势、识别波动，输出指标分析结果。',
   '你是指标分析智能体。用 prometheus_query 获取指标, 研判趋势、识别波动, 输出分析结论与异常点。',
   '["prometheus_query", "calculate", "get_current_time"]', '[]', '[]', '[]', 4),
  ('log-agent', '日志分析', 'executor', 'active',
   '日志分析智能体，查询 Loki 日志，筛选异常、定位报错，输出日志分析结果。',
   '你是日志分析智能体。用 loki_query 筛选日志, 识别错误与异常, 定位报错原因并输出分析结论。',
   '["loki_query", "calculate", "get_current_time"]', '[]', '[]', '[]', 5);

-- ---------------------------------------------------------------------
-- 3. RBAC 权限点种子(与迁移 e7f9a3b5c1d6 的 PERMISSIONS/GRANTS 一致)
--    admin 运行时展开为通配 *, 不需要授权行。
-- ---------------------------------------------------------------------
INSERT IGNORE INTO `permission` (`perm_code`, `perm_name`, `resource`, `api_method`, `api_path`) VALUES
  ('agent:read',  '查看智能体注册表',           'agent', 'GET',  '/api/v1/agents'),
  ('agent:write', '新建/编辑/启停/删除智能体', 'agent', 'POST', '/api/v1/agents');

INSERT IGNORE INTO `role_permission` (`role_id`, `permission_id`)
SELECT r.id, p.id FROM `role` r JOIN `permission` p
  ON (r.role_code = 'member' AND p.perm_code IN ('agent:read'))
  OR (r.role_code = 'operator' AND p.perm_code IN ('agent:read', 'agent:write'));
