# 多阶段构建: 依赖层与代码层分离, 最大化利用构建缓存
FROM python:3.13-slim

COPY --from=ghcr.io/astral-sh/uv:0.12.9 /uv /uvx /bin/

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    PATH="/app/.venv/bin:$PATH"

# 依赖层: 只拷锁文件, 代码变动不触发重装
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# 代码层 + Alembic 迁移(容器内执行 `alembic upgrade head` 用)
COPY app ./app
COPY alembic ./alembic
COPY alembic.ini ./
RUN uv sync --frozen --no-dev

# 非 root 运行 + checkpointer 数据目录
RUN useradd --create-home appuser \
    && mkdir -p /data \
    && chown -R appuser /app /data
USER appuser
VOLUME /data

EXPOSE 5001
HEALTHCHECK --interval=10s --timeout=3s --start-period=10s --retries=12 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:5001/api/health', timeout=2)" || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "5001"]
