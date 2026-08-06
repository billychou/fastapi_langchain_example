"""审计落库: 登录日志与敏感操作日志。写入失败不应阻断主流程。"""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit import AuditLog, LoginLog

logger = logging.getLogger("auth.audit")


async def record_login(
    db: AsyncSession,
    *,
    account_id: int | None,
    identity_type: str | None,
    identifier_masked: str | None,
    ip: str,
    user_agent: str | None,
    device_id: str | None,
    session_id: str | None,
    success: bool,
    fail_reason: str | None = None,
) -> None:
    try:
        db.add(
            LoginLog(
                account_id=account_id,
                identity_type=identity_type,
                identifier=identifier_masked,
                login_ip=ip,
                user_agent=(user_agent or "")[:512] or None,
                device_id=device_id,
                session_id=session_id,
                status=1 if success else 2,
                fail_reason=fail_reason,
            )
        )
        await db.commit()
    except Exception:  # noqa: BLE001 — 审计失败不阻断认证主链路
        logger.exception("写入登录日志失败")
        await db.rollback()


async def record_audit(
    db: AsyncSession,
    *,
    account_id: int | None,
    action: str,
    target_type: str | None = None,
    target_id: str | None = None,
    ip: str | None = None,
    user_agent: str | None = None,
    detail: dict[str, Any] | None = None,
) -> None:
    try:
        db.add(
            AuditLog(
                account_id=account_id,
                action=action,
                target_type=target_type,
                target_id=target_id,
                ip=ip,
                user_agent=(user_agent or "")[:512] or None,
                detail=detail,
            )
        )
        await db.commit()
    except Exception:  # noqa: BLE001
        logger.exception("写入审计日志失败")
        await db.rollback()
