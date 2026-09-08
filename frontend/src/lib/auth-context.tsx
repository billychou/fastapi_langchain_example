'use client';

/**
 * 全局认证上下文: 登录态、当前用户、登录/注册/登出动作。
 */
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
} from 'react';
import {
  type AccountInfo,
  authApi,
  clearTokens,
  getAccessToken,
  getDeviceId,
  type LoginParams,
  type RegisterParams,
  setTokens,
} from './api';

interface AuthContextValue {
  user: AccountInfo | null;
  initializing: boolean;
  isAdmin: boolean;
  login: (params: LoginParams) => Promise<void>;
  register: (
    params: RegisterParams & { confirmPassword: string },
  ) => Promise<void>;
  logout: () => Promise<void>;
  refreshUser: () => Promise<void>;
}

const AuthContext = createContext<AuthContextValue | null>(null);

export const AuthProvider: React.FC<React.PropsWithChildren> = ({
  children,
}) => {
  const [user, setUser] = useState<AccountInfo | null>(null);
  const [initializing, setInitializing] = useState(true);

  const refreshUser = useCallback(async () => {
    setUser(await authApi.me());
  }, []);

  // 首次加载: 有本地 Token 则恢复会话
  useEffect(() => {
    (async () => {
      if (getAccessToken()) {
        try {
          setUser(await authApi.me());
        } catch {
          clearTokens();
        }
      }
      setInitializing(false);
    })();
  }, []);

  const login = useCallback(async (params: LoginParams) => {
    // 带上稳定的设备标识, 便于「个人设置 → 设备管理」区分多端会话
    const data = await authApi.login({ device_id: getDeviceId(), ...params });
    setTokens(data);
    setUser(await authApi.me());
  }, []);

  const register = useCallback(
    async (params: RegisterParams & { confirmPassword: string }) => {
      const { confirmPassword, ...rest } = params;
      await authApi.register(rest);
      // 注册成功后自动登录
      const data = await authApi.login({
        identity_type: rest.identity_type,
        identifier: rest.identifier,
        password: rest.password,
        device_id: getDeviceId(),
      });
      setTokens(data);
      setUser(await authApi.me());
      void confirmPassword;
    },
    [],
  );

  const logout = useCallback(async () => {
    try {
      await authApi.logout();
    } catch {
      /* Token 已失效时也继续本地清理 */
    }
    clearTokens();
    setUser(null);
  }, []);

  const value = useMemo<AuthContextValue>(
    () => ({
      user,
      initializing,
      isAdmin: !!user?.roles?.includes('admin'),
      login,
      register,
      logout,
      refreshUser,
    }),
    [user, initializing, login, register, logout, refreshUser],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
};

export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error('useAuth 必须在 <AuthProvider> 内使用');
  return ctx;
}
