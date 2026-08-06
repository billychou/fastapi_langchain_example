# User & Auth Service

企业级用户与身份认证系统: FastAPI + MySQL(SQLAlchemy 2.0 async) + Redis + JWT 双 Token。

## 特性

- **多身份账号体系**: `account`(主表) / `auth_credential`(凭证) / `user_profile`(资料) 三表解耦,
  单账号可绑定 手机号 / 邮箱 / 用户名 / 微信(OAuth2) 等多种登录方式。
- **双 Token 无感续期**: Access(15min, 无状态验签) + Refresh(30d, 服务端会话绑定);
  刷新即轮换(Rotation) + 重放检测(Reuse Detection); 支持登出/踢人/改密的即时撤销。
- **多端策略**: 账号级 `multi_device` / `single_device`(单端互踢), 会话仓库在 Redis。
- **RBAC**: 用户 ↔ 角色 ↔ API权限/菜单, Redis 权限缓存; 提供依赖注入式
  `require_permissions(...)` 与全局中间件两种拦截形态。
- **安全防护**: Argon2id 密码哈希(OWASP 参数) / Redis 令牌桶限流(Lua 原子) /
  登录失败指数退避锁定 / PII 字段级 AES-256-GCM 加密 + HMAC 盲索引 + 脱敏 /
  登录审计日志(IP、UA、设备ID、结果)。

## 目录结构

```
auth_service/
├── migrations/          # MySQL DDL 与 RBAC 种子数据
│   ├── 001_schema.sql
│   └── 002_seed_rbac.sql
├── docs/architecture.md # 架构图 + 双 Token 时序图
├── app/
│   ├── main.py          # 应用工厂: 中间件/异常信封/路由挂载
│   ├── config.py        # pydantic-settings 配置
│   ├── deps.py          # get_current / require_permissions (RBAC 拦截器)
│   ├── db/              # 异步引擎 / Redis 客户端
│   ├── models/          # account / credential / rbac / audit
│   ├── core/            # Argon2id / JWT / AES-GCM+盲索引 / 令牌桶限流
│   ├── services/        # auth 主流程 / 会话仓库 / RBAC / 审计
│   ├── middleware/      # RequestId / 安全头 / 全局RBAC(演示)
│   └── api/v1/          # auth / account / admin 路由
└── .env.example
```

## 快速开始

```bash
cd auth_service
uv sync
cp .env.example .env          # 填入 JWT_SECRET_KEY / 数据库 / Redis / PII 密钥

# 初始化数据库
mysql -uroot -p < migrations/001_schema.sql
mysql -uroot -p < migrations/002_seed_rbac.sql

uv run uvicorn app.main:app --reload --port 8000
# 开发环境打开 http://127.0.0.1:8000/docs 查看 OpenAPI
```

## 接口速览

| Method | Path | 说明 | 鉴权 |
|---|---|---|---|
| POST | /api/v1/auth/register | 注册(手机号/邮箱+密码) | 匿名(限流) |
| POST | /api/v1/auth/login | 登录, 返回双 Token | 匿名(限流+锁定) |
| POST | /api/v1/auth/refresh | 无感续期(轮换+重放检测) | Refresh Token |
| POST | /api/v1/auth/logout | 登出(可选 all_devices) | Access |
| POST | /api/v1/auth/password/change | 改密(全端下线) | Access |
| POST | /api/v1/auth/sms/send | 发送验证码 | 匿名(限流) |
| POST | /api/v1/auth/oauth/{provider}/callback | OAuth 登录/注册(演示) | 匿名 |
| GET | /api/v1/account/me | 当前资料(PII 脱敏) | Access |
| GET | /api/v1/account/sessions | 在线会话列表 | Access |
| DELETE | /api/v1/account/sessions/{sid} | 下线指定设备 | Access |
| PUT | /api/v1/account/login-policy | 切换单端/多端策略 | Access |
| GET | /api/v1/admin/accounts | 账号分页列表 | account:read |
| POST | /api/v1/admin/accounts/{uuid}/roles | 全量分配角色 | rbac:assign |

## 生产上线前必读

见 `docs/architecture.md` 与会话回复中的《安全防范 Checklist》,
重点: JWT/PII 密钥轮换、TLS、限流参数压测、审计日志采集、Redis 高可用。
