-- =====================================================================
-- Agent 会话元数据表 — MySQL 8.0 DDL
-- 设计原则:
--   1. thread_id 与 LangGraph 的 thread_id 一致(服务端生成 uuid4)
--   2. 挂在账号体系上: account_id 外键, 账号注销级联清理
--   3. (account_id, updated_at DESC) 组合索引支撑"按用户倒序查会话列表"
--   4. 仅存元数据(标题/最后消息预览), 完整对话记忆在 LangGraph checkpointer
-- =====================================================================

USE `auth_service`;

-- ---------------------------------------------------------------------
-- Agent 会话元数据表
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `agent_threads` (
  `thread_id`    VARCHAR(64)     NOT NULL                COMMENT '会话ID, 与 LangGraph thread_id 一致(uuid4)',
  `account_id`   BIGINT UNSIGNED NOT NULL                COMMENT '所属账号ID',
  `title`        VARCHAR(255)    NOT NULL DEFAULT '新会话' COMMENT '会话标题(首条用户消息截断或手动重命名)',
  `last_message` TEXT            NULL DEFAULT NULL       COMMENT '最新一条助手回复预览(截断存储)',
  `created_at`   DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  `updated_at`   DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP
                                 ON UPDATE CURRENT_TIMESTAMP COMMENT '最后对话时间(按此倒序排序)',
  PRIMARY KEY (`thread_id`),
  KEY `idx_threads_account_updated` (`account_id`, `updated_at` DESC),
  CONSTRAINT `fk_threads_account` FOREIGN KEY (`account_id`)
    REFERENCES `account` (`id`) ON DELETE CASCADE ON UPDATE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='Agent 会话元数据';
