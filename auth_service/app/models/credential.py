"""登录凭证表: 统一承载密码凭证与 OAuth2 社交凭证(多身份绑定)。"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, SmallInteger, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.session import Base


class AuthCredential(Base):
    """一个账号可挂 N 条凭证: phone/email/username 走密码, wechat/apple 走 OAuth。"""

    __tablename__ = "auth_credential"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    account_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("account.id", ondelete="CASCADE"))
    identity_type: Mapped[str] = mapped_column(String(20), comment="phone/email/username/wechat/...")
    identifier: Mapped[str] = mapped_column(String(255), comment="归一化标识; OAuth 为 provider:openid")
    credential_hash: Mapped[str | None] = mapped_column(String(255), comment="Argon2id 哈希; OAuth 为 NULL")
    oauth_provider: Mapped[str | None] = mapped_column(String(32))
    oauth_openid: Mapped[str | None] = mapped_column(String(128))
    oauth_unionid: Mapped[str | None] = mapped_column(String(128))
    verified: Mapped[int] = mapped_column(SmallInteger, default=0)
    status: Mapped[int] = mapped_column(SmallInteger, default=1, comment="1=启用 2=禁用")
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), server_onupdate=func.now()
    )

    account: Mapped[Account] = relationship(back_populates="credentials")


from app.models.account import Account  # noqa: E402
