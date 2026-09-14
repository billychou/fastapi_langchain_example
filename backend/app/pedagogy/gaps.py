"""卡点检测器: 学习者自述 -> claims -> 四类断裂 -> GapReport。

对应 ``docs/pedagogy-core.md`` §2、§3。这是内核里**唯一会调 LLM 的一层**, 因此
判分器被抽象成可注入的 :class:`Grader` 协议 —— 测试注入固定输出的假判分器,
即可在零网络、零密钥下穷举四类卡点(roadmap P0 交付物 #2)。

职责边界(很重要):

- **LLM 只做主观拆解**: 把原话切成 claims、标出术语/类比/反例、给 ``own_words`` 与
  ``boundary_awareness`` 两个主观维度打分;
- **内核做客观裁决**: ``completeness`` 由 claim 的 Atom 覆盖率算出, 四类卡点由
  :func:`derive_gaps` 的**规则**判定, ``verdict`` 由是否存在卡点决定。

这样即使模型跑偏, 诊断结论仍然是可解释、可回归的; 也是判分一致率(Cohen's κ)
能被优化的前提 —— 规则改动不需要重新训练提示词的手感。
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Sequence
from typing import Any, Protocol

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.pedagogy.policy import PedagogyConfig
from app.pedagogy.schemas import (
    Atom,
    AtomBundle,
    Claim,
    ClaimStatus,
    ExplanationTurn,
    GapItem,
    GapReport,
    GapScores,
    GapType,
    MissingEdge,
)
from app.pedagogy.states import FeynmanState

logger = logging.getLogger("pedagogy.gaps")

#: 判分温度: rubric 要可复现, 不能每次都不一样。
GRADER_TEMPERATURE = 0.0

#: 因果连接词: ``causal_gap`` 的检测信号之一(pedagogy-core §3「然后就 / 所以」)。
CAUSAL_CONNECTIVES: tuple[str, ...] = (
    "然后就",
    "然后",
    "所以",
    "因此",
    "于是",
    "从而",
    "接着",
    "这样就",
)

#: 每类卡点的默认追问 —— 与 pedagogy-core §3 表格的「教学动作」列一一对应。
#: 判分器没给出 ``probe`` 时用它兜底, 保证「诊断必须可执行」这条不变量永远成立。
DEFAULT_PROBES: dict[GapType, str] = {
    "jargon_shield": "不许用「{term}」这个词，把这句话重说一遍。",
    "causal_gap": "这一步为什么可以这样？中间缺了哪个理由？",
    "broken_analogy": "这个类比在什么地方会失效？",
    "no_boundary": "这个说法什么时候不成立？给我一个反例。",
}

#: 判分器把某条命题标成 ``jargon_shield`` 却没交出具体术语时的兜底追问。
GENERIC_JARGON_PROBE = "不许用术语，把这句话用大白话重说一遍。"


class GradingError(RuntimeError):
    """判分器输出无法解析成合法证据。

    **刻意向上抛而不是静默降级**: 拿不到可信判分时宁可让状态机停在原地
    (不转移、不记掌握度), 也不能凭半个报告给学习者加分。
    """


class GradeResult(BaseModel):
    """判分器的原始结构化输出(主观部分)。"""

    model_config = ConfigDict(extra="ignore")

    #: 刻意声明为 ``list[Any]``: 模型偶尔会混进字符串/空项, 这些由 :func:`_to_claims`
    #: 逐条丢弃, 而不是在 schema 层让整份判分直接报废。
    claims: list[Any] = Field(default_factory=list)
    jargon_terms: list[str] = Field(default_factory=list)
    analogy: str | None = None
    counterexample: str | None = None
    own_words: float = Field(default=0.0, ge=0.0, le=1.0)
    boundary_awareness: float = Field(default=0.0, ge=0.0, le=1.0)
    reason: str = ""


class Grader(Protocol):
    """判分接口: 给定系统/用户提示, 返回一段 JSON 文本。

    实现方负责温度与结构化输出; 内核只负责解析与裁决。测试里注入一个返回固定
    JSON 的假实现即可(见 ``tests/test_pedagogy_gaps.py``)。
    """

    async def grade(self, *, system: str, user: str) -> str: ...


class LlmGrader:
    """默认判分器: 用一个温度 0 的 ``BaseChatModel`` 出 JSON。"""

    def __init__(self, model: BaseChatModel, temperature: float = GRADER_TEMPERATURE) -> None:
        self.temperature = temperature
        # bind() 是 BaseChatModel 的通用能力; mock 模型会忽略这个参数
        self.model = model.bind(temperature=temperature)

    async def grade(self, *, system: str, user: str) -> str:
        message = await self.model.ainvoke(
            [SystemMessage(content=system), HumanMessage(content=user)]
        )
        return _text_of(getattr(message, "content", ""))


class Detection(BaseModel):
    """一次检测的完整产物: 证据(ExplanationTurn) + 诊断(GapReport)。"""

    model_config = ConfigDict(frozen=True)

    turn: ExplanationTurn
    report: GapReport


# --------------------------------------------------------------------------- 提示词


def build_prompt(
    *,
    explanation: str,
    concept_id: str,
    atoms: Sequence[Atom] = (),
    require_boundary: bool = False,
) -> tuple[str, str]:
    """构造 ``(system, user)`` 提示。纯函数, 可直接被测试断言。"""
    atom_lines = "\n".join(
        f"- {atom.id} [{atom.kind}] {atom.statement}" for atom in atoms
    ) or "(暂无已验证 Atom)"
    system = (
        "你是学习诊断器。你的任务不是评价学习者聪不聪明, 而是把 TA 的原话拆成可判真伪的命题,"
        "并指出**卡在哪里**。\n"
        "严格输出一个 JSON 对象, 不要输出任何解释性文字或代码块围栏, 字段如下:\n"
        '{"claims":[{"id":"c1","text":"…","atom_ref":"atom:…"或null,'
        '"status":"supported|unsupported|wrong|jargon_shield|vague","note":"…"}],'
        '"jargon_terms":["…"],"analogy":"…"或null,"counterexample":"…"或null,'
        '"own_words":0.0-1.0,"boundary_awareness":0.0-1.0,"reason":"…"}\n'
        "判定口径:\n"
        "- supported: 命题与某个已验证 Atom 一致;\n"
        "- unsupported: 命题缺理由, 说不出「为什么可以这样」;\n"
        "- wrong: 命题与已验证 Atom 冲突;\n"
        "- jargon_shield: 用术语解释术语, 该术语本身没被展开;\n"
        "- vague: 无法判真伪(如「差不多就是那种感觉」)。\n"
        "own_words 衡量「是不是自己的话」: 术语密度与句式若与材料原文高度重合则给低分。\n"
        "reason 必填, 说明你为什么这样判。"
    )
    user = (
        f"概念: {concept_id}\n"
        f"是否需要反例/边界: {'是' if require_boundary else '否'}\n\n"
        f"已验证 Atom 清单:\n{atom_lines}\n\n"
        f"学习者原话:\n\"\"\"\n{explanation}\n\"\"\"\n\n"
        "请按口径拆解并输出 JSON。"
    )
    return system, user


# --------------------------------------------------------------------------- 规则判定


def has_causal_connective(text: str) -> bool:
    """原话里是否出现「然后就 / 所以」这类跳过理由的连接词。"""
    return any(word in text for word in CAUSAL_CONNECTIVES)


def completeness_of(claims: Sequence[Claim], required_atoms: Sequence[str] = ()) -> float:
    """必要 Atom 覆盖率: 客观维度, 由内核算, 不采信模型自报。

    给了 ``required_atoms`` 就按「覆盖了几条必要 Atom」算; 没给就退化为
    「supported claim 占比」, 至少保证不会因为模型自评虚高而放行。
    """
    if required_atoms:
        covered = {claim.atom_ref for claim in claims if claim.status == "supported"}
        hit = sum(1 for atom_id in required_atoms if atom_id in covered)
        return hit / len(required_atoms)
    if not claims:
        return 0.0
    supported = sum(1 for claim in claims if claim.status == "supported")
    return supported / len(claims)


def derive_gaps(
    *,
    claims: Sequence[Claim],
    raw_text: str,
    jargon_terms: Sequence[str] = (),
    analogy: str | None = None,
    counterexample: str | None = None,
    atoms: Sequence[Atom] = (),
    require_boundary: bool = False,
    analogy_flagged: bool = False,
    config: PedagogyConfig | None = None,
) -> tuple[GapItem, ...]:
    """按 pedagogy-core §3 的检测信号, 从证据里**规则化**地推出四类卡点。

    每条 gap 都自带 ``probe``(下一步问什么)与 ``supply``(该补哪个 Atom),
    因此诊断结果是可执行的, 而不是一句「理解不够深入」。
    """
    cfg = config or PedagogyConfig()
    gaps: list[GapItem] = []
    atom_index = {atom.id: atom for atom in atoms}
    claim_by_id = {claim.id: claim for claim in claims}

    # 本轮已经被讲通的 Atom(supported claim 指向的那些), 用来判 DAG 上是否缺边
    covered = {claim.atom_ref for claim in claims if claim.status == "supported" and claim.atom_ref}

    # --- 1. 术语盾: 出现术语, 但该术语从未被展开(用术语解释术语)
    # 两个信号取并集: 判分器交出的 ``jargon_terms``, 以及被标成 ``jargon_shield`` 的 claim。
    # 只用其中一个都会漏诊(模型经常只给其一), 这是判分一致率 κ 的主要来源之一。
    shield_terms = [
        term
        for term in dict.fromkeys(t for t in jargon_terms if t)  # 去重且保序
        if term in raw_text and not _term_expanded(term, claims, atom_index)
    ]
    pending_shields = {claim.id for claim in claims if claim.status == "jargon_shield"}
    for term in shield_terms:
        claim_id = _first_claim_mentioning(claims, term)
        pending_shields.discard(claim_id)
        gaps.append(
            GapItem(
                type="jargon_shield",
                claim_id=claim_id,
                probe=DEFAULT_PROBES["jargon_shield"].format(term=term),
                severity=3,  # 术语盾会阻断闭合, 严重度最高
                supply=_supply_for(claim_by_id.get(claim_id), atom_index, cfg, covered),
            )
        )
    # 判分器标了 jargon_shield 却没交出术语: 仍然记账, 追问退化为「用大白话重说」
    for claim_id in sorted(pending_shields):
        claim = claim_by_id[claim_id]
        term = next((t for t in jargon_terms if t and t in claim.text), "")
        gaps.append(
            GapItem(
                type="jargon_shield",
                claim_id=claim_id,
                probe=(
                    DEFAULT_PROBES["jargon_shield"].format(term=term)
                    if term
                    else GENERIC_JARGON_PROBE
                ),
                severity=3,
                supply=_supply_for(claim, atom_index, cfg, covered),
            )
        )

    # --- 2. 因果断链: 步骤正确但「为什么可以这样」缺失(pedagogy-core §3)
    # 两个信号, 都要求出现「然后就 / 所以」这类跳过理由的连接词:
    #   A. status=unsupported -> 直接说不出理由(严重度 2);
    #   B. status=supported   ->  能复述流程, 但该命题的前置 Atom 在本轮从未被讲到,
    #      即 Atom DAG 上缺一条 entails 边(严重度 1, 补给那条边即可)。
    connective_in_raw = has_causal_connective(raw_text)
    for claim in claims:
        if claim.gap_type not in (None, "causal_gap"):
            continue  # 判分器已归到别的类, 尊重它
        if claim.status not in ("unsupported", "supported"):
            continue
        if not (has_causal_connective(claim.text) or connective_in_raw):
            continue
        edge = _missing_edge(claim, atom_index, covered)
        if claim.status == "supported" and edge is None:
            continue  # 流程对、边也在 -> 不是断链
        gaps.append(
            GapItem(
                type="causal_gap",
                claim_id=claim.id,
                probe=claim.note.strip() or DEFAULT_PROBES["causal_gap"],
                severity=2 if claim.status == "unsupported" else 1,
                missing_edge=edge,
                supply=_supply_for(claim, atom_index, cfg, covered),
            )
        )

    # --- 3. 类比失配: 有类比, 且被判为失配(调用方标注或判分器把 claim 归到这一类)
    analogy_claims = [claim for claim in claims if claim.gap_type == "broken_analogy"]
    if analogy and (analogy_flagged or analogy_claims):
        anchor = analogy_claims[0] if analogy_claims else None
        gaps.append(
            GapItem(
                type="broken_analogy",
                claim_id=anchor.id if anchor else "",
                probe=(anchor.note.strip() if anchor and anchor.note.strip() else "")
                or DEFAULT_PROBES["broken_analogy"],
                severity=2,
                supply=_supply_for(anchor, atom_index, cfg, covered),
            )
        )

    # --- 4. 边界缺失: 说不出适用条件/反例(仅在要求边界的阶段判定)
    if require_boundary:
        boundary_claims = [claim for claim in claims if claim.gap_type == "no_boundary"]
        if not (counterexample or "").strip():
            # 一个反例都没给 -> 整体判为无边界, 不再逐条 claim 重复记账
            anchor = boundary_claims[0] if boundary_claims else None
            gaps.append(
                GapItem(
                    type="no_boundary",
                    claim_id=anchor.id if anchor else "",
                    probe=(anchor.note.strip() if anchor and anchor.note.strip() else "")
                    or DEFAULT_PROBES["no_boundary"],
                    severity=2,
                )
            )
        else:
            # 给了反例, 但仍有命题缺适用条件 -> 逐条追问
            for claim in boundary_claims:
                gaps.append(
                    GapItem(
                        type="no_boundary",
                        claim_id=claim.id,
                        probe=claim.note.strip() or DEFAULT_PROBES["no_boundary"],
                        severity=2,
                    )
                )

    return tuple(gaps)


# --------------------------------------------------------------------------- 检测器


class GapDetector:
    """把一份自述变成 ``(ExplanationTurn, GapReport)``。"""

    def __init__(
        self,
        grader: Grader,
        *,
        config: PedagogyConfig | None = None,
        prompt_builder: Any = build_prompt,
    ) -> None:
        self.grader = grader
        self.config = config or PedagogyConfig()
        self.prompt_builder = prompt_builder

    async def analyze(
        self,
        *,
        explanation: str,
        concept_id: str,
        atoms: Sequence[Atom] = (),
        state: FeynmanState = FeynmanState.DETECT,
        loop_index: int = 0,
        thread_id: str = "",
        account_id: int = 0,
        learner_chars: int = 0,
        system_chars: int = 0,
        analogy_flagged: bool = False,
        explanation_id: str = "",
    ) -> Detection:
        require_boundary = state is FeynmanState.SIMPLIFY
        system, user = self.prompt_builder(
            explanation=explanation,
            concept_id=concept_id,
            atoms=atoms,
            require_boundary=require_boundary,
        )
        raw = await self.grader.grade(system=system, user=user)
        result = _parse_grade(raw)

        claims = _to_claims(result.claims)
        gaps = derive_gaps(
            claims=claims,
            raw_text=explanation,
            jargon_terms=result.jargon_terms,
            analogy=result.analogy,
            counterexample=result.counterexample,
            atoms=atoms,
            require_boundary=require_boundary,
            analogy_flagged=analogy_flagged,
            config=self.config,
        )
        verified_ids = [atom.id for atom in atoms if atom.verified]
        scores = GapScores(
            completeness=completeness_of(claims, verified_ids),
            own_words=result.own_words,
            boundary_awareness=result.boundary_awareness,
            reason=result.reason or "(判分器未给出理由)",
        )
        turn = ExplanationTurn(
            id=explanation_id,
            thread_id=thread_id,
            account_id=account_id,
            concept_id=concept_id,
            loop_index=loop_index,
            state=state,
            raw_text=explanation,
            claims=tuple(claims),
            jargon_terms=tuple(result.jargon_terms),
            analogy=result.analogy,
            counterexample=result.counterexample,
        )
        wrong_ids = tuple(c.id for c in claims if c.status == "wrong")
        report = GapReport(
            explanation_id=explanation_id,
            gaps=gaps,
            scores=scores,
            learner_talk_ratio=_ratio(learner_chars + len(explanation), system_chars),
            # 与已验证 Atom 冲突的命题本身不产生卡点(它走 wrong_claim_ids 通道),
            # 但一样阻断闭合 —— 报告的 verdict 必须与 loop.closure_verdict 同口径,
            # 否则前端会看到「verdict=closed 但没闭合」这种自相矛盾的证据。
            verdict="closed" if not gaps and not wrong_ids else "not_closed",
            wrong_claim_ids=wrong_ids,
        )
        return Detection(turn=turn, report=report)


# --------------------------------------------------------------------------- 内部工具


def _text_of(content: Any) -> str:
    """AIMessage.content 可能是 str, 也可能是多模态块列表。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and isinstance(block.get("text"), str):
                parts.append(block["text"])
        return "".join(parts)
    return str(content or "")


_FENCE = re.compile(r"^\s*(?:```|~~~)\s*(?:json|JSON)?\s*|\s*(?:```|~~~)\s*$")


def _parse_grade(raw: str) -> GradeResult:
    """解析判分器输出: 容忍代码块围栏与前后废话, 但不容忍非法结构。"""
    text = _FENCE.sub("", (raw or "").strip()).strip()
    if not text:
        raise GradingError("判分器返回空内容")
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise GradingError(f"判分器输出里找不到 JSON 对象: {text[:120]!r}")
    try:
        payload = json.loads(text[start : end + 1])
    except json.JSONDecodeError as exc:
        raise GradingError(f"判分器输出不是合法 JSON: {exc}") from exc
    try:
        return GradeResult.model_validate(payload)
    except ValidationError as exc:
        raise GradingError(f"判分器输出结构不合法: {exc}") from exc


def _to_claims(raw_claims: Sequence[Any]) -> list[Claim]:
    """把判分器给的 claim 字典转成 :class:`Claim`, 并自动补 id、丢弃非法项。

    自动编号按**保留下来的**顺序(c1, c2, …), 被丢弃的非法项不占号 —— 否则
    claim_id 会与前端展示的第几条命题对不上。
    """
    claims: list[Claim] = []
    for item in raw_claims:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        status = item.get("status")
        if status not in ClaimStatus.__args__:  # type: ignore[attr-defined]
            status = "vague"
        gap_type = item.get("gap_type")
        if gap_type not in GapType.__args__:  # type: ignore[attr-defined]
            gap_type = None
        if status == "supported":
            gap_type = None  # schema 不变量: supported 不带 gap_type
        try:
            claims.append(
                Claim(
                    id=str(item.get("id") or f"c{len(claims) + 1}"),
                    text=text,
                    atom_ref=item.get("atom_ref") or None,
                    status=status,
                    gap_type=gap_type,
                    note=str(item.get("note") or ""),
                )
            )
        except ValidationError:  # pragma: no cover - 防御性: 非法项不该拖垮整份判分
            logger.warning("丢弃非法 claim: %r", item)
    return claims


def _ratio(learner_chars: int, system_chars: int) -> float:
    total = learner_chars + system_chars
    return round(learner_chars / total, 4) if total else 0.0


def _term_expanded(term: str, claims: Sequence[Claim], atom_index: dict[str, Atom]) -> bool:
    """术语是否**已被展开**: 存在一条 supported claim 真的把它讲清楚了。

    两个充分条件(满足其一即算展开):
    1. 某条 supported claim 提到该术语, 且长度明显超过术语本身(说明是在解释它);
    2. 某条 supported claim 引用的 Atom, 其 ``statement`` 就是在讲这个术语。
    """
    if not term:
        return True
    for claim in claims:
        if claim.status != "supported":
            continue
        if term in claim.text and len(claim.text) > len(term) + 8:
            return True
        atom = atom_index.get(claim.atom_ref or "")
        if atom is not None and term in atom.statement:
            return True
    return False


def _first_claim_mentioning(claims: Sequence[Claim], term: str) -> str:
    return next((c.id for c in claims if term in c.text), "")


def _missing_edge(
    claim: Claim, atom_index: dict[str, Atom], covered: set[str | None] = frozenset()
) -> MissingEdge | None:
    """从 claim 的 atom_ref 推出「缺的那条边」(``from`` entails ``to``)。

    只认 ``atom_index`` 里真实存在、且本轮**尚未被讲通**的前置 —— 宁可说
    「没有边可补」, 也不编一个幽灵 Atom。多个前置未覆盖时取第一个
    (Atom 顺序即教学顺序)。
    """
    if not claim.atom_ref:
        return None
    atom = atom_index.get(claim.atom_ref)
    if atom is None:
        return None
    known = [dep for dep in atom.depends_on if dep in atom_index]
    # 全部前置都已被讲通 -> 没有「缺的那条边」可指, 返回 None(信号 B 因此不会误触发)
    missing = next((dep for dep in known if dep not in covered), None)
    if missing is None:
        return None
    return MissingEdge(from_atom=missing, to_atom=atom.id)


def _supply_for(
    claim: Claim | None,
    atom_index: dict[str, Atom],
    cfg: PedagogyConfig,
    covered: set[str | None] = frozenset(),
) -> AtomBundle | None:
    """补给包: 优先补 claim **还没讲通**的那条前置 Atom, 没有就补它引用的 Atom 本身。"""
    if claim is None:
        return None
    atom = atom_index.get(claim.atom_ref or "")
    if atom is None:
        return None
    wanted: list[str] = [
        dep for dep in atom.depends_on if dep in atom_index and dep not in covered
    ][:1]
    wanted.append(atom.id)
    atoms = tuple(atom_index[atom_id] for atom_id in dict.fromkeys(wanted))
    if not atoms:
        return None
    # 未验证的 Atom 只能用于提示, 不得参与掌握度记账(vision §9)
    return AtomBundle(atoms=atoms, max_chars=min(cfg.return_max_chars, 300))
