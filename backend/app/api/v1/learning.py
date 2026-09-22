"""学习证据 API(P0 #7): 概念图 / 讲解证据 / 掌握度账本 / 复习队列。

这一层只做四件事: **鉴权、取 account_id、调 service、套信封**。业务换算(Decimal↔float、
slug↔主键、ORM 行↔内核 ``MasteryEntry``)全部在 ``app/services/learning_service.py``,
掌握度公式全部在 ``app/pedagogy/mastery.py`` —— 路由里出现 ``m + α·w·(1−m)`` 就是分层破了。

四条口径(改之前先读):

1. **权限点逐字对应 ``migrations/005_learning.sql`` 的种子**: 读自己 ``learning:self:read``、
   写证据 ``learning:explanation:write``、改知识图 ``learning:atom:admin``。
   常量定义在 ``app/schemas/learning.py``, 由 ``tests/test_migrations.py`` 守卫那张种子表,
   所以「接口上线即 403」不会发生, 但也别在路由里写字符串字面量。
2. **``account_id`` 只来自 Token, 绝不来自请求体或 query。** 于是「看别人的掌握度」在这层
   根本无法表达 —— 家长/教师视图是 ``GET /learning/report?account_id=``(门禁
   ``learning:report:read``, P1 #10), 它会把「谁在看谁」写进审计, 而不是把越权能力
   悄悄塞进每个自服务接口。
3. **不存在与不属于你不可区分**: 读他人自述、读不存在的概念一律 404(与 skills /
   ``thread_service.get_thread`` 同口径), 避免自增 id 被枚举出「这个平台有多少条证据」。
4. **改知识图要留痕**: ``POST /concepts`` 会 upsert 甚至整图替换 Atom, 而 Atom 上挂着
   学习者的证据账本(``replace_atoms=true`` 会连带删账本行)。这类「教研动作影响他人
   数据」的写操作一律进 ``audit_log`` —— 掌握度账本可回溯, 前提是图本身的变更也可回溯。

对外引用一律用稳定字符串键(概念 ``slug``、原子 ``atom_key``), 不暴露自增主键;
``ConceptOut.id`` 只是排查用的附带信息, 不要拿它拼 URL。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Path, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.net import resolve_client_ip
from app.db.session import get_db
from app.deps import AuthContext, require_permissions
from app.exceptions import BizCode, BizError
from app.schemas.common import ok
from app.schemas.learning import (
    PERM_ATOM_ADMIN,
    PERM_EXPLANATION_WRITE,
    PERM_SELF_READ,
    SLUG_PATTERN,
    ConceptUpsertRequest,
    ExplanationCreate,
)
from app.services import audit_service, learning_service

logger = logging.getLogger("learning")

router = APIRouter(prefix="/learning", tags=["learning"])

#: 概念 slug 走路径参数: 模式与请求体里的 ``slug`` 同源, 非法 slug 在路由层就 422,
#: 不会退化成一次「查不到就 404」的空查询。
SlugPath = Path(max_length=128, pattern=SLUG_PATTERN, description="概念 slug")
#: ``audit_log.target_id`` 是 ``VARCHAR(64)``, 而 slug 最宽 128 —— 存审计时截断,
#: 完整 slug 在 ``detail`` 里(审计是给排障看的, 不能因为字段宽度丢信息)。
AUDIT_TARGET_MAX = 64


@router.post("/concepts", summary="建立/绑定概念并写入前置 Atom 子图(需 learning:atom:admin)")
async def upsert_concept(
    body: ConceptUpsertRequest,
    request: Request,
    ctx: AuthContext = Depends(require_permissions(PERM_ATOM_ADMIN)),
    db: AsyncSession = Depends(get_db),
):
    """幂等地绑定一个概念, 并 upsert 它的知识原子与依赖边。

    为什么是 ``POST`` 而不是 ``PUT``: 调用方(教研脚本 / 材料摄取)通常只关心「这个概念
    和这些 Atom 现在是对的」, 不关心它此前存不存在; 响应用 ``created`` /
    ``atom_keys_created`` / ``atom_keys_updated`` 把「新建」与「重放」区分开。

    ``replace_atoms=true`` 是破坏性的(未出现的旧 Atom 连同其账本行一并删除), 因此
    被门禁在 ``learning:atom:admin``, 并且在日志与审计里都点名。
    """
    result = await learning_service.bind_concept(db, body)
    if result.atom_keys_removed:
        # 删 Atom = 删证据, 属于不可逆的教研动作: 日志留一条 warning, 审计留明细。
        logger.warning(
            "concept %s 整图替换删除 %d 个 Atom: %s",
            result.concept.slug,
            len(result.atom_keys_removed),
            list(result.atom_keys_removed),
        )
    await audit_service.record_audit(
        db,
        account_id=ctx.account_id,
        action="learning_concept_upsert",
        target_type="learning_concept",
        target_id=result.concept.slug[:AUDIT_TARGET_MAX],
        ip=resolve_client_ip(request),
        user_agent=request.headers.get("User-Agent"),
        detail={
            "slug": result.concept.slug,
            "created": result.created,
            "replace_atoms": body.replace_atoms,
            "atoms_created": list(result.atom_keys_created),
            "atoms_updated": list(result.atom_keys_updated),
            "atoms_removed": list(result.atom_keys_removed),
        },
    )
    return ok(result.model_dump(mode="json"))


@router.get("/concepts/{slug}/graph", summary="概念子图(Atom + 依赖边 + 我的掌握度)")
async def get_concept_graph(
    slug: str = SlugPath,
    ctx: AuthContext = Depends(require_permissions(PERM_SELF_READ)),
    db: AsyncSession = Depends(get_db),
):
    """前端概念图页与教练上下文**同源**的一份数据: 图的结构 + 我的掌握度快照。

    只返回本人的账目(``account_id`` 来自 Token): 同一个概念在两个学习者眼里的图结构
    相同、掌握度不同, 这正是「证据优先」的对外表达。跨账号视图见 ``/learning/report``(P1)。
    """
    graph = await learning_service.concept_graph(db, slug, ctx.account_id)
    if graph is None:
        raise BizError(BizCode.NOT_FOUND, "概念不存在")
    return ok(graph.model_dump(mode="json"))


@router.post("/explanations", summary="提交一份自述(可随带一条掌握度证据)")
async def create_explanation(
    body: ExplanationCreate,
    ctx: AuthContext = Depends(require_permissions(PERM_EXPLANATION_WRITE)),
    db: AsyncSession = Depends(get_db),
):
    """写入学习者原话 + 拆解结论, 并在给出 ``evidence`` 时记一条掌握度账。

    原话(``raw_text``)与结论(``claims`` / ``gap_report``)分列存储: 判分器换 rubric 时
    拿原话重跑, 历史证据不会被新结论污染。响应里的 ``mastery`` 与 SSE ``learn.mastery``
    事件同形(前端一套渲染逻辑), ``credited=false`` 时**原样返回原因**而不是省略 ——
    「我明明闭合了, 掌握度没动」是最需要解释的一种情况(读直答/放弃/Atom 未溯源)。

    ``evidence.atom_keys`` 里不属于该概念的键不会静默丢弃, 而是列进 ``skipped_atom_keys``。
    """
    result = await learning_service.create_explanation(db, ctx.account_id, body)
    return ok(result.model_dump(mode="json"))


@router.get("/explanations", summary="我的自述列表(倒序, 不含全文)")
async def list_explanations(
    concept_slug: str | None = Query(
        default=None, max_length=128, pattern=SLUG_PATTERN, description="只列该概念下的自述"
    ),
    limit: int = Query(
        default=learning_service.EXPLANATION_LIST_LIMIT,
        ge=1,
        le=learning_service.EXPLANATION_LIST_MAX,
        description="返回条数上限(v1 不分页, 列表页只要索引)",
    ),
    ctx: AuthContext = Depends(require_permissions(PERM_SELF_READ)),
    db: AsyncSession = Depends(get_db),
):
    """列表项只带索引信息(字数 / 卡点类型 / 判分结论), 全文走 ``/{id}``。

    刻意不在列表里塞 ``raw_text``: 一个认真用了三个月的学习者会有几百条自述,
    列表页拉全文等于让「回顾自己的学习轨迹」这个高频动作变成慢查询。
    """
    result = await learning_service.list_explanations(
        db, ctx.account_id, concept_slug=concept_slug, limit=limit
    )
    return ok(result.model_dump(mode="json"))


@router.get("/explanations/{explanation_id}", summary="自述详情(含原话全文)")
async def get_explanation(
    explanation_id: int = Path(gt=0),
    ctx: AuthContext = Depends(require_permissions(PERM_SELF_READ)),
    db: AsyncSession = Depends(get_db),
):
    """他人的自述一律 404(不泄露「这条证据是否存在」), 与 skills / threads 同口径。"""
    item = await learning_service.get_explanation(db, ctx.account_id, explanation_id)
    if item is None:
        raise BizError(BizCode.NOT_FOUND, "自述不存在")
    return ok(item.model_dump(mode="json"))


@router.get("/mastery", summary="掌握度账本(逐 Atom 可回溯 + 按概念聚合)")
async def get_mastery(
    ctx: AuthContext = Depends(require_permissions(PERM_SELF_READ)),
    db: AsyncSession = Depends(get_db),
):
    """每一行都能追到证据: ``evidence_count`` + ``last_evidence_at`` + 复习排期。

    同时返回 ``mastery``(上次记账时的原值)与 ``decayed_mastery``(按当下衰减后的值):
    衰减公式与 τ 都在服务端, 前端不该自己算; 但两个数都要给, 否则「为什么掉了」无法解释。
    ``concepts`` 是按概念聚合的加权掌握度, 未记账的 Atom 以 m=0 参与聚合 ——
    只闭合了 5 个 Atom 中的 1 个就报「概念掌握 100%」, 对家长是误导。
    """
    return ok((await learning_service.mastery_list(db, ctx.account_id)).model_dump(mode="json"))


@router.get("/review-queue", summary="今日复习队列(90 秒重讲的入口)")
async def get_review_queue(
    limit: int | None = Query(
        default=learning_service.REVIEW_QUEUE_LIMIT,
        ge=1,
        le=learning_service.REVIEW_QUEUE_MAX,
        description="最多返回多少个到期 Atom(过期越久排越前)",
    ),
    ctx: AuthContext = Depends(require_permissions(PERM_SELF_READ)),
    db: AsyncSession = Depends(get_db),
):
    """只包含**记过账**且已到期的 Atom: 从没记过账的新 Atom 没有复习义务,
    它该走正常的费曼循环而不是被塞进队列(``mastery.scheduled_review`` 返回 ``None``)。

    到期裁决完全交给纯函数 :func:`app.pedagogy.mastery.review_queue`(有独立单测),
    路由与 service 只负责翻译。
    """
    result = await learning_service.review_queue(db, ctx.account_id, limit=limit)
    return ok(result.model_dump(mode="json"))


__all__ = ["AUDIT_TARGET_MAX", "router"]
