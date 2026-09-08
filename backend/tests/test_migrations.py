"""Alembic 迁移链守卫: 不依赖数据库, 可在 CI 直接运行。"""
from __future__ import annotations

from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent

INITIAL_REVISION = "409a7d6da51e"
HEAD_REVISION = "9c4a1f7b2d65"
SEED_SQL = BACKEND_DIR / "migrations" / "002_seed_rbac.sql"

_EXPECTED_TABLES = {
    "account",
    "user_profile",
    "auth_credential",
    "role",
    "permission",
    "menu",
    "account_role",
    "role_permission",
    "role_menu",
    "agent_threads",
    "login_log",
    "audit_log",
}


def test_alembic_script_has_single_head():
    """迁移链必须只有一个 head, 否则多人并行开发会产生分叉。"""
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    script = ScriptDirectory.from_config(cfg)
    assert script.get_heads() == [HEAD_REVISION]


def test_skill_permission_migration_chains_onto_initial_schema():
    """技能权限点是一条纯数据迁移, 必须直接挂在初始 schema 之后(链不断不分叉)。"""
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    revision = ScriptDirectory.from_config(cfg).get_revision(HEAD_REVISION)
    assert revision.down_revision == INITIAL_REVISION
    assert {code for code, *_ in revision.module.PERMISSIONS} == {
        "skill:admin",
        "skill:expense-report:use",
    }


def test_initdb_stamp_matches_alembic_head():
    """004_alembic_stamp.sql 的版本必须与迁移 head 一致, 否则新建库会重放迁移。"""
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    head = ScriptDirectory.from_config(cfg).get_heads()[0]
    stamp_sql = (BACKEND_DIR / "migrations" / "004_alembic_stamp.sql").read_text()
    assert f"VALUES ('{head}')" in stamp_sql


def test_data_migration_seeds_are_also_in_initdb_sql():
    """新建库不重放迁移(直接 stamp 到 head), 所以数据迁移的种子必须同样写进初始 SQL。"""
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    revision = ScriptDirectory.from_config(cfg).get_revision(HEAD_REVISION)
    seed = SEED_SQL.read_text()
    for code, *_ in revision.module.PERMISSIONS:
        assert f"('{code}'" in seed, f"{code} 未写入 migrations/002_seed_rbac.sql"
    for role_code, perm_codes in revision.module.GRANTS.items():
        for code in perm_codes:
            assert code in seed, f"{role_code} 的 {code} 授权未写入初始 SQL"


def test_gated_bundled_skills_require_seeded_permissions():
    """内置门禁技能声明的权限点必须真实存在, 否则除 admin(通配 *) 外永远无人可用。"""
    from app.skills import scan_skills

    result = scan_skills(BACKEND_DIR / "skills")
    assert not result.errors, result.errors
    required = {code for skill in result.skills.values() for code in skill.requires_permissions}
    assert required, "内置技能应至少保留一个门禁示例(演示 RBAC 与技能的结合)"
    seed = SEED_SQL.read_text()
    for code in sorted(required):
        assert f"('{code}'" in seed, f"技能门禁权限 {code} 未在 RBAC 种子里声明"


def test_all_models_registered_in_metadata():
    """autogenerate 的正确性依赖模型全部注册到 Base.metadata。"""
    import app.models  # noqa: F401
    from app.db.session import Base

    assert _EXPECTED_TABLES <= set(Base.metadata.tables)
