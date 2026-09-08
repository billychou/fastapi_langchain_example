"""技能(skills)API: 目录 / 详情 / 附件(只读) + 管理端热重载。

与 agent 共用同一个 :class:`~app.skills.registry.SkillRegistry` 与同一套鉴权规则
(:meth:`app.skills.schema.SkillManifest.allows`), 因此「技能页看到的内容」严格等于
「模型在该账号下能看到的内容」, 不会出现页面列了、模型却调不到的错觉。

鉴权分层:

- 读接口(目录/详情/附件): 登录即可, 但只返回该调用者可见的技能 —— 与 ``/account/me``
  一样属于自服务, 不额外引入权限点; 技能门禁由各技能 frontmatter 的
  ``visibility`` / ``requires_permissions`` 决定。
- 不可见技能一律按 **404** 处理(而不是 403), 与工具层一致, 避免技能名枚举。
- ``skill:admin``: 可以用 ``scope=all`` 看到含门禁技能的全量目录、读取任意技能内容与
  加载失败原因, 并强制热重载技能目录。
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, Path, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.net import resolve_client_ip
from app.db.session import get_db
from app.deps import AuthContext, get_current, require_permissions
from app.exceptions import AuthError, BizCode, BizError
from app.schemas import SkillDetail, SkillFile, SkillReloadResult, SkillSummary
from app.schemas.common import ok
from app.services import audit_service
from app.skills import SkillRegistry, get_skill_registry
from app.skills.schema import SKILL_NAME_PATTERN, SkillManifest
from app.skills.security import SkillSecurityError

router = APIRouter(prefix="/skills", tags=["skills"])

ADMIN_PERM = "skill:admin"
NamePath = Path(max_length=64, pattern=SKILL_NAME_PATTERN)


def _registry() -> SkillRegistry:
    """每次请求现取单例(而不是模块级常量), 便于测试替换与配置热更。"""
    return get_skill_registry()


def _is_admin(ctx: AuthContext) -> bool:
    return ctx.has(ADMIN_PERM)


def _summary(skill: SkillManifest) -> SkillSummary:
    return SkillSummary(
        name=skill.name,
        version=skill.version,
        description=skill.description,
        visibility=skill.visibility,
        requires_permissions=list(skill.requires_permissions),
        tools=list(skill.tools),
        files=list(skill.files),
    )


def _detail(registry: SkillRegistry, skill: SkillManifest) -> SkillDetail:
    return SkillDetail(
        **_summary(skill).model_dump(),
        max_body_chars=min(skill.max_body_chars, registry.default_max_body_chars),
        # 与 load_skill 交给模型的文本完全一致, 前端所见即模型所得
        body=registry.render_body(skill),
    )


def _authorized(registry: SkillRegistry, ctx: AuthContext, name: str) -> SkillManifest:
    """取调用者有权访问的技能; 不存在与无权限同样返回 404(不泄露技能是否存在)。"""
    skill = registry.authorize(name, permissions=ctx.permissions, authenticated=True)
    if skill is None and _is_admin(ctx):  # 管理端可越过门禁排障
        skill = registry.get(name)
    if skill is None:
        raise BizError(BizCode.NOT_FOUND, "技能不存在")
    return skill


@router.post(
    "/reload",
    summary="强制重扫技能目录(需 skill:admin)",
)
async def reload_skills(
    request: Request,
    ctx: AuthContext = Depends(require_permissions(ADMIN_PERM)),
    db: AsyncSession = Depends(get_db),
):
    """技能目录本身按 SKILL.md 的 mtime 指纹惰性重扫, 通常无需调用本接口。

    需要它的场景: 挂载了新卷/新目录、批量替换技能后要立刻生效、或排查「技能没被识别」。
    重载是进程级的(单例注册表), 多副本部署需逐个实例调用。
    """
    result = _registry().reload()
    payload = SkillReloadResult(
        count=len(result.names), names=result.names, errors=list(result.errors)
    )
    await audit_service.record_audit(
        db,
        account_id=ctx.account_id,
        action="skill_reload",
        target_type="skill",
        target_id=None,
        ip=resolve_client_ip(request),
        user_agent=request.headers.get("User-Agent"),
        detail={"count": payload.count, "errors": payload.errors},
    )
    return ok(payload.model_dump())


@router.get("", summary="当前账号可见的技能目录")
async def list_skills(
    scope: Literal["visible", "all"] = Query(
        default="visible",
        description="visible=仅当前账号可见(默认); all=全量含门禁技能, 需 skill:admin",
    ),
    ctx: AuthContext = Depends(get_current),
):
    registry = _registry()
    if scope == "all":
        if not _is_admin(ctx):
            raise AuthError(BizCode.FORBIDDEN, f"权限不足: 缺少 ['{ADMIN_PERM}']", http_status=403)
        skills = registry.all()
    else:
        # 读接口本身要求登录(get_current), 因此 authenticated 恒为 True
        skills = registry.visible_to(permissions=ctx.permissions, authenticated=True)

    data: dict = {
        "enabled": registry.enabled,
        "scope": scope,
        "count": len(skills),
        "items": [_summary(skill).model_dump() for skill in skills],
    }
    if _is_admin(ctx):
        data["load_errors"] = registry.errors  # 仅管理端可见: 哪些技能目录加载失败
    return ok(data)


@router.get("/{name}", summary="技能详情(含模型将读到的正文)")
async def get_skill(
    name: str = NamePath,
    ctx: AuthContext = Depends(get_current),
):
    registry = _registry()
    return ok(_detail(registry, _authorized(registry, ctx, name)).model_dump())


@router.get("/{name}/files/{file_path:path}", summary="读取技能附件文本(L3)")
async def get_skill_file(
    file_path: str,
    name: str = NamePath,
    ctx: AuthContext = Depends(get_current),
):
    registry = _registry()
    skill = _authorized(registry, ctx, name)
    try:
        content = registry.read_file(skill, file_path)
    except SkillSecurityError:
        # 路径穿越/软链接/非文本/超限一律 404: 不把防护细节与真实文件系统结构暴露出去
        raise BizError(BizCode.NOT_FOUND, "技能附件不存在") from None
    return ok(
        SkillFile(name=skill.name, path=file_path, content=content, chars=len(content)).model_dump()
    )
