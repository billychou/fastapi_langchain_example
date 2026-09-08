"""Seed skill permissions

技能(skills)子系统的 RBAC 权限点种子(纯数据迁移, 不改表结构)。

两类权限点:

- ``skill:admin``: 管理端能力 —— ``POST /api/v1/skills/reload`` 强制热重载,
  以及 ``GET /api/v1/skills?scope=all`` 查看含门禁技能的全量目录与加载失败原因。
  内置 ``admin`` 角色在运行时展开为通配 ``*``, 因此无需额外授权行。
- ``skill:<name>:use``: 技能门禁 —— 由技能自己 frontmatter 里的
  ``visibility: permission`` + ``requires_permissions`` 声明。这里种下随仓库内置的
  ``expense-report`` 技能所需的那一个, 否则除 admin 外永远无人可用。

技能**目录/详情/附件的读接口不引入权限点**(登录即可, 只返回该账号可见的技能),
与 ``/api/v1/account/me`` 的自服务定位一致, 因此这里没有 ``skill:list``。

新建库不会重放本迁移(``migrations/004_alembic_stamp.sql`` 直接把库标记为 head),
所以同一批权限点也写进了 ``migrations/002_seed_rbac.sql``; 两条路径由
``tests/test_migrations.py`` 守卫一致性。SQL 用 ``INSERT IGNORE``(MySQL)保证幂等,
重复执行或与 002 种子叠加都不会报错。

Revision ID: 9c4a1f7b2d65
Revises: 409a7d6da51e
Create Date: 2026-09-08 11:20:00.000000

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = '9c4a1f7b2d65'
down_revision: str | Sequence[str] | None = '409a7d6da51e'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# (perm_code, perm_name, resource, api_method, api_path)
PERMISSIONS: tuple[tuple[str, str, str, str | None, str | None], ...] = (
    ("skill:admin", "管理技能", "skill", "POST", "/api/v1/skills/reload"),
    ("skill:expense-report:use", "使用报销技能", "skill", None, None),
)

# 角色 → 权限: 给 operator 一个非通配的门禁技能示例(member 保持无管理/门禁权限)
GRANTS: dict[str, tuple[str, ...]] = {
    "operator": ("skill:expense-report:use",),
}

_PERM_CODES = tuple(code for code, *_ in PERMISSIONS)


def _quoted(values: Sequence[str]) -> str:
    return ", ".join("'" + value.replace("'", "''") + "'" for value in values)


def upgrade() -> None:
    """Upgrade schema."""
    for code, name, resource, method, path in PERMISSIONS:
        method_sql = "NULL" if method is None else f"'{method}'"
        path_sql = "NULL" if path is None else f"'{path}'"
        op.execute(
            "INSERT IGNORE INTO `permission` "
            "(`perm_code`, `perm_name`, `resource`, `api_method`, `api_path`) VALUES "
            f"('{code}', '{name}', '{resource}', {method_sql}, {path_sql})"
        )

    for role_code, perm_codes in GRANTS.items():
        op.execute(
            "INSERT IGNORE INTO `role_permission` (`role_id`, `permission_id`) "
            "SELECT r.id, p.id FROM `role` r JOIN `permission` p "
            f"ON r.role_code = '{role_code}' AND p.perm_code IN ({_quoted(perm_codes)})"
        )


def downgrade() -> None:
    """Downgrade schema."""
    codes = _quoted(_PERM_CODES)
    granted = _quoted(sorted({code for codes_ in GRANTS.values() for code in codes_}))
    op.execute(
        "DELETE rp FROM `role_permission` rp JOIN `permission` p ON p.id = rp.permission_id "
        f"WHERE p.perm_code IN ({granted})"
    )
    op.execute(f"DELETE FROM `permission` WHERE `perm_code` IN ({codes})")
