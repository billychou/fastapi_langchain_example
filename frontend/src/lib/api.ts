/**
 * 后端 API 客户端: 统一信封解析 + 双 Token 管理(预刷新 + 401 自动续期)。
 */

export const API_BASE =
  process.env.NEXT_PUBLIC_API_BASE || 'http://127.0.0.1:5001';

const ACCESS_KEY = 'auth.accessToken';
const REFRESH_KEY = 'auth.refreshToken';

// ---------------- 类型 ----------------
export interface ApiEnvelope<T> {
  code: number;
  message: string;
  data: T;
}

export interface TokenPair {
  access_token: string;
  refresh_token: string;
  token_type: string;
  expires_in: number;
  session_id: string;
}

export interface AccountInfo {
  account_uuid: string;
  nickname?: string | null;
  avatar_url?: string | null;
  phone_masked?: string | null;
  email_masked?: string | null;
  roles: string[];
}

export interface AdminAccountItem {
  account_uuid: string;
  status: number;
  login_policy: string;
  nickname?: string | null;
  phone_masked?: string | null;
  email_masked?: string | null;
  last_login_at?: string | null;
  created_at?: string | null;
}

export interface RoleItem {
  role_code: string;
  role_name: string;
  description?: string | null;
  is_builtin: boolean;
}

export class ApiError extends Error {
  code: number;
  constructor(code: number, message: string) {
    super(message);
    this.code = code;
  }
}

// ---------------- Token 存储 ----------------
export function getAccessToken(): string | null {
  if (typeof window === 'undefined') return null;
  return window.localStorage.getItem(ACCESS_KEY);
}

export function getRefreshToken(): string | null {
  if (typeof window === 'undefined') return null;
  return window.localStorage.getItem(REFRESH_KEY);
}

export function setTokens(pair: TokenPair): void {
  window.localStorage.setItem(ACCESS_KEY, pair.access_token);
  window.localStorage.setItem(REFRESH_KEY, pair.refresh_token);
}

export function clearTokens(): void {
  window.localStorage.removeItem(ACCESS_KEY);
  window.localStorage.removeItem(REFRESH_KEY);
}

/** 解析 JWT exp(秒); 解析失败返回 null。 */
function jwtExpSeconds(token: string): number | null {
  try {
    const payload = JSON.parse(
      atob(token.split('.')[1].replace(/-/g, '+').replace(/_/g, '/')),
    );
    return typeof payload.exp === 'number' ? payload.exp : null;
  } catch {
    return null;
  }
}

// ---------------- 刷新(单飞) ----------------
let refreshPromise: Promise<string | null> | null = null;

/** 用 Refresh Token 换新双 Token; 失败返回 null(需重新登录)。 */
export function refreshTokens(): Promise<string | null> {
  if (!refreshPromise) {
    refreshPromise = (async () => {
      try {
        const refresh_token = getRefreshToken();
        if (!refresh_token) return null;
        const resp = await fetch(`${API_BASE}/api/v1/auth/refresh`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ refresh_token }),
        });
        const body: ApiEnvelope<TokenPair> = await resp.json();
        if (resp.ok && body.code === 0) {
          setTokens(body.data);
          return body.data.access_token;
        }
        clearTokens();
        return null;
      } catch {
        return null;
      } finally {
        setTimeout(() => {
          refreshPromise = null;
        }, 0);
      }
    })();
  }
  return refreshPromise;
}

/**
 * 取"当前可用"的 Access Token:
 * - 剩余有效期 > 60s 直接返回;
 * - 临近过期则先刷新(无感续期);
 * - 无 Refresh Token 返回 null。
 */
export async function getValidAccessToken(): Promise<string | null> {
  const token = getAccessToken();
  if (!token) return null;
  const exp = jwtExpSeconds(token);
  if (exp && exp * 1000 - Date.now() > 60_000) return token;
  return refreshTokens();
}

// ---------------- 通用请求 ----------------
async function request<T>(
  path: string,
  init: RequestInit,
  retried = false,
): Promise<T> {
  const token = await getValidAccessToken();
  const headers = new Headers(init.headers);
  if (token) headers.set('Authorization', `Bearer ${token}`);
  if (init.body && !headers.has('Content-Type'))
    headers.set('Content-Type', 'application/json');

  const resp = await fetch(`${API_BASE}${path}`, { ...init, headers });
  let body: ApiEnvelope<T> | null = null;
  try {
    body = (await resp.json()) as ApiEnvelope<T>;
  } catch {
    /* 非 JSON 响应 */
  }

  // Access Token 失效 → 刷新一次并重试
  if (!retried && (resp.status === 401 || body?.code === 40101)) {
    const fresh = await refreshTokens();
    if (fresh) return request<T>(path, init, true);
    clearTokens();
  }

  if (!resp.ok || !body || body.code !== 0) {
    throw new ApiError(
      body?.code ?? resp.status,
      body?.message ?? `请求失败(${resp.status})`,
    );
  }
  return body.data;
}

// ---------------- 业务接口 ----------------
export interface LoginParams {
  identity_type: 'phone' | 'email';
  identifier: string;
  password: string;
  device_id?: string;
}

export interface RegisterParams {
  identity_type: 'phone' | 'email';
  identifier: string;
  password: string;
  nickname?: string;
}

export const authApi = {
  login: (params: LoginParams) =>
    request<TokenPair & { account_uuid: string }>('/api/v1/auth/login', {
      method: 'POST',
      body: JSON.stringify(params),
    }),
  register: (params: RegisterParams) =>
    request<{ account_uuid: string }>('/api/v1/auth/register', {
      method: 'POST',
      body: JSON.stringify(params),
    }),
  logout: (all_devices = false) =>
    request<{ all_devices: boolean }>('/api/v1/auth/logout', {
      method: 'POST',
      body: JSON.stringify({ all_devices }),
    }),
  me: () => request<AccountInfo>('/api/v1/account/me', { method: 'GET' }),

  // 管理端
  listAccounts: (page: number, pageSize: number) =>
    request<{ total: number; items: AdminAccountItem[] }>(
      `/api/v1/admin/accounts?page=${page}&page_size=${pageSize}`,
      { method: 'GET' },
    ),
  listRoles: () =>
    request<RoleItem[]>('/api/v1/admin/roles', { method: 'GET' }),
  getAccountRoles: (uuid: string) =>
    request<{ account_uuid: string; role_codes: string[] }>(
      `/api/v1/admin/accounts/${uuid}/roles`,
      { method: 'GET' },
    ),
  assignRoles: (uuid: string, roleCodes: string[]) =>
    request<{ account_uuid: string; roles: string[] }>(
      `/api/v1/admin/accounts/${uuid}/roles`,
      {
        method: 'POST',
        body: JSON.stringify({ role_codes: roleCodes }),
      },
    ),
};

// ---------------- Agent 会话 (agent_threads) ----------------
export interface ThreadItem {
  thread_id: string;
  title: string;
  last_message: string | null;
  created_at: string;
  updated_at: string;
}

export interface ThreadMessage {
  role: 'user' | 'assistant';
  content: string;
}

export const threadApi = {
  list: () => request<ThreadItem[]>('/api/v1/threads', { method: 'GET' }),
  create: (title?: string) =>
    request<ThreadItem>('/api/v1/threads', {
      method: 'POST',
      body: JSON.stringify(title ? { title } : {}),
    }),
  rename: (threadId: string, title: string) =>
    request<ThreadItem>(`/api/v1/threads/${threadId}`, {
      method: 'PATCH',
      body: JSON.stringify({ title }),
    }),
  remove: (threadId: string) =>
    request<{ thread_id: string }>(`/api/v1/threads/${threadId}`, {
      method: 'DELETE',
    }),
  messages: (threadId: string) =>
    request<ThreadMessage[]>(`/api/v1/threads/${threadId}/messages`, {
      method: 'GET',
    }),
};
