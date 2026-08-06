"""账号主表与用户资料表(1:1)。PII 只存在于 user_profile 且加密存储。"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, SmallInteger, String, func
from sqlalchemy.dialects.mysql import VARBINARY
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.session import Base


def _new_uuid() -> str:
    return str(uuid.uuid4())


class Account(Base):
    """账号主表: 只承载生命周期与登录策略, 与具体登录方式解耦。"""

    __tablename__ = "account"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    account_uuid: Mapped[str] = mapped_column(String(36), unique=True, default=_new_uuid)
    status: Mapped[int] = mapped_column(SmallInteger, default=1, comment="1=正常 2=锁定 3=已注销")
    login_policy: Mapped[str] = mapped_column(String(16), default="multi_device")
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), server_onupdate=func.now()
    )

    profile: Mapped[UserProfile] = relationship(
        back_populates="account", uselist=False, cascade="all, delete-orphan", lazy="selectin"
    )
    credentials: Mapped[list[AuthCredential]] = relationship(
        back_populates="account", cascade="all, delete-orphan"
    )


class UserProfile(Base):
    """用户资料: PII 采用 密文(enc) + 盲索引(hash) + 脱敏值(masked) 三元组。"""

    __tablename__ = "user_profile"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    account_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("account.id", ondelete="CASCADE"), unique=True
    )
    nickname: Mapped[str | None] = mapped_column(String(64))
    avatar_url: Mapped[str | None] = mapped_column(String(512))

    phone_enc: Mapped[bytes | None] = mapped_column(VARBINARY(512))
    phone_hash: Mapped[str | None] = mapped_column(String(64), unique=True)
    phone_masked: Mapped[str | None] = mapped_column(String(32))

    email_enc: Mapped[bytes | None] = mapped_column(VARBINARY(512))
    email_hash: Mapped[str | None] = mapped_column(String(64), unique=True)
    email_masked: Mapped[str | None] = mapped_column(String(128))

    id_card_enc: Mapped[bytes | None] = mapped_column(VARBINARY(512))
    id_card_masked: Mapped[str | None] = mapped_column(String(32))

    key_id: Mapped[str] = mapped_column(String(32), default="k1", comment="加密密钥版本, 支持轮转")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), server_onupdate=func.now()
    )

    account: Mapped[Account] = relationship(back_populates="profile")


# 避免循环导入: credential 在 account 之后定义
from app.models.credential import AuthCredential  # noqa: E402
