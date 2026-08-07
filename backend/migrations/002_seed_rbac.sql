-- =====================================================================
-- RBAC 初始化种子数据: 内置角色 / API 权限 / 菜单 / 授权关系
-- 执行前置: 001_schema.sql
-- =====================================================================
USE `fastapi_langchain_example`;

-- ---- 内置角色 --------------------------------------------------------
INSERT INTO `role` (`role_code`, `role_name`, `description`, `is_builtin`) VALUES
  ('admin',    '超级管理员', '拥有全部权限(*)',        1),
  ('operator', '运营人员',   '账号查看与风控操作',     0),
  ('member',   '普通用户',   '注册默认角色',           1);

-- ---- API 权限 --------------------------------------------------------
INSERT INTO `permission` (`perm_code`, `perm_name`, `resource`, `api_method`, `api_path`) VALUES
  ('account:read',    '查看账号列表', 'account', 'GET',    '/api/v1/admin/accounts'),
  ('account:lock',    '锁定/解锁账号', 'account', 'POST',  '/api/v1/admin/accounts/*/status'),
  ('rbac:read',       '查看角色权限', 'rbac',    'GET',    '/api/v1/admin/roles'),
  ('rbac:assign',     '分配用户角色', 'rbac',    'POST',   '/api/v1/admin/accounts/*/roles'),
  ('audit:read',      '查看审计日志', 'audit',   'GET',    '/api/v1/admin/audit-logs');

-- ---- admin 角色拥有全部权限(通配), operator 只读 + 锁定, member 无管理权限 ----
INSERT INTO `role_permission` (`role_id`, `permission_id`)
SELECT r.id, p.id FROM `role` r JOIN `permission` p
  ON (r.role_code = 'admin')
  OR (r.role_code = 'operator' AND p.perm_code IN ('account:read', 'account:lock', 'audit:read'));

-- ---- 菜单 -------------------------------------------------------------
INSERT INTO `menu` (`parent_id`, `menu_code`, `menu_name`, `menu_type`, `route_path`, `sort_order`) VALUES
  (NULL, 'system',        '系统管理', 1, '/system',          100);
-- 上一条占位行仅用于保持自增连续, 实际菜单按下面方式追加:
DELETE FROM `menu` WHERE `menu_code` IS NULL;

INSERT INTO `menu` (`parent_id`, `menu_code`, `menu_name`, `menu_type`, `route_path`, `sort_order`)
SELECT m.id, 'system.accounts', '账号管理', 2, '/system/accounts', 1 FROM `menu` m WHERE m.menu_code = 'system';
INSERT INTO `menu` (`parent_id`, `menu_code`, `menu_name`, `menu_type`, `route_path`, `sort_order`)
SELECT m.id, 'system.roles', '角色权限', 2, '/system/roles', 2 FROM `menu` m WHERE m.menu_code = 'system';
INSERT INTO `menu` (`parent_id`, `menu_code`, `menu_name`, `menu_type`, `route_path`, `sort_order`)
SELECT m.id, 'system.audit', '审计日志', 2, '/system/audit', 3 FROM `menu` m WHERE m.menu_code = 'system';

-- ---- admin / operator 可见菜单 ----------------------------------------
INSERT INTO `role_menu` (`role_id`, `menu_id`)
SELECT r.id, m.id FROM `role` r JOIN `menu` m
 WHERE r.role_code IN ('admin', 'operator');
