-- =====================================================================
-- 学习内核(P0 教学内核 MVP) — MySQL 8.0 DDL + RBAC 权限种子
-- 执行前置: 001_schema.sql(account) / 002_seed_rbac.sql(role, permission)
-- 与 Alembic 迁移 f3a9c1d47b60_add_learning_tables.py **等价**:
--   新建库走本文件(004 已把库 stamp 到 head, 不重放迁移),
--   既有库走迁移; 两条路径的列名/类型/默认值/索引由 tests/test_migrations.py 守卫。
-- 设计原则(见 docs/education-agent-architecture.md §4、docs/pedagogy-core.md):
--   1. 知识图与账号无关(全局共享): concept → atom → atom_edge 三张表
--   2. 学习证据挂在账号上, 账号注销级联清理(learner_explanation / mastery_ledger)
--   3. slug / atom_key 是内核使用的稳定字符串 ID; BIGINT 主键只在库内流通
--   4. verified=0 的 Atom 不得进入掌握度记账(未溯源只能用于提示)
--   5. 掌握度用 DECIMAL 而非 FLOAT: 账本是要给家长/教研看的证据, 不容浮点噪声
-- =====================================================================

USE `auth_service`;

-- ---------------------------------------------------------------------
-- 1. 概念节点: 一轮费曼循环的对象
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `learning_concept` (
  `id`         BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '主键',
  `slug`       VARCHAR(128)    NOT NULL                COMMENT '概念标识, 如 attention-scaled-dot-product',
  `title`      VARCHAR(255)    NOT NULL                COMMENT '概念标题',
  `subject`    VARCHAR(64)     NULL DEFAULT NULL       COMMENT '学科标签, 仅用于筛选, 不参与内核逻辑',
  `scope`      TEXT            NULL                    COMMENT '范围边界: 明确不讨论什么(no_boundary 卡点的判据)',
  `summary`    TEXT            NULL                    COMMENT '概念摘要',
  `created_at` DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_concept_slug` (`slug`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='概念节点';

-- ---------------------------------------------------------------------
-- 2. 知识原子: 不可再分的最小命题(Why-chain 的下探落点)
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `learning_atom` (
  `id`             BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '主键',
  `atom_key`       VARCHAR(191)    NOT NULL                COMMENT '稳定标识 atom:xxx(即内核 Atom.id)',
  `concept_id`     BIGINT UNSIGNED NOT NULL                COMMENT '所属概念',
  `statement`      TEXT            NOT NULL                COMMENT '不可再分的最小命题',
  `kind`           VARCHAR(16)     NOT NULL                COMMENT 'definition|axiom|observable|derivable(前三者是 Why-chain 可停点)',
  `source_title`   VARCHAR(255)    NULL DEFAULT NULL       COMMENT '来源标题',
  `source_locator` VARCHAR(255)    NULL DEFAULT NULL       COMMENT '章节/页码/时间戳',
  `source_url`     VARCHAR(1024)   NULL DEFAULT NULL       COMMENT '来源链接',
  `verified`       TINYINT(1)      NOT NULL DEFAULT 0      COMMENT '未溯源不得进入掌握度记账',
  `difficulty`     TINYINT         NOT NULL DEFAULT 1      COMMENT '1-5, 出题难度校准用(默认值与 schemas.Atom 一致)',
  `created_at`     DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_atom_key` (`atom_key`),
  KEY `idx_atom_concept` (`concept_id`),
  CONSTRAINT `fk_atom_concept` FOREIGN KEY (`concept_id`)
    REFERENCES `learning_concept` (`id`) ON DELETE CASCADE ON UPDATE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='知识原子';

-- ---------------------------------------------------------------------
-- 3. 原子依赖边: 第一性原理的图化身
--    weight 决定概念掌握度 M = Σ m·weight / Σ weight 的分母
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `learning_atom_edge` (
  `id`           BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '主键',
  `from_atom_id` BIGINT UNSIGNED NOT NULL                COMMENT '起点 Atom',
  `to_atom_id`   BIGINT UNSIGNED NOT NULL                COMMENT '终点 Atom',
  `relation`     VARCHAR(16)     NOT NULL                COMMENT 'depends_on|entails|contradicts',
  `weight`       DECIMAL(4,3)    NOT NULL DEFAULT 1.000  COMMENT '关键边权重更高',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_edge` (`from_atom_id`, `to_atom_id`, `relation`),
  KEY `idx_edge_from` (`from_atom_id`),
  KEY `idx_edge_to` (`to_atom_id`),
  CONSTRAINT `fk_edge_from` FOREIGN KEY (`from_atom_id`)
    REFERENCES `learning_atom` (`id`) ON DELETE CASCADE ON UPDATE RESTRICT,
  CONSTRAINT `fk_edge_to` FOREIGN KEY (`to_atom_id`)
    REFERENCES `learning_atom` (`id`) ON DELETE CASCADE ON UPDATE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='原子依赖边(第一性原理的图化身)';

-- ---------------------------------------------------------------------
-- 4. 讲解证据: 费曼循环每一轮自述的原始产物
--    原话与判分结论分列存储 —— 换 rubric 可拿 raw_text 重跑, 证据不被结论污染
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `learner_explanation` (
  `id`                 BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '主键',
  `account_id`         BIGINT UNSIGNED NOT NULL                COMMENT '学习者账号',
  `thread_id`          VARCHAR(64)     NULL DEFAULT NULL       COMMENT '关联 LangGraph thread(L1 记忆)',
  `concept_id`         BIGINT UNSIGNED NOT NULL                COMMENT '所属概念',
  `loop_index`         TINYINT         NOT NULL DEFAULT 1      COMMENT '第几轮 Teach(1 起, 上限 PedagogyConfig.max_loops)',
  `state`              VARCHAR(16)     NOT NULL                COMMENT 'Pick|Teach|Detect|Return|Simplify|Closed|Escaped',
  `raw_text`           MEDIUMTEXT      NOT NULL                COMMENT '学习者原话, 不加工',
  `claims_json`        JSON            NOT NULL                COMMENT '命题拆解 + status',
  `gap_report_json`    JSON            NULL                    COMMENT '四类卡点 + probe + supply',
  `learner_talk_ratio` DECIMAL(4,3)    NULL DEFAULT NULL       COMMENT 'learner_chars/(learner+system), 护栏口径',
  `created_at`         DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  PRIMARY KEY (`id`),
  KEY `idx_expl_account` (`account_id`, `concept_id`, `created_at`),
  CONSTRAINT `fk_expl_account` FOREIGN KEY (`account_id`)
    REFERENCES `account` (`id`) ON DELETE CASCADE ON UPDATE RESTRICT,
  CONSTRAINT `fk_expl_concept` FOREIGN KEY (`concept_id`)
    REFERENCES `learning_concept` (`id`) ON DELETE CASCADE ON UPDATE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='讲解证据(费曼循环的原始产物)';

-- ---------------------------------------------------------------------
-- 5. 掌握度账本: 每个 (账号, Atom) 一行, 每个数字都追得到证据
--    tau_days/depth 让 MasteryEntry 整条往返: m+α·w·(1−m) 与 m·exp(−Δt/τ) 可回溯
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `mastery_ledger` (
  `id`               BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '主键',
  `account_id`       BIGINT UNSIGNED NOT NULL                COMMENT '学习者账号',
  `atom_id`          BIGINT UNSIGNED NOT NULL                COMMENT '知识原子',
  `mastery`          DECIMAL(5,4)    NOT NULL DEFAULT 0.0000 COMMENT '掌握度 m ∈ [0,1]',
  `evidence_count`   INT             NOT NULL DEFAULT 0      COMMENT '有效证据条数(读直答/放弃不计数)',
  `last_evidence_at` DATETIME        NULL DEFAULT NULL       COMMENT '最近一次有效证据时间',
  `next_review_at`   DATETIME        NULL DEFAULT NULL       COMMENT '间隔重复调度(1/3/7/21/60 天阶梯)',
  `review_stage`     TINYINT         NOT NULL DEFAULT 0      COMMENT '复习阶梯下标(REVIEW_LADDER_DAYS 的索引)',
  `tau_days`         DECIMAL(6,3)    NOT NULL DEFAULT 7.000  COMMENT '遗忘时间常数 τ(天): 冷启动取配置, 之后按该 Atom 历史表现拟合',
  `depth`            TINYINT         NOT NULL DEFAULT 0      COMMENT '最近一次 Why-chain 下探深度(证据权重修正用)',
  `updated_at`       DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP
                                     ON UPDATE CURRENT_TIMESTAMP COMMENT '最近记账时间(衰减按此算 Δt)',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_mastery` (`account_id`, `atom_id`),
  KEY `idx_mastery_review` (`account_id`, `next_review_at`),
  CONSTRAINT `fk_mastery_account` FOREIGN KEY (`account_id`)
    REFERENCES `account` (`id`) ON DELETE CASCADE ON UPDATE RESTRICT,
  CONSTRAINT `fk_mastery_atom` FOREIGN KEY (`atom_id`)
    REFERENCES `learning_atom` (`id`) ON DELETE CASCADE ON UPDATE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='掌握度账本: 每条边一行, 可回溯证据';

-- ---------------------------------------------------------------------
-- 6. RBAC 权限点种子(与迁移 f3a9c1d47b60 的 PERMISSIONS/GRANTS 一致)
--    admin 运行时展开为通配 *, 不需要授权行; 接口在后续提交落地, 先播种避免上线即 403
-- ---------------------------------------------------------------------
INSERT IGNORE INTO `permission` (`perm_code`, `perm_name`, `resource`, `api_method`, `api_path`) VALUES
  ('learning:self:read',         '查看自己的学习数据',   'learning', 'GET',  '/api/v1/learning/*'),
  ('learning:explanation:write', '写入讲解证据',         'learning', 'POST', '/api/v1/learning/explanations'),
  ('learning:report:read',       '查看学习报告',         'learning', 'GET',  '/api/v1/learning/report'),
  ('learning:atom:admin',        '维护概念图与知识原子', 'learning', 'POST', '/api/v1/learning/concepts');

INSERT IGNORE INTO `role_permission` (`role_id`, `permission_id`)
SELECT r.id, p.id FROM `role` r JOIN `permission` p
  ON (r.role_code = 'member' AND p.perm_code IN
      ('learning:self:read', 'learning:explanation:write'))
  OR (r.role_code = 'operator' AND p.perm_code IN
      ('learning:report:read'));
