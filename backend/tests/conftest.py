"""pytest 全局配置: 在导入任何 app 模块之前冻结环境变量。

测试环境原则:
- 不读本地 .env(显式环境变量优先级高于 .env 文件);
- 不依赖外部 MySQL/Redis: DATABASE_URL 指向死地址(引擎惰性创建, 用例不触发连接),
  REDIS_URL 指向死端口(验证聊天限流的降级路径);
- checkpointer SQLite 放临时目录, 不污染仓库根;
- LLM 固定 mock, 零网络、零密钥。
"""
from __future__ import annotations

import base64
import hashlib
import os
import tempfile
from pathlib import Path


def _b64_32(seed: str) -> str:
    """确定性的 32 字节 base64 密钥(仅测试用)。"""
    return base64.b64encode(hashlib.sha256(seed.encode()).digest()).decode()


os.environ.update(
    {
        "APP_ENV": "dev",
        "DEBUG": "false",
        # ---- LLM: 强制 mock, 清空真实密钥 ----
        "LLM_PROVIDER": "mock",
        "OPENAI_API_KEY": "",
        "OPENAI_BASE_URL": "",
        "ANTHROPIC_API_KEY": "",
        # ---- 聊天接口: 匿名演示模式 ----
        "CHAT_REQUIRE_AUTH": "false",
        "TRUSTED_PROXY_HOPS": "0",
        # ---- 密钥(满足安全闸门即可, 全为测试值) ----
        "JWT_SECRET_KEY": hashlib.sha256(b"test-jwt-secret").hexdigest(),
        "PII_KEYS": f"k1:{_b64_32('test-k1')},k2:{_b64_32('test-k2')}",
        "PII_ACTIVE_KEY_ID": "k1",
        "PII_BLIND_INDEX_KEY": _b64_32("test-blind-index"),
        # ---- 外部依赖全部指向死地址 ----
        "REDIS_URL": "redis://127.0.0.1:6390/0",
        "DATABASE_URL": "mysql+aiomysql://user:pass@127.0.0.1:3399/noop?charset=utf8mb4",
        "CHECKPOINT_DB_PATH": os.path.join(
            tempfile.mkdtemp(prefix="agent-ckpt-"), "checkpoints.sqlite"
        ),
        # ---- 技能目录: 绝对路径, 不随 pytest 工作目录漂移 ----
        "SKILLS_DIR": str(Path(__file__).resolve().parents[1] / "skills"),
    }
)
