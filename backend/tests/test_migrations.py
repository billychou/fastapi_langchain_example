"""Alembic 迁移链守卫: 不依赖数据库, 可在 CI 直接运行。

守卫四件事(每一条都对应过真实的回归):

1. **链不断不分叉**: 单 head, 且顺序是 初始 schema → 技能权限 → 学习内核表 → 智能体注册表。
2. **两条建库路径等价**: 新建库走 ``migrations/*.sql``(004 直接 stamp 到 head, 不重放
   迁移), 既有库走 ``alembic upgrade head`` —— 所以每条数据迁移的种子必须同样出现在
   initdb SQL 里, 每条 DDL 迁移的表结构必须与 ``Base.metadata`` 逐列一致。
3. **门禁权限点真实存在**: 技能声明的 ``requires_permissions`` 必须被种下,
   否则除 admin(通配 ``*``)外永远无人可用。
4. **downgrade 完整**: 建了几张表就要 drop 几张, 且顺序反过来(外键依赖)。
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

BACKEND_DIR = Path(__file__).resolve().parent.parent
MIGRATIONS_DIR = BACKEND_DIR / "migrations"

INITIAL_REVISION = "409a7d6da51e"
SKILL_REVISION = "9c4a1f7b2d65"
LEARNING_REVISION = "f3a9c1d47b60"
HEAD_REVISION = "e7f9a3b5c1d6"
SEED_SQL = MIGRATIONS_DIR / "002_seed_rbac.sql"
LEARNING_SQL = MIGRATIONS_DIR / "005_learning.sql"
AGENTS_SQL = MIGRATIONS_DIR / "006_agents.sql"

#: 学习内核新增的五张表(顺序 = 建表顺序 = 外键依赖顺序)。
LEARNING_TABLES = (
    "learning_concept",
    "learning_atom",
    "learning_atom_edge",
    "learner_explanation",
    "mastery_ledger",
)

#: 智能体注册表新增的一张表。
AGENT_TABLES = ("agent",)

AGENT_PERMISSIONS = {
    "agent:read",
    "agent:write",
}

AGENT_SEED_KEYS = {
    "supervisor_main",
    "k8s-agent",
    "edu-agent",
    "rag-agent",
    "metric-agent",
    "log-agent",
}

LEARNING_PERMISSIONS = {
    "learning:self:read",
    "learning:explanation:write",
    "learning:report:read",
    "learning:atom:admin",
}

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
    *LEARNING_TABLES,
    *AGENT_TABLES,
}


# --------------------------------------------------------------------------- 工具


def _script():
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    return ScriptDirectory.from_config(cfg)


def _chain() -> list[Any]:
    """从 head 往根走的迁移对象列表(单 head 前提下就是整条链)。"""
    script = _script()
    revisions = []
    revision = script.get_revision(HEAD_REVISION)
    while revision is not None:
        revisions.append(revision)
        revision = (
            script.get_revision(revision.down_revision) if revision.down_revision else None
        )
    return revisions


class _OpRecorder:
    """``alembic.op`` 的替身: 只记录 DDL/DML 调用, 不连数据库。

    ``get_bind()`` 返回 ``None``, 因此 ``_bigint_id_type()`` 走离线回退分支
    (返回模型声明的有符号 ``BIGINT``) —— 与 ``Base.metadata`` 可直接比较。
    """

    def __init__(self) -> None:
        self.tables: dict[str, dict[str, Any]] = {}
        self.indexes: dict[str, dict[str, Any]] = {}
        self.executed: list[str] = []
        self.dropped: list[str] = []

    def get_bind(self) -> None:
        return None

    def create_table(self, name: str, *args: Any, **kwargs: Any) -> None:
        self.tables[name] = {"args": args, "kwargs": kwargs}

    def create_index(self, name: str, table: str, columns: Any, **kwargs: Any) -> None:
        self.indexes[name] = {"table": table, "columns": list(columns), **kwargs}

    def execute(self, sql: Any) -> None:
        self.executed.append(str(sql))

    def drop_table(self, name: str) -> None:
        self.dropped.append(name)


def _load(revision: str) -> Any:
    """按文件路径加载迁移模块。

    不能用 ``importlib.import_module("alembic.versions.xxx")``: 仓库里的 ``alembic/``
    是命名空间目录, 导入时会解析到**已安装的 alembic 库**, 拿不到 versions 子模块。
    """
    import importlib.util

    matches = sorted((BACKEND_DIR / "alembic" / "versions").glob(f"{revision}_*.py"))
    assert len(matches) == 1, f"{revision} 对应的迁移文件应唯一, 实际: {matches}"
    spec = importlib.util.spec_from_file_location(f"_migration_{revision}", matches[0])
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run(revision: str, function: str) -> _OpRecorder:
    """在 ``op`` 替身下执行某个迁移的 ``upgrade``/``downgrade``。"""
    module = _load(revision)
    recorder = _OpRecorder()
    module.op = recorder
    getattr(module, function)()
    return recorder


def _migration_columns(call: dict[str, Any]) -> dict[str, Any]:
    """迁移 ``create_table`` 里的列: ``name -> sa.Column``。"""
    import sqlalchemy as sa

    return {arg.name: arg for arg in call["args"] if isinstance(arg, sa.Column)}


def _constraint_names(constraint: Any) -> tuple[str, ...]:
    """约束涉及的列名: 未挂到表上时读 ``_pending_colargs``, 已挂上时读 ``columns``。"""
    pending = getattr(constraint, "_pending_colargs", None)
    if pending:
        return tuple(str(item) for item in pending)
    return tuple(column.name for column in constraint.columns)


def _compiled(column_type: Any) -> str:
    from sqlalchemy.dialects import mysql

    return str(column_type.compile(dialect=mysql.dialect()))


def _default(column: Any) -> str | None:
    if column.server_default is None:
        return None
    return str(column.server_default.arg)


def _sql_tables(path: Path) -> dict[str, set[str]]:
    """从 initdb SQL 里抠出 ``表名 -> 列名集合``(只看行首的反引号列定义)。"""
    tables: dict[str, set[str]] = {}
    pattern = re.compile(r"CREATE TABLE IF NOT EXISTS `(\w+)` \((.*?)\n\) ENGINE", re.S)
    for match in pattern.finditer(path.read_text(encoding="utf-8")):
        name, body = match.group(1), match.group(2)
        tables[name] = {
            line.strip().split("`")[1] for line in body.splitlines() if line.strip().startswith("`")
        }
    return tables


# --------------------------------------------------------------------------- 迁移链


def test_alembic_script_has_single_head():
    """迁移链必须只有一个 head, 否则多人并行开发会产生分叉。"""
    assert _script().get_heads() == [HEAD_REVISION]


def test_migration_chain_is_linear():
    """链是 初始 schema → 技能权限 → 学习内核表: 断链或分叉都会让老库升级失败。"""
    assert [revision.revision for revision in _chain()] == [
        HEAD_REVISION,
        LEARNING_REVISION,
        SKILL_REVISION,
        INITIAL_REVISION,
    ]


def test_skill_permission_migration_chains_onto_initial_schema():
    """技能权限点是一条纯数据迁移, 必须直接挂在初始 schema 之后(链不断不分叉)。"""
    revision = _script().get_revision(SKILL_REVISION)
    assert revision.down_revision == INITIAL_REVISION
    assert {code for code, *_ in revision.module.PERMISSIONS} == {
        "skill:admin",
        "skill:expense-report:use",
    }


def test_learning_migration_chains_onto_skill_permissions():
    """学习内核表挂在技能权限之后, 且创建的就是那五张表(多一张少一张都算漂移)。"""
    revision = _script().get_revision(LEARNING_REVISION)
    assert revision.down_revision == SKILL_REVISION
    assert revision.module.TABLES == LEARNING_TABLES
    recorder = _run(LEARNING_REVISION, "upgrade")
    assert tuple(recorder.tables) == LEARNING_TABLES


def test_learning_migration_seeds_documented_permissions():
    """权限点与授权关系是产品口径: member 自助读写, operator 读报告, admin 走通配 *。"""
    module = _script().get_revision(LEARNING_REVISION).module
    assert {code for code, *_ in module.PERMISSIONS} == LEARNING_PERMISSIONS
    assert module.GRANTS == {
        "member": ("learning:self:read", "learning:explanation:write"),
        "operator": ("learning:report:read",),
    }
    recorder = _run(LEARNING_REVISION, "upgrade")
    seeded = [sql for sql in recorder.executed if "INSERT IGNORE INTO `permission`" in sql]
    assert len(seeded) == len(LEARNING_PERMISSIONS)
    granted = [sql for sql in recorder.executed if "`role_permission`" in sql]
    assert len(granted) == len(module.GRANTS)


def test_learning_migration_downgrade_drops_everything_it_created():
    """downgrade 要能干净回滚: 倒序 drop 五张表 + 删掉自己种下的权限点。"""
    recorder = _run(LEARNING_REVISION, "downgrade")
    assert recorder.dropped == list(reversed(LEARNING_TABLES))
    deletes = [sql for sql in recorder.executed if sql.startswith("DELETE")]
    assert len(deletes) == 2
    for code in LEARNING_PERMISSIONS:
        assert code in deletes[-1], f"{code} 未在 downgrade 中清理"


def test_bigint_id_type_reads_the_referenced_column():
    """``_bigint_id_type()`` 必须读真实类型: MySQL 外键要求两侧含 UNSIGNED 完全一致。"""
    import sqlalchemy as sa

    module = _load(LEARNING_REVISION)
    engine = sa.create_engine("sqlite://")
    sa.Table(
        "account",
        sa.MetaData(),
        sa.Column("id", sa.String(42), primary_key=True),
    ).create(engine)

    class _Op:
        @staticmethod
        def get_bind():
            return engine

    original = module.op
    module.op = _Op  # type: ignore[assignment]
    try:
        resolved = module._bigint_id_type()
    finally:
        module.op = original  # type: ignore[assignment]
    assert _compiled(resolved) == "VARCHAR(42)"

    # 拿不到连接(离线 / 替身)时回退到模型声明的有符号 BIGINT
    module.op = _OpRecorder()
    assert _compiled(module._bigint_id_type()) == "BIGINT"
    engine.dispose()


# --------------------------------------------------------------------------- initdb 一致性


def test_initdb_stamp_matches_alembic_head():
    """004_alembic_stamp.sql 的版本必须与迁移 head 一致, 否则新建库会重放迁移。"""
    head = _script().get_heads()[0]
    stamp_sql = (MIGRATIONS_DIR / "004_alembic_stamp.sql").read_text()
    assert f"VALUES ('{head}')" in stamp_sql


def test_data_migration_seeds_are_also_in_initdb_sql():
    """新建库不重放迁移(直接 stamp 到 head), 所以数据迁移的种子必须同样写进初始 SQL。"""
    seeded = [revision for revision in _chain() if hasattr(revision.module, "PERMISSIONS")]
    assert {revision.revision for revision in seeded} == {
        SKILL_REVISION,
        LEARNING_REVISION,
        HEAD_REVISION,
    }
    for revision in seeded:
        module = revision.module
        seed_path = MIGRATIONS_DIR / module.INITDB_SEED_SQL
        assert seed_path.exists(), f"{revision.revision} 指向的种子文件不存在: {seed_path}"
        seed = seed_path.read_text(encoding="utf-8")
        for code, *_ in module.PERMISSIONS:
            assert f"('{code}'" in seed, f"{code} 未写入 migrations/{seed_path.name}"
        for role_code, perm_codes in module.GRANTS.items():
            for code in perm_codes:
                assert code in seed, f"{role_code} 的 {code} 授权未写入 {seed_path.name}"


def test_initdb_learning_sql_declares_every_model_column():
    """005_learning.sql 与模型逐列一致: 新建库不能缺列(否则 API 一上线就 500)。"""
    import app.models  # noqa: F401
    from app.db.session import Base

    sql_tables = _sql_tables(LEARNING_SQL)
    assert set(sql_tables) == set(LEARNING_TABLES)
    for table in LEARNING_TABLES:
        model_columns = set(Base.metadata.tables[table].columns.keys())
        assert sql_tables[table] == model_columns, f"{table} 的列在 SQL 与模型之间不一致"


def test_initdb_learning_sql_cascades_on_account_deletion():
    """学习者数据挂在账号体系上: 注销账号必须级联清理(隐私口径, 见 003 的设计原则)。"""
    sql = LEARNING_SQL.read_text(encoding="utf-8")
    for table in ("learner_explanation", "mastery_ledger"):
        block = sql.split(f"CREATE TABLE IF NOT EXISTS `{table}`", 1)[1].split("ENGINE=InnoDB", 1)[0]
        assert "REFERENCES `account` (`id`) ON DELETE CASCADE" in block, f"{table} 缺少级联外键"


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


# --------------------------------------------------------------------------- DDL ↔ 模型


def test_all_models_registered_in_metadata():
    """autogenerate 的正确性依赖模型全部注册到 Base.metadata。"""
    import app.models  # noqa: F401
    from app.db.session import Base

    assert _EXPECTED_TABLES <= set(Base.metadata.tables)


def test_learning_migration_ddl_matches_models():
    """迁移建的表必须与模型逐列一致: 列名/类型/可空/服务端默认值/索引/唯一约束。

    这条守卫的是「两条建库路径等价」: 迁移漂移一次, 老库与新库的 schema 就永久分叉,
    而 ``alembic revision --autogenerate`` 只会看到模型, 看不到已上线的库。
    """
    import app.models  # noqa: F401
    import sqlalchemy as sa
    from app.db.session import Base

    recorder = _run(LEARNING_REVISION, "upgrade")
    for table in LEARNING_TABLES:
        model = Base.metadata.tables[table]
        call = recorder.tables[table]
        migration_columns = _migration_columns(call)

        assert set(migration_columns) == set(model.columns.keys()), f"{table} 列名不一致"
        for name, column in model.columns.items():
            created = migration_columns[name]
            assert _compiled(created.type) == _compiled(column.type), f"{table}.{name} 类型不一致"
            assert created.nullable == column.nullable, f"{table}.{name} 可空性不一致"
            assert _default(created) == _default(column), f"{table}.{name} 服务端默认值不一致"

        model_uniques = {
            (constraint.name, _constraint_names(constraint))
            for constraint in model.constraints
            if isinstance(constraint, sa.UniqueConstraint)
        }
        migration_uniques = {
            (arg.name, _constraint_names(arg))
            for arg in call["args"]
            if isinstance(arg, sa.UniqueConstraint)
        }
        assert migration_uniques == model_uniques, f"{table} 唯一约束不一致"

        model_indexes = {
            index.name: (tuple(column.name for column in index.columns), bool(index.unique))
            for index in model.indexes
        }
        migration_indexes = {
            name: (tuple(info["columns"]), bool(info.get("unique", False)))
            for name, info in recorder.indexes.items()
            if info["table"] == table
        }
        assert migration_indexes == model_indexes, f"{table} 索引不一致"


def test_learning_migration_declares_foreign_keys():
    """外键是级联清理与图完整性的来源, 迁移里必须逐条声明(名字由 MySQL 生成)。"""
    import sqlalchemy as sa

    recorder = _run(LEARNING_REVISION, "upgrade")
    specs: dict[str, set[tuple[str, str]]] = {}
    for table, call in recorder.tables.items():
        specs[table] = {
            (_constraint_names(arg), arg.elements[0]._get_colspec())
            for arg in call["args"]
            if isinstance(arg, sa.ForeignKeyConstraint)
        }

    assert specs["learning_atom"] == {(("concept_id",), "learning_concept.id")}
    assert specs["learning_atom_edge"] == {
        (("from_atom_id",), "learning_atom.id"),
        (("to_atom_id",), "learning_atom.id"),
    }
    assert specs["learner_explanation"] == {
        (("account_id",), "account.id"),
        (("concept_id",), "learning_concept.id"),
    }
    assert specs["mastery_ledger"] == {
        (("account_id",), "account.id"),
        (("atom_id",), "learning_atom.id"),
    }
    for table in LEARNING_TABLES:
        for arg in recorder.tables[table]["args"]:
            if isinstance(arg, sa.ForeignKeyConstraint):
                assert arg.ondelete == "CASCADE", f"{table} 的外键必须级联删除"


# --------------------------------------------------------------------------- 智能体注册表


def test_agents_migration_chains_onto_learning_tables():
    """智能体注册表挂在学习内核表之后, 且只建 agent 一张表。"""
    revision = _script().get_revision(HEAD_REVISION)
    assert revision.down_revision == LEARNING_REVISION
    assert revision.module.TABLES == AGENT_TABLES
    recorder = _run(HEAD_REVISION, "upgrade")
    assert tuple(recorder.tables) == AGENT_TABLES


def test_agents_migration_seeds_documented_permissions():
    """权限口径: 全部角色可读, operator 可管理, admin 走通配 *。"""
    module = _script().get_revision(HEAD_REVISION).module
    assert {code for code, *_ in module.PERMISSIONS} == AGENT_PERMISSIONS
    assert module.GRANTS == {
        "member": ("agent:read",),
        "operator": ("agent:read", "agent:write"),
    }
    recorder = _run(HEAD_REVISION, "upgrade")
    seeded = [sql for sql in recorder.executed if "INSERT IGNORE INTO `permission`" in sql]
    assert len(seeded) == len(AGENT_PERMISSIONS)
    granted = [sql for sql in recorder.executed if "`role_permission`" in sql]
    assert len(granted) == len(module.GRANTS)


def test_agents_migration_seeds_the_six_launch_agents():
    """平台首屏六个智能体是产品口径: 少一个种子, 新建库的首屏就缺一块。"""
    recorder = _run(HEAD_REVISION, "upgrade")
    inserts = [sql for sql in recorder.executed if "INSERT IGNORE INTO `agent`" in sql]
    assert len(inserts) == 1
    for key in AGENT_SEED_KEYS:
        assert key in inserts[0], f"{key} 未写入智能体种子"


def test_agents_migration_downgrade_drops_everything_it_created():
    """downgrade 干净回滚: 清种子行 + 删权限点 + drop 表。"""
    recorder = _run(HEAD_REVISION, "downgrade")
    assert recorder.dropped == list(reversed(AGENT_TABLES))
    deletes = [sql for sql in recorder.executed if sql.startswith("DELETE")]
    assert len(deletes) == 3
    for code in AGENT_PERMISSIONS:
        assert code in deletes[-1], f"{code} 未在 downgrade 中清理"


def test_agents_migration_ddl_matches_models():
    """agent 表迁移 DDL 与模型逐列一致(两条建库路径等价的守卫)。"""
    import app.models  # noqa: F401
    import sqlalchemy as sa
    from app.db.session import Base

    recorder = _run(HEAD_REVISION, "upgrade")
    for table in AGENT_TABLES:
        model = Base.metadata.tables[table]
        call = recorder.tables[table]
        migration_columns = _migration_columns(call)

        assert set(migration_columns) == set(model.columns.keys()), f"{table} 列名不一致"
        for name, column in model.columns.items():
            created = migration_columns[name]
            assert _compiled(created.type) == _compiled(column.type), f"{table}.{name} 类型不一致"
            assert created.nullable == column.nullable, f"{table}.{name} 可空性不一致"
            assert _default(created) == _default(column), f"{table}.{name} 服务端默认值不一致"

        model_uniques = {
            (constraint.name, _constraint_names(constraint))
            for constraint in model.constraints
            if isinstance(constraint, sa.UniqueConstraint)
        }
        migration_uniques = {
            (arg.name, _constraint_names(arg))
            for arg in call["args"]
            if isinstance(arg, sa.UniqueConstraint)
        }
        assert migration_uniques == model_uniques, f"{table} 唯一约束不一致"

        model_indexes = {
            index.name: (tuple(column.name for column in index.columns), bool(index.unique))
            for index in model.indexes
        }
        migration_indexes = {
            name: (tuple(info["columns"]), bool(info.get("unique", False)))
            for name, info in recorder.indexes.items()
            if info["table"] == table
        }
        assert migration_indexes == model_indexes, f"{table} 索引不一致"


def test_initdb_agents_sql_declares_every_model_column():
    """006_agents.sql 与模型逐列一致, 且六个种子智能体都在。"""
    import app.models  # noqa: F401
    from app.db.session import Base

    sql_tables = _sql_tables(AGENTS_SQL)
    assert set(sql_tables) == set(AGENT_TABLES)
    for table in AGENT_TABLES:
        model_columns = set(Base.metadata.tables[table].columns.keys())
        assert sql_tables[table] == model_columns, f"{table} 的列在 SQL 与模型之间不一致"
    seed = AGENTS_SQL.read_text(encoding="utf-8")
    for key in AGENT_SEED_KEYS:
        assert f"('{key}'" in seed, f"{key} 未写入 migrations/006_agents.sql"
    for code in AGENT_PERMISSIONS:
        assert f"('{code}'" in seed, f"{code} 未写入 migrations/006_agents.sql"
