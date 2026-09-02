-- =====================================================================
-- 将 initdb 创建的初始标记为 Alembic head (409a7d6da51e)
--
-- docker-entrypoint-initdb.d 用本目录的 SQL 建初始库; 打上 alembic 版本
-- 标记后, 后续 `alembic upgrade head` 只做增量迁移, 不会重建表结构。
-- 注意: 初始迁移版本变化时(如 squash), 必须同步更新此文件。
-- =====================================================================
USE `auth_service`;

CREATE TABLE IF NOT EXISTS `alembic_version` (
  `version_num` VARCHAR(32) NOT NULL,
  CONSTRAINT `alembic_version_pkc` PRIMARY KEY (`version_num`)
);

INSERT INTO `alembic_version` (`version_num`) VALUES ('409a7d6da51e');
