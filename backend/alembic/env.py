"""Alembic 迁移环境: 异步 aiomysql, URL 与模型元数据均取自应用配置。

用法(在 backend/ 目录):
- 生成迁移:  uv run alembic revision --autogenerate -m "Add xxx"
- 升级:      uv run alembic upgrade head
- 查看当前:  uv run alembic current
数据库地址来自 DATABASE_URL(默认取 .env), 无需在 alembic.ini 里重复维护。
"""
from __future__ import annotations

import asyncio
from logging.config import fileConfig

import app.models  # noqa: F401  # 确保所有模型在 autogenerate 前完成注册
from alembic import context
from app.config import get_settings
from app.db.session import Base
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# 以应用配置为唯一事实来源, 避免 alembic.ini 与应用 .env 双份维护
config.set_main_option("sqlalchemy.url", get_settings().database_url)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Offline mode: 仅生成 SQL 脚本, 不连接数据库。"""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
