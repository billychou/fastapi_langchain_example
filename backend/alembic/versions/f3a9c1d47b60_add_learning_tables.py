"""Add learning tables

P0 教学内核的持久化层: 知识图三张表 + 学习证据两张表。

表结构与 ``app/models/learning.py`` 逐列对应(列名/类型/可空/服务端默认值),
设计依据见 ``docs/education-agent-architecture.md`` §4:

- ``learning_concept`` / ``learning_atom`` / ``learning_atom_edge``: 学科无关的概念图,
  全局共享。``slug`` 与 ``atom_key`` 是内核(``app/pedagogy``)使用的稳定字符串 ID,
  自增 BIGINT 主键只在数据库内部流通; ``verified=0`` 的 Atom 不得进入掌握度记账。
- ``learner_explanation``: 费曼循环每一轮自述的原始证据(原话 + claims + gap 报告)。
- ``mastery_ledger``: 每个 (账号, Atom) 一行, ``UNIQUE(account_id, atom_id)``;
  比文档草图多 ``tau_days`` / ``depth`` 两列, 使 ``MasteryEntry`` 能整条往返
  (``m + α·w·(1−m)`` 与 ``m·exp(−Δt/τ)`` 的输入全部可回溯), ``idx_mastery_review``
  只为「今日复习队列」这一条查询服务。

新建库不会重放本迁移(``migrations/004_alembic_stamp.sql`` 直接把库标记为 head),
所以同一批 DDL 与权限种子也写进了 ``migrations/005_learning.sql``; 两条路径的一致性由
``tests/test_migrations.py`` 守卫(DDL 对齐 ``Base.metadata``, 种子对齐
``INITDB_SEED_SQL`` 指向的文件)。

主键/外键列的类型在运行时读一次 ``account.id``(见 :func:`_bigint_id_type`):
MySQL 建外键要求两侧类型**含 UNSIGNED 完全一致**, 而本仓库的 initdb SQL 用
``BIGINT UNSIGNED``、纯 Alembic 路径用有符号 ``BIGINT``, 老库升级只跑本迁移,
不自省就会在其中一条路径上踩 errno 150。

权限点(``learning:*``; 接口在后续提交落地, 先播种避免上线即 403):

- ``learning:self:read``          学习者读自己的概念/证据/掌握度/复习队列
- ``learning:explanation:write``  写入讲解证据(费曼循环落库的唯一入口)
- ``learning:report:read``        家长/教师读学习报告(P1 才有接口, 先播种)
- ``learning:atom:admin``         维护概念图与 Atom(教研/管理端)

内置 ``admin`` 角色运行时展开为通配 ``*``, 因此无需授权行; ``member`` 拿自助两项,
``operator`` 拿报告只读。种子 SQL 用 ``INSERT IGNORE``(MySQL)保证幂等。

Revision ID: f3a9c1d47b60
Revises: 9c4a1f7b2d65
Create Date: 2026-09-14 10:30:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

# revision identifiers, used by Alembic.
revision: str = 'f3a9c1d47b60'
down_revision: str | Sequence[str] | None = '9c4a1f7b2d65'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: 本迁移的数据种子在 initdb 路径上的对应文件(新建库不重放迁移, 见模块 docstring)。
INITDB_SEED_SQL: str = "005_learning.sql"

#: 本迁移创建的表(建表顺序; downgrade 倒序 drop)。
TABLES: tuple[str, ...] = (
    "learning_concept",
    "learning_atom",
    "learning_atom_edge",
    "learner_explanation",
    "mastery_ledger",
)

# (perm_code, perm_name, resource, api_method, api_path)
PERMISSIONS: tuple[tuple[str, str, str, str | None, str | None], ...] = (
    ("learning:self:read", "查看自己的学习数据", "learning", "GET", "/api/v1/learning/*"),
    (
        "learning:explanation:write",
        "写入讲解证据",
        "learning",
        "POST",
        "/api/v1/learning/explanations",
    ),
    ("learning:report:read", "查看学习报告", "learning", "GET", "/api/v1/learning/report"),
    (
        "learning:atom:admin",
        "维护概念图与知识原子",
        "learning",
        "POST",
        "/api/v1/learning/concepts",
    ),
)

# 角色 → 权限: member 自助读写自己的学习数据, operator 只读报告(admin 走通配 *)
GRANTS: dict[str, tuple[str, ...]] = {
    "member": ("learning:self:read", "learning:explanation:write"),
    "operator": ("learning:report:read",),
}

_PERM_CODES = tuple(code for code, *_ in PERMISSIONS)

#: 所有学习表共用的 MySQL 表选项(与 migrations/005_learning.sql 一致)。
_MYSQL_KWARGS: dict[str, str] = {
    "mysql_engine": "InnoDB",
    "mysql_charset": "utf8mb4",
    "mysql_collate": "utf8mb4_0900_ai_ci",
}


def _quoted(values: Sequence[str]) -> str:
    return ", ".join("'" + value.replace("'", "''") + "'" for value in values)


def _bigint_id_type() -> sa.types.TypeEngine:
    """新表主键/外键列的类型: 与既有 ``account.id`` 逐位一致。

    拿不到可自省的真实连接时(离线 ``alembic --sql``、单测桩)退回模型声明的有符号
    ``BIGINT`` —— 那种场景下没有 MySQL 外键检查, 类型只要与模型一致即可。
    """
    try:
        columns = sa.inspect(op.get_bind()).get_columns("account")
    except Exception:
        return sa.BigInteger()
    for column in columns:
        if column["name"] == "id":
            return column["type"]
    return sa.BigInteger()


def upgrade() -> None:
    """Upgrade schema."""
    id_type = _bigint_id_type()

    def _id() -> sa.types.TypeEngine:
        return id_type.copy()

    op.create_table(
        'learning_concept',
        sa.Column('id', _id(), autoincrement=True, nullable=False),
        sa.Column(
            'slug',
            sa.String(length=128),
            nullable=False,
            comment='概念标识, 如 attention-scaled-dot-product',
        ),
        sa.Column('title', sa.String(length=255), nullable=False),
        sa.Column(
            'subject',
            sa.String(length=64),
            nullable=True,
            comment='学科标签, 仅用于筛选, 不参与内核逻辑',
        ),
        sa.Column(
            'scope',
            sa.Text(),
            nullable=True,
            comment='范围边界: 明确不讨论什么(no_boundary 卡点的判据)',
        ),
        sa.Column('summary', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('slug', name='uk_concept_slug'),
        comment='概念节点',
        **_MYSQL_KWARGS,
    )
    op.create_table(
        'learning_atom',
        sa.Column('id', _id(), autoincrement=True, nullable=False),
        sa.Column(
            'atom_key',
            sa.String(length=191),
            nullable=False,
            comment='稳定标识 atom:xxx(即内核 Atom.id)',
        ),
        sa.Column('concept_id', _id(), nullable=False, comment='所属概念'),
        sa.Column('statement', sa.Text(), nullable=False, comment='不可再分的最小命题'),
        sa.Column(
            'kind',
            sa.String(length=16),
            nullable=False,
            comment='definition|axiom|observable|derivable(前三者是 Why-chain 可停点)',
        ),
        sa.Column('source_title', sa.String(length=255), nullable=True),
        sa.Column(
            'source_locator', sa.String(length=255), nullable=True, comment='章节/页码/时间戳'
        ),
        sa.Column('source_url', sa.String(length=1024), nullable=True),
        sa.Column(
            'verified',
            sa.Boolean(),
            server_default=sa.text('0'),
            nullable=False,
            comment='未溯源不得进入掌握度记账',
        ),
        sa.Column(
            'difficulty',
            sa.SmallInteger(),
            server_default=sa.text('1'),
            nullable=False,
            comment='1-5, 出题难度校准用(默认值与 schemas.Atom 一致)',
        ),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['concept_id'], ['learning_concept.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('atom_key', name='uk_atom_key'),
        comment='知识原子',
        **_MYSQL_KWARGS,
    )
    op.create_index('idx_atom_concept', 'learning_atom', ['concept_id'], unique=False)
    op.create_table(
        'learning_atom_edge',
        sa.Column('id', _id(), autoincrement=True, nullable=False),
        sa.Column('from_atom_id', _id(), nullable=False),
        sa.Column('to_atom_id', _id(), nullable=False),
        sa.Column(
            'relation',
            sa.String(length=16),
            nullable=False,
            comment='depends_on|entails|contradicts',
        ),
        sa.Column(
            'weight',
            sa.Numeric(precision=4, scale=3),
            server_default=sa.text('1.000'),
            nullable=False,
            comment='关键边权重更高',
        ),
        sa.ForeignKeyConstraint(['from_atom_id'], ['learning_atom.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['to_atom_id'], ['learning_atom.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('from_atom_id', 'to_atom_id', 'relation', name='uk_edge'),
        comment='原子依赖边(第一性原理的图化身)',
        **_MYSQL_KWARGS,
    )
    op.create_index('idx_edge_from', 'learning_atom_edge', ['from_atom_id'], unique=False)
    op.create_index('idx_edge_to', 'learning_atom_edge', ['to_atom_id'], unique=False)
    op.create_table(
        'learner_explanation',
        sa.Column('id', _id(), autoincrement=True, nullable=False),
        sa.Column('account_id', _id(), nullable=False, comment='学习者账号'),
        sa.Column(
            'thread_id',
            sa.String(length=64),
            nullable=True,
            comment='关联 LangGraph thread(L1 记忆)',
        ),
        sa.Column('concept_id', _id(), nullable=False),
        sa.Column(
            'loop_index',
            sa.SmallInteger(),
            server_default=sa.text('1'),
            nullable=False,
            comment='第几轮 Teach(1 起, 上限 PedagogyConfig.max_loops)',
        ),
        sa.Column(
            'state',
            sa.String(length=16),
            nullable=False,
            comment='Pick|Teach|Detect|Return|Simplify|Closed|Escaped',
        ),
        sa.Column(
            'raw_text',
            sa.Text().with_variant(mysql.MEDIUMTEXT(), 'mysql'),
            nullable=False,
            comment='学习者原话, 不加工',
        ),
        sa.Column('claims_json', sa.JSON(), nullable=False, comment='命题拆解 + status'),
        sa.Column(
            'gap_report_json', sa.JSON(), nullable=True, comment='四类卡点 + probe + supply'
        ),
        sa.Column(
            'learner_talk_ratio',
            sa.Numeric(precision=4, scale=3),
            nullable=True,
            comment='learner_chars/(learner+system), 护栏口径',
        ),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['account_id'], ['account.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['concept_id'], ['learning_concept.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        comment='讲解证据(费曼循环的原始产物)',
        **_MYSQL_KWARGS,
    )
    op.create_index(
        'idx_expl_account',
        'learner_explanation',
        ['account_id', 'concept_id', 'created_at'],
        unique=False,
    )
    op.create_table(
        'mastery_ledger',
        sa.Column('id', _id(), autoincrement=True, nullable=False),
        sa.Column('account_id', _id(), nullable=False),
        sa.Column('atom_id', _id(), nullable=False),
        sa.Column(
            'mastery',
            sa.Numeric(precision=5, scale=4),
            server_default=sa.text('0.0000'),
            nullable=False,
            comment='掌握度 m ∈ [0,1]',
        ),
        sa.Column(
            'evidence_count',
            sa.Integer(),
            server_default=sa.text('0'),
            nullable=False,
            comment='有效证据条数(读直答/放弃不计数)',
        ),
        sa.Column('last_evidence_at', sa.DateTime(), nullable=True),
        sa.Column(
            'next_review_at',
            sa.DateTime(),
            nullable=True,
            comment='间隔重复调度(1/3/7/21/60 天阶梯)',
        ),
        sa.Column(
            'review_stage',
            sa.SmallInteger(),
            server_default=sa.text('0'),
            nullable=False,
            comment='复习阶梯下标(REVIEW_LADDER_DAYS 的索引)',
        ),
        sa.Column(
            'tau_days',
            sa.Numeric(precision=6, scale=3),
            server_default=sa.text('7.000'),
            nullable=False,
            comment='遗忘时间常数 τ(天): 冷启动取配置, 之后按该 Atom 历史表现拟合',
        ),
        sa.Column(
            'depth',
            sa.SmallInteger(),
            server_default=sa.text('0'),
            nullable=False,
            comment='最近一次 Why-chain 下探深度(证据权重修正用)',
        ),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['account_id'], ['account.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['atom_id'], ['learning_atom.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('account_id', 'atom_id', name='uk_mastery'),
        comment='掌握度账本: 每条边一行, 可回溯证据',
        **_MYSQL_KWARGS,
    )
    op.create_index(
        'idx_mastery_review', 'mastery_ledger', ['account_id', 'next_review_at'], unique=False
    )

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

    for table in reversed(TABLES):
        op.drop_table(table)
