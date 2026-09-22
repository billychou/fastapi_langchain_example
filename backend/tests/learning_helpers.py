"""学习证据层(P0 #7)的共用夹具与构造器(非测试模块, pytest 不收集)。

被 ``test_learning_service.py`` / ``test_learning_api.py`` 复用。抽出来的只有两件事:

- **内存 SQLite 建真表**: 这一层的行为大半是「唯一约束 + 外键 + DECIMAL 精度」,
  mock session 测不到; 与 ``conftest.py`` 冻结的死地址配合, 全程不碰 MySQL/Redis。
- **确定性的时间与载荷**: 掌握度是 ``m + α·w·(1−m)`` 与 ``m·exp(−Δt/τ)`` 的函数,
  service 的每个时间相关入口都收 ``now=``, 于是断言能写成精确数字而不是「大于 0」。

: data:`T0` / :data:`DAY` 与 ``PedagogyConfig`` 的默认值(α=0.35、τ=7 天、深度修正
下限 0.7)共同决定下面所有期望值 —— 改配置就得改这里的常数, 这是**有意的**:
让「阈值配置化」在测试里看得见, 而不是悄悄漂移。
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.db.session import Base
from app.pedagogy.mastery import DEPTH_FACTOR_FLOOR, EVIDENCE_WEIGHTS, REVIEW_LADDER_DAYS
from app.pedagogy.policy import PedagogyConfig
from app.pedagogy.turn import concept_slug
from app.schemas.learning import AtomIn, AtomSourceIn, ConceptUpsertRequest, ExplanationCreate
from sqlalchemy import BigInteger, event, insert
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.ext.compiler import compiles


# SQLite 只把 ``INTEGER PRIMARY KEY`` 当 rowid 别名, ``BIGINT`` 主键不会自增
# (与 test_account_api / test_learning_models 同一手法; 重复注册是幂等的)。
@compiles(BigInteger, "sqlite")
def _sqlite_bigint_as_integer(type_, compiler, **kw):  # noqa: ANN001, ARG001
    return "INTEGER"


# --------------------------------------------------------------------------- 时间


#: 所有记账的基准时刻(UTC, 与库里 ``DateTime`` 的时区口径一致)。
T0 = datetime(2026, 9, 14, 3, 0, 0, tzinfo=UTC)
DAY = timedelta(days=1)

#: 内核默认阈值(从 ``PedagogyConfig`` 取, 不在测试里重抄一遍数字)。
_CFG = PedagogyConfig()
ALPHA = _CFG.mastery_alpha
TAU_DAYS = _CFG.mastery_coldstart_tau_days
MAX_DEPTH = _CFG.why_chain_max_depth

#: ``depth=0`` 时一次 ``closed_explanation`` 的实际权重 w = 1.0 × 深度修正下限。
W_DEPTH0 = EVIDENCE_WEIGHTS["closed_explanation"] * DEPTH_FACTOR_FLOOR
#: 深度打满时不修正(问到底的人和第二层就停的人不等值, 见 pedagogy-core §4.2)。
W_FULL_DEPTH = EVIDENCE_WEIGHTS["closed_explanation"]
#: 冷启动后第一条证据带来的掌握度: ``0 + α·w·(1−0)``。
M_FIRST = ALPHA * W_DEPTH0
#: 复习阶梯的第一级间隔(天) —— 记一次账, 明天就该复习。
FIRST_INTERVAL = REVIEW_LADDER_DAYS[0]


def decayed(mastery: float, days: float, tau: float = TAU_DAYS) -> float:
    """期望值算子: 与 :func:`app.pedagogy.mastery.decay` 同一个公式。

    刻意在测试里重算而不是调内核函数 —— 调内核函数会让「公式被改错」这件事
    在测试里同样自洽(自己证明自己), 而重算至少要求两边同时错才可能全绿。
    """
    import math

    return mastery * math.exp(-days / tau)


# --------------------------------------------------------------------------- 数据库


@dataclass(slots=True)
class LearningDb:
    """一套自包含的内存库: engine + session 工厂 + 一个已打开的 session。"""

    engine: AsyncEngine
    factory: async_sessionmaker[AsyncSession]
    session: AsyncSession

    async def close(self) -> None:
        await self.session.close()
        await self.engine.dispose()


async def build_db(*, accounts: Sequence[int] = (1,)) -> LearningDb:
    """建全表 + 插入账号行(外键真的会被校验, 所以开 ``PRAGMA foreign_keys``)。

    ``Account.profile`` 是 ``lazy="selectin"``, 少了 ``user_profile`` 表会让任何
    账号加载都炸在 "no such table" —— 因此建**全部**表, 不挑子集。
    """
    from app.models import Account  # 局部导入: 让 conftest 的环境变量先生效

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")

    @event.listens_for(engine.sync_engine, "connect")
    def _enable_sqlite_fks(dbapi_connection, _record):  # noqa: ANN001
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        if accounts:
            await conn.execute(
                insert(Account).values(
                    [
                        {"id": account_id, "login_policy": "multi_device"}
                        for account_id in accounts
                    ]
                )
            )
    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    return LearningDb(engine=engine, factory=factory, session=factory())


# --------------------------------------------------------------------------- 知识图

#: 「闭包」这个概念的三个 Atom: 一个定义(可停点) + 两个可推导命题。
CONCEPT_TITLE = "闭包"
CONCEPT_SLUG = concept_slug(CONCEPT_TITLE)
ATOM_DEF = "atom:closure:definition"
ATOM_WHY = "atom:closure:why-lexical-env"
ATOM_SCOPE = "atom:closure:scope-rule"

#: 第二个概念(用来验证「按概念聚合」不会把两个概念混在一起)。
OTHER_TITLE = "缩放点积注意力"
OTHER_SLUG = concept_slug(OTHER_TITLE)
OTHER_ATOM = "atom:attention:score-formula"


def atom(
    key: str,
    statement: str,
    *,
    kind: str = "derivable",
    verified: bool = True,
    difficulty: int = 1,
    depends_on: Iterable[str] = (),
    entails: Iterable[str] = (),
    source: tuple[str, str, str] | None = None,
) -> AtomIn:
    """一个 Atom 的写入载荷(``source`` 给三元组即视为已溯源)。"""
    return AtomIn(
        atom_key=key,
        statement=statement,
        kind=kind,  # type: ignore[arg-type]
        verified=verified,
        difficulty=difficulty,
        depends_on=tuple(depends_on),
        entails=tuple(entails),
        source=(
            AtomSourceIn(title=source[0], locator=source[1], url=source[2]) if source else None
        ),
    )


def closure_atoms(*, why_verified: bool = True) -> tuple[AtomIn, ...]:
    """闭包概念的标准子图: ``why`` 与 ``scope`` 都依赖 ``definition``。"""
    return (
        atom(
            ATOM_DEF,
            "闭包是函数与其词法作用域的组合, 函数离开定义处仍能访问那些变量",
            kind="definition",
            source=("SICP", "§1.4.3", "https://example.org/sicp#1.4.3"),
        ),
        atom(
            ATOM_WHY,
            "之所以还能访问, 是因为函数对象持有了定义时的环境引用, 而不是当时的值拷贝",
            depends_on=(ATOM_DEF,),
            verified=why_verified,
        ),
        atom(
            ATOM_SCOPE,
            "作用域规则决定了哪些名字对函数可见; 闭包把这套规则延长到调用之后",
            depends_on=(ATOM_DEF,),
            entails=(ATOM_WHY,),
        ),
    )


def concept_payload(**overrides: object) -> ConceptUpsertRequest:
    """``POST /learning/concepts`` 的请求体(默认: 闭包 + 三个 Atom)。

    刻意**不给** ``slug``: 让 service 走 ``concept_slug(title)`` 生成分支,
    这条分支一旦与聊天里「我想搞懂闭包」的 slug 分叉, 账本就裂成两半。
    """
    payload: dict[str, object] = {
        "title": CONCEPT_TITLE,
        "subject": "编程语言",
        "scope": "只讨论词法作用域下的闭包, 不涉及 this 绑定与内存回收",
        "atoms": closure_atoms(),
    }
    payload.update(overrides)
    return ConceptUpsertRequest.model_validate(payload)


def other_concept_payload() -> ConceptUpsertRequest:
    return ConceptUpsertRequest.model_validate(
        {
            "title": OTHER_TITLE,
            "subject": "深度学习",
            "atoms": (
                atom(
                    OTHER_ATOM,
                    "注意力分数是 Q 与 K 的点积除以 sqrt(d_k)",
                    kind="definition",
                    source=("Attention Is All You Need", "§3.2.1", ""),
                ),
            ),
        }
    )


def explanation_payload(**overrides: object) -> ExplanationCreate:
    """一份自述的请求体(默认不带 ``evidence``, 只存原话)。"""
    payload: dict[str, object] = {
        "concept_slug": CONCEPT_SLUG,
        "raw_text": (
            "闭包就是函数把定义它的时候周围那些变量一起带走了, 所以哪怕外层函数已经返回, "
            "内层函数还能读到它们; 我理解是函数对象里存了一份对那个环境的引用。"
        ),
        "thread_id": "thread-learning-1",
        "loop_index": 2,
        "state": "Simplify",
        "claims": (
            {
                "id": "c1",
                "text": "函数带走了定义时的变量",
                "status": "supported",
                "atom_ref": ATOM_DEF,
            },
            {
                "id": "c2",
                "text": "因为存的是环境引用而不是值拷贝",
                "status": "supported",
                "atom_ref": ATOM_WHY,
            },
        ),
        "gap_report": {
            "explanation_id": "exp-1",
            "verdict": "closed",
            "gaps": (),
            "scores": {
                "completeness": 0.9,
                "own_words": 0.85,
                "boundary_awareness": 0.7,
                "reason": "覆盖定义与成因, 且用自己的话重述",
            },
            "learner_talk_ratio": 0.72,
        },
        "learner_talk_ratio": 0.72,
    }
    payload.update(overrides)
    return ExplanationCreate.model_validate(payload)


def evidence(
    *atom_keys: str, kind: str = "closed_explanation", depth: int = 0
) -> dict[str, object]:
    """``ExplanationCreate.evidence`` 的载荷。"""
    return {"kind": kind, "atom_keys": list(atom_keys), "depth": depth}


def ledger_row(*, account_id: int, atom_id: int, mastery: str = "0.2450") -> object:
    """直接造一行账本(测「衰减」「复习到期」时不想先跑一遍记账)。"""
    from app.models.learning import MasteryLedger

    return MasteryLedger(
        account_id=account_id,
        atom_id=atom_id,
        mastery=Decimal(mastery),
        evidence_count=1,
        last_evidence_at=T0,
        next_review_at=T0 + timedelta(days=FIRST_INTERVAL),
        review_stage=0,
        tau_days=Decimal(str(TAU_DAYS)),
        depth=0,
        updated_at=T0,
    )


__all__ = [
    "ALPHA",
    "ATOM_DEF",
    "ATOM_SCOPE",
    "ATOM_WHY",
    "CONCEPT_SLUG",
    "CONCEPT_TITLE",
    "DAY",
    "FIRST_INTERVAL",
    "M_FIRST",
    "MAX_DEPTH",
    "OTHER_ATOM",
    "OTHER_SLUG",
    "OTHER_TITLE",
    "T0",
    "TAU_DAYS",
    "W_DEPTH0",
    "W_FULL_DEPTH",
    "LearningDb",
    "atom",
    "build_db",
    "closure_atoms",
    "concept_payload",
    "decayed",
    "evidence",
    "explanation_payload",
    "ledger_row",
    "other_concept_payload",
]
