-- =====================================================================
-- 将 initdb 创建的初始库标记为 Alembic head (e7f9a3b5c1d6)
--
-- docker-entrypoint-initdb.d 用本目录的 SQL 建初始库; 打上 alembic 版本
-- 标记后, 后续 `alembic upgrade head` 只做增量迁移, 不会重建表结构。
-- 注意: 迁移 head 变化时(新增迁移或 squash), 必须同步更新此文件;
-- 同时要保证 head 之前的迁移内容已并入 001~003 / 005 的初始 SQL
-- (技能权限点见 002_seed_rbac.sql 与 9c4a1f7b2d65_seed_skill_permissions.py;
--  学习内核表结构与 learning:* 权限点见 005_learning.sql 与
--  f3a9c1d47b60_add_learning_tables.py;
--  智能体注册表与 agent:* 权限点见 006_agents.sql 与
--  e7f9a3b5c1d6_add_agents_table.py),
-- 否则新建库会缺表或缺数据。tests/test_migrations.py 守卫这些约束。
--
-- 本文件按文件名顺序在 005_learning.sql **之前**执行, 但 initdb 是同一次
-- 初始化里的连续脚本, stamp 只是往 alembic_version 写一行, 不依赖后面的表。
-- =====================================================================
USE `auth_service`;

CREATE TABLE IF NOT EXISTS `alembic_version` (
  `version_num` VARCHAR(32) NOT NULL,
  CONSTRAINT `alembic_version_pkc` PRIMARY KEY (`version_num`)
);

INSERT INTO `alembic_version` (`version_num`) VALUES ('e7f9a3b5c1d6');
