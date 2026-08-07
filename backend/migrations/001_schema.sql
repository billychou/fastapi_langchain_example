-- =====================================================================
-- User & Auth Service — MySQL 8.0 DDL
-- 设计原则:
--   1. 账号主表(account)与凭证表(auth_credential)解耦, 一个账号可挂多种登录方式
--   2. PII(手机号/邮箱/证件号)不落明文: *_enc 存 AES-256-GCM 密文,
--      *_hash 存 HMAC 盲索引(等值查询用), *_masked 存展示用脱敏值
--   3. RBAC: 用户 ↔ 角色 ↔ 权限(API) / 菜单
--   4. 所有表 InnoDB / utf8mb4, 显式外键 + 唯一索引 + 组合索引
-- =====================================================================

CREATE DATABASE IF NOT EXISTS `fastapi_langchain_example`
  DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;
USE `fastapi_langchain_example`;

-- ---------------------------------------------------------------------
-- 1. 账号主表: 只放账号生命周期与策略, 不存放任何 PII
-- ---------------------------------------------------------------------
CREATE TABLE `account` (
  `id`            BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '账号主键(内部)',
  `account_uuid`  CHAR(36)        NOT NULL                COMMENT '对外暴露的账号UUID, 禁止外泄自增ID',
  `status`        TINYINT         NOT NULL DEFAULT 1      COMMENT '状态: 1=正常 2=锁定 3=已注销',
  `login_policy`  VARCHAR(16)     NOT NULL DEFAULT 'multi_device' COMMENT '登录策略: multi_device=多端在线 single_device=单端互踢',
  `last_login_at` DATETIME        NULL DEFAULT NULL       COMMENT '最近登录成功时间',
  `created_at`    DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  `updated_at`    DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP
                                  ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_account_uuid` (`account_uuid`),
  KEY `idx_account_status` (`status`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='账号主表';

USE `fastapi_langchain_example`;
-- ---------------------------------------------------------------------
-- 2. 用户资料表: 与账号 1:1, 承载 PII(加密存储 + 盲索引 + 脱敏展示)
-- ---------------------------------------------------------------------
CREATE TABLE `user_profile` (
  `id`             BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '主键',
  `account_id`     BIGINT UNSIGNED NOT NULL                COMMENT '账号ID',
  `nickname`       VARCHAR(64)     NULL DEFAULT NULL       COMMENT '昵称',
  `avatar_url`     VARCHAR(512)    NULL DEFAULT NULL       COMMENT '头像URL',
  `phone_enc`      VARBINARY(512)  NULL DEFAULT NULL       COMMENT '手机号 AES-256-GCM 密文(nonce+ct+tag)',
  `phone_hash`     CHAR(64)        NULL DEFAULT NULL       COMMENT '手机号 HMAC-SHA256 盲索引(等值查询)',
  `phone_masked`   VARCHAR(32)     NULL DEFAULT NULL       COMMENT '手机号脱敏展示值 138****0000',
  `email_enc`      VARBINARY(512)  NULL DEFAULT NULL       COMMENT '邮箱 AES-256-GCM 密文',
  `email_hash`     CHAR(64)        NULL DEFAULT NULL       COMMENT '邮箱 HMAC-SHA256 盲索引',
  `email_masked`   VARCHAR(128)    NULL DEFAULT NULL       COMMENT '邮箱脱敏展示值 a***@example.com',
  `id_card_enc`    VARBINARY(512)  NULL DEFAULT NULL       COMMENT '身份证号 AES-256-GCM 密文',
  `id_card_masked` VARCHAR(32)     NULL DEFAULT NULL       COMMENT '证件号脱敏展示值',
  `key_id`         VARCHAR(32)     NOT NULL DEFAULT 'k1'   COMMENT 'PII 加密密钥版本(支持密钥轮转)',
  `created_at`     DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  `updated_at`     DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP
                                   ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_profile_account` (`account_id`),
  UNIQUE KEY `uk_profile_phone_hash` (`phone_hash`),
  UNIQUE KEY `uk_profile_email_hash` (`email_hash`),
  CONSTRAINT `fk_profile_account` FOREIGN KEY (`account_id`)
    REFERENCES `account` (`id`) ON DELETE CASCADE ON UPDATE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='用户资料表(PII加密存储)';

-- ---------------------------------------------------------------------
-- 3. 登录凭证表: 与账号 N:1, 统一承载 密码凭证 与 OAuth2 社交凭证
--    identity_type: phone / email / username / wechat / apple ...
--    密码凭证: credential_hash 存 Argon2id 哈希
--    OAuth 凭证: credential_hash 为 NULL, 使用 oauth_openid/unionid
-- ---------------------------------------------------------------------
CREATE TABLE `auth_credential` (
  `id`              BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '主键',
  `account_id`      BIGINT UNSIGNED NOT NULL                COMMENT '账号ID',
  `identity_type`   VARCHAR(20)     NOT NULL                COMMENT '凭证类型: phone/email/username/wechat/apple...',
  `identifier`      VARCHAR(255)    NOT NULL                COMMENT '归一化标识: 手机号/邮箱/用户名, OAuth 为 provider:openid',
  `credential_hash` VARCHAR(255)    NULL DEFAULT NULL       COMMENT 'Argon2id 密码哈希; OAuth 凭证为 NULL',
  `oauth_provider`  VARCHAR(32)     NULL DEFAULT NULL       COMMENT 'OAuth 提供方: wechat/apple/...',
  `oauth_openid`    VARCHAR(128)    NULL DEFAULT NULL       COMMENT 'OAuth openid',
  `oauth_unionid`   VARCHAR(128)    NULL DEFAULT NULL       COMMENT 'OAuth unionid(微信生态跨应用)',
  `verified`        TINYINT         NOT NULL DEFAULT 0      COMMENT '是否已验证: 0=否 1=是',
  `status`          TINYINT         NOT NULL DEFAULT 1      COMMENT '状态: 1=启用 2=禁用',
  `last_used_at`    DATETIME        NULL DEFAULT NULL       COMMENT '最近使用时间',
  `created_at`      DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  `updated_at`      DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP
                                    ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_cred_type_identifier` (`identity_type`, `identifier`),
  KEY `idx_cred_account` (`account_id`),
  KEY `idx_cred_oauth` (`oauth_provider`, `oauth_openid`),
  CONSTRAINT `fk_cred_account` FOREIGN KEY (`account_id`)
    REFERENCES `account` (`id`) ON DELETE CASCADE ON UPDATE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='登录凭证表(多身份绑定)';

-- ---------------------------------------------------------------------
-- 4. RBAC: 角色表
-- ---------------------------------------------------------------------
CREATE TABLE `role` (
  `id`          BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '主键',
  `role_code`   VARCHAR(64)     NOT NULL                COMMENT '角色编码: admin/operator/member',
  `role_name`   VARCHAR(64)     NOT NULL                COMMENT '角色名称',
  `description` VARCHAR(255)    NULL DEFAULT NULL       COMMENT '描述',
  `is_builtin`  TINYINT         NOT NULL DEFAULT 0      COMMENT '是否内置角色(禁止删除)',
  `status`      TINYINT         NOT NULL DEFAULT 1      COMMENT '状态: 1=启用 2=禁用',
  `created_at`  DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  `updated_at`  DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP
                                ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_role_code` (`role_code`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='角色表';

-- ---------------------------------------------------------------------
-- 5. RBAC: API 权限表 (perm_code 与代码中 require_permissions 对应)
-- ---------------------------------------------------------------------
CREATE TABLE `permission` (
  `id`         BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '主键',
  `perm_code`  VARCHAR(128)    NOT NULL                COMMENT '权限编码: account:read / rbac:assign',
  `perm_name`  VARCHAR(64)     NOT NULL                COMMENT '权限名称',
  `resource`   VARCHAR(64)     NULL DEFAULT NULL       COMMENT '资源分组: account/rbac/audit',
  `api_method` VARCHAR(10)     NULL DEFAULT NULL       COMMENT 'HTTP 方法(网关式鉴权用)',
  `api_path`   VARCHAR(255)    NULL DEFAULT NULL       COMMENT 'API 路径模式(网关式鉴权用)',
  `status`     TINYINT         NOT NULL DEFAULT 1      COMMENT '状态: 1=启用 2=禁用',
  `created_at` DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  `updated_at` DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP
                               ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_perm_code` (`perm_code`),
  KEY `idx_perm_resource` (`resource`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='API权限表';

-- ---------------------------------------------------------------------
-- 6. RBAC: 菜单表 (前端路由/按钮级控制)
-- ---------------------------------------------------------------------
CREATE TABLE `menu` (
  `id`         BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '主键',
  `parent_id`  BIGINT UNSIGNED NULL DEFAULT NULL       COMMENT '父菜单ID, NULL=顶级',
  `menu_code`  VARCHAR(64)     NOT NULL                COMMENT '菜单编码',
  `menu_name`  VARCHAR(64)     NOT NULL                COMMENT '菜单名称',
  `menu_type`  TINYINT         NOT NULL DEFAULT 2      COMMENT '类型: 1=目录 2=菜单 3=按钮',
  `route_path` VARCHAR(255)    NULL DEFAULT NULL       COMMENT '前端路由路径',
  `icon`       VARCHAR(64)     NULL DEFAULT NULL       COMMENT '图标',
  `sort_order` INT             NOT NULL DEFAULT 0      COMMENT '排序值(升序)',
  `status`     TINYINT         NOT NULL DEFAULT 1      COMMENT '状态: 1=启用 2=禁用',
  `created_at` DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  `updated_at` DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP
                               ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_menu_code` (`menu_code`),
  KEY `idx_menu_parent` (`parent_id`),
  CONSTRAINT `fk_menu_parent` FOREIGN KEY (`parent_id`)
    REFERENCES `menu` (`id`) ON DELETE SET NULL ON UPDATE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='菜单表';

-- ---------------------------------------------------------------------
-- 7. RBAC 关联表: 账号 ↔ 角色
-- ---------------------------------------------------------------------
CREATE TABLE `account_role` (
  `id`         BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '主键',
  `account_id` BIGINT UNSIGNED NOT NULL                COMMENT '账号ID',
  `role_id`    BIGINT UNSIGNED NOT NULL                COMMENT '角色ID',
  `granted_by` BIGINT UNSIGNED NULL DEFAULT NULL       COMMENT '授权操作人账号ID',
  `granted_at` DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '授权时间',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_account_role` (`account_id`, `role_id`),
  KEY `idx_ar_role` (`role_id`),
  KEY `idx_ar_granted_by` (`granted_by`),
  CONSTRAINT `fk_ar_account` FOREIGN KEY (`account_id`)
    REFERENCES `account` (`id`) ON DELETE CASCADE ON UPDATE RESTRICT,
  CONSTRAINT `fk_ar_role` FOREIGN KEY (`role_id`)
    REFERENCES `role` (`id`) ON DELETE CASCADE ON UPDATE RESTRICT,
  CONSTRAINT `fk_ar_granter` FOREIGN KEY (`granted_by`)
    REFERENCES `account` (`id`) ON DELETE SET NULL ON UPDATE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='账号-角色关联表';

-- ---------------------------------------------------------------------
-- 8. RBAC 关联表: 角色 ↔ API权限
-- ---------------------------------------------------------------------
CREATE TABLE `role_permission` (
  `role_id`       BIGINT UNSIGNED NOT NULL COMMENT '角色ID',
  `permission_id` BIGINT UNSIGNED NOT NULL COMMENT '权限ID',
  `created_at`    DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  PRIMARY KEY (`role_id`, `permission_id`),
  KEY `idx_rp_permission` (`permission_id`),
  CONSTRAINT `fk_rp_role` FOREIGN KEY (`role_id`)
    REFERENCES `role` (`id`) ON DELETE CASCADE ON UPDATE RESTRICT,
  CONSTRAINT `fk_rp_permission` FOREIGN KEY (`permission_id`)
    REFERENCES `permission` (`id`) ON DELETE CASCADE ON UPDATE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='角色-权限关联表';

-- ---------------------------------------------------------------------
-- 9. RBAC 关联表: 角色 ↔ 菜单
-- ---------------------------------------------------------------------
CREATE TABLE `role_menu` (
  `role_id`    BIGINT UNSIGNED NOT NULL COMMENT '角色ID',
  `menu_id`    BIGINT UNSIGNED NOT NULL COMMENT '菜单ID',
  `created_at` DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  PRIMARY KEY (`role_id`, `menu_id`),
  KEY `idx_rm_menu` (`menu_id`),
  CONSTRAINT `fk_rm_role` FOREIGN KEY (`role_id`)
    REFERENCES `role` (`id`) ON DELETE CASCADE ON UPDATE RESTRICT,
  CONSTRAINT `fk_rm_menu` FOREIGN KEY (`menu_id`)
    REFERENCES `menu` (`id`) ON DELETE CASCADE ON UPDATE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='角色-菜单关联表';

-- ---------------------------------------------------------------------
-- 10. 登录审计日志: 记录每次登录尝试(成功/失败)的设备指纹
-- ---------------------------------------------------------------------
CREATE TABLE `login_log` (
  `id`            BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '主键',
  `account_id`    BIGINT UNSIGNED NULL DEFAULT NULL       COMMENT '账号ID; 账号不存在时为 NULL',
  `identity_type` VARCHAR(20)     NULL DEFAULT NULL       COMMENT '登录凭证类型',
  `identifier`    VARCHAR(255)    NULL DEFAULT NULL       COMMENT '登录标识(必须脱敏后写入)',
  `login_ip`      VARCHAR(45)     NOT NULL                COMMENT '来源IP(兼容IPv6)',
  `user_agent`    VARCHAR(512)    NULL DEFAULT NULL       COMMENT '客户端UA',
  `device_id`     VARCHAR(128)    NULL DEFAULT NULL       COMMENT '设备ID(客户端上报)',
  `session_id`    CHAR(36)        NULL DEFAULT NULL       COMMENT '登录成功时签发的会话ID',
  `status`        TINYINT         NOT NULL                COMMENT '结果: 1=成功 2=失败',
  `fail_reason`   VARCHAR(64)     NULL DEFAULT NULL       COMMENT '失败原因码: bad_password/account_locked/rate_limited...',
  `created_at`    DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '事件时间',
  PRIMARY KEY (`id`),
  KEY `idx_login_log_account_time` (`account_id`, `created_at`),
  KEY `idx_login_log_ip_time` (`login_ip`, `created_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='登录审计日志';

-- ---------------------------------------------------------------------
-- 11. 通用审计日志: 敏感操作留痕(改密/授权/注销等)
-- ---------------------------------------------------------------------
CREATE TABLE `audit_log` (
  `id`          BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '主键',
  `account_id`  BIGINT UNSIGNED NULL DEFAULT NULL       COMMENT '操作人账号ID',
  `action`      VARCHAR(64)     NOT NULL                COMMENT '动作: password_change/role_grant/account_lock...',
  `target_type` VARCHAR(32)     NULL DEFAULT NULL       COMMENT '对象类型: account/role/credential',
  `target_id`   VARCHAR(64)     NULL DEFAULT NULL       COMMENT '对象ID',
  `ip`          VARCHAR(45)     NULL DEFAULT NULL       COMMENT '来源IP',
  `user_agent`  VARCHAR(512)    NULL DEFAULT NULL       COMMENT '客户端UA',
  `detail`      JSON            NULL DEFAULT NULL       COMMENT '结构化上下文(禁止写入明文密码/PII)',
  `created_at`  DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '事件时间',
  PRIMARY KEY (`id`),
  KEY `idx_audit_account_time` (`account_id`, `created_at`),
  KEY `idx_audit_action_time` (`action`, `created_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='通用审计日志';
