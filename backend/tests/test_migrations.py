"""Alembic 迁移链守卫: 不依赖数据库, 可在 CI 直接运行。"""
from __future__ import annotations

from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent

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
    assert script.get_heads() == ["409a7d6da51e"]


def test_initdb_stamp_matches_alembic_head():
    """004_alembic_stamp.sql 的版本必须与迁移 head 一致, 否则新建库会重放迁移。"""
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    head = ScriptDirectory.from_config(cfg).get_heads()[0]
    stamp_sql = (BACKEND_DIR / "migrations" / "004_alembic_stamp.sql").read_text()
    assert f"VALUES ('{head}')" in stamp_sql


def test_all_models_registered_in_metadata():
    """autogenerate 的正确性依赖模型全部注册到 Base.metadata。"""
    import app.models  # noqa: F401
    from app.db.session import Base

    assert _EXPECTED_TABLES <= set(Base.metadata.tables)
