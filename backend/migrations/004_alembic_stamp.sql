-- =====================================================================
-- 将 initdb 创建的初始库标记为 Alembic head (9c4a1f7b2d65)
--
-- docker-entrypoint-initdb.d 用本目录的 SQL 建初始库; 打上 alembic 版本
-- 标记后, 后续 `alembic upgrade head` 只做增量迁移, 不会重建表结构。
-- 注意: 迁移 head 变化时(新增迁移或 squash), 必须同步更新此文件;
-- 同时要保证 head 之前的数据迁移内容已并入 001~003 的初始 SQL
-- (技能权限点见 002_seed_rbac.sql 与 9c4a1f7b2d65_seed_skill_permissions.py),
-- 否则新建库会缺数据。tests/test_migrations.py 守卫这两条约束。
-- =====================================================================
USE `auth_service`;

CREATE TABLE IF NOT EXISTS `alembic_version` (
  `version_num` VARCHAR(32) NOT NULL,
  CONSTRAINT `alembic_version_pkc` PRIMARY KEY (`version_num`)
);

INSERT INTO `alembic_version` (`version_num`) VALUES ('9c4a1f7b2d65');
