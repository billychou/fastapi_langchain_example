# User & Auth Service — 架构与数据流转

## 1. 总体架构

```mermaid
flowchart LR
    subgraph Client["客户端 (Web / App / 小程序)"]
        C1["业务请求 Bearer Access Token"]
    end

    subgraph LB["接入层"]
        GW["网关 / Nginx (TLS 终止, IP 透传)"]
    end

    subgraph App["Auth Service (FastAPI, 水平扩展无状态)"]
        MW["中间件链: RequestId → 安全头 → (可选)全局RBAC"]
        DEP["依赖注入鉴权: get_current + require_permissions"]
        SVC["认证服务: 注册/登录/刷新/登出/OAuth"]
    end

    subgraph Store["存储层"]
        DB[("MySQL 8.0\n账号/凭证/RBAC/审计")]
        RD[("Redis\n会话仓库/黑名单/限流/锁定/权限缓存")]
    end

    subgraph Ext["外部依赖"]
        SMS["短信网关"]
        OAUTH["OAuth 提供方 (微信/Apple)"]
    end

    C1 --> GW --> MW --> DEP --> SVC
    SVC --> DB
    SVC --> RD
    SVC -.-> SMS
    SVC -.-> OAUTH
```

## 2. 登录时序图(含风控链)

```mermaid
sequenceDiagram
    autonumber
    participant C as "客户端"
    participant A as "Auth Service"
    participant R as "Redis"
    participant M as "MySQL"

    C->>A: POST /auth/login {identity_type, identifier, password, device_id}
    A->>R: 令牌桶限流 (IP 桶 + 账号桶, Lua 原子扣减)
    alt 超限
        R-->>A: 拒绝
        A-->>C: 429 + Retry-After
    end
    A->>R: 查询锁定 auth:lock:{identity}
    alt 已锁定
        A-->>C: 423 账号临时锁定
    end
    A->>M: 联查 auth_credential + account (identity_type, identifier)
    A->>A: Argon2id verify (账号不存在时 dummy verify 对齐时序)
    alt 密码错误
        A->>R: 失败计数 INCR auth:fail:{identity} (达阈值→锁定, 指数退避)
        A->>M: INSERT login_log (失败, IP/UA/设备ID)
        A-->>C: 401 "账号或密码错误" (不泄露账号是否存在)
    else 密码正确
        A->>R: DEL 失败计数
        A->>M: UPDATE last_login_at / 必要时 rehash 升级参数
        A->>A: 签发 Access(15min, jti, sid) + Refresh(30d, jti, sid)
        A->>R: HSET auth:sess:{uid}:{sid} (refresh_jti, 设备指纹) + 索引 SADD
        alt login_policy = single_device
            A->>R: 踢掉其余会话 (单端互踢)
        end
        A->>M: INSERT login_log (成功)
        A-->>C: 200 {access_token, refresh_token, expires_in}
    end
```

## 3. 双 Token 无感续期时序图(轮换 + 重放检测)

```mermaid
sequenceDiagram
    autonumber
    participant C as "客户端"
    participant A as "Auth Service"
    participant R as "Redis"
    participant M as "MySQL"

    Note over C: Access 临近过期(401 TOKEN_EXPIRED 或主动预刷新)
    C->>A: POST /auth/refresh {refresh_token}
    A->>A: JWT 验签 + exp + issuer + typ=refresh
    alt 验签失败/过期
        A-->>C: 401 TOKEN_INVALID / TOKEN_EXPIRED
    end
    A->>R: HGET auth:sess:{uid}:{sid}
    alt 会话不存在(已登出/被踢)
        A-->>C: 401 SESSION_REVOKED
    end
    A->>R: Lua CAS 轮换 refresh_jti (旧jti → 新jti)
    alt CAS 失败 = Refresh Token 重放
        A->>R: 撤销该会话(疑似凭证泄露)
        A-->>C: 401 TOKEN_REUSE_DETECTED (强制重新登录)
    else 轮换成功
        A->>M: 校验账号状态(锁定/注销)
        A->>A: 签发新 Access + 新 Refresh(jti 与 Redis 一致)
        A-->>C: 200 {新 access_token, 新 refresh_token}
        Note over C: 客户端原子替换双 Token
    end

    Note over C,R: 后续业务请求携带新 Access
    C->>A: GET /api/... (Bearer Access)
    A->>A: 验签 + typ=access
    A->>R: jti 黑名单检查 (登出即时失效)
    A->>R: strict 模式: 会话存在性检查 (踢人即时生效)
    A->>R: RBAC 权限缓存 auth:rbac:{uid} (miss 则查 MySQL)
    A-->>C: 200 业务数据 / 403 FORBIDDEN
```

## 4. 登出 / 改密 / 踢人

| 场景 | 动作 | 生效时机 |
|---|---|---|
| 登出当前端 | Access jti 入黑名单(TTL=剩余寿命) + 删除会话 | 即时 |
| 全端登出 | 黑名单 + 枚举 sess-idx 删除全部会话 | 即时 |
| 修改密码 | 更新 Argon2id 哈希 + 撤销全部会话 | 即时, 强制重新登录 |
| 单端互踢登录 | 新会话创建后删除其余 sid | 即时(strict_session_check) |
| 角色变更 | 删除 auth:rbac:{uid} 缓存 | ≤ 缓存 TTL 生效 |
