'use client';

/**
 * 平台外壳: 左侧固定侧边栏(七大模块) + 右上账号菜单。
 *
 * 菜单按登录态与角色 gating: 用户管理仅 admin, 模型/凭证配置仅 admin+operator;
 * 匿名(演示模式)也能进外壳浏览, 账号菜单换成登录/注册入口。
 * 视觉对齐设计稿: 浅灰蓝内容底 + 白侧边栏 + 靛蓝品牌色。
 */
import {
  ApiOutlined,
  AppstoreOutlined,
  BookOutlined,
  ClockCircleOutlined,
  ClusterOutlined,
  ControlOutlined,
  DashboardOutlined,
  KeyOutlined,
  LoginOutlined,
  LogoutOutlined,
  MessageOutlined,
  MonitorOutlined,
  ProfileOutlined,
  SettingOutlined,
  TeamOutlined,
  ToolOutlined,
  UnorderedListOutlined,
} from '@ant-design/icons';
import type { MenuProps } from 'antd';
import { Avatar, Dropdown, Layout, Menu, Space, Typography } from 'antd';
import Link from 'next/link';
import { usePathname, useRouter } from 'next/navigation';
import { useEffect, useMemo, useState } from 'react';
import { useAuth } from '@/lib/auth-context';

const { Sider, Header, Content } = Layout;
const { Title } = Typography;

const BRAND_COLOR = '#5b5bd6';

/** 路由 → 侧边栏分组 key, 用于自动展开对应子菜单。 */
const groupOf = (path: string): string | null => {
  if (path.startsWith('/assets')) return 'assets';
  if (path.startsWith('/tasks')) return 'tasks';
  if (path.startsWith('/settings')) return 'settings';
  return null;
};

const PlatformLayout: React.FC<React.PropsWithChildren> = ({ children }) => {
  const router = useRouter();
  const pathname = usePathname();
  const { user, isAdmin, logout } = useAuth();
  const isManager = useMemo(
    () => !!user?.roles?.includes('operator') || isAdmin,
    [user, isAdmin],
  );

  const menuItems = useMemo<MenuProps['items']>(() => {
    const settingsChildren = [
      ...(isAdmin
        ? [
            {
              key: '/settings/users',
              icon: <TeamOutlined />,
              label: '用户管理',
            },
          ]
        : []),
      ...(isManager
        ? [
            {
              key: '/settings/models',
              icon: <ControlOutlined />,
              label: '模型配置',
            },
            {
              key: '/settings/credentials',
              icon: <KeyOutlined />,
              label: '凭证管理',
            },
          ]
        : []),
    ];
    return [
      { key: '/dashboard', icon: <DashboardOutlined />, label: '仪表盘' },
      { key: '/chat', icon: <MessageOutlined />, label: 'AI对话助手' },
      { key: '/agents', icon: <ClusterOutlined />, label: '智能体' },
      {
        key: 'assets',
        icon: <AppstoreOutlined />,
        label: '资产中心',
        children: [
          { key: '/assets/tools', icon: <ToolOutlined />, label: '本地工具' },
          { key: '/assets/mcp', icon: <ApiOutlined />, label: 'MCP远端工具' },
          { key: '/assets/knowledge', icon: <BookOutlined />, label: '知识库' },
          { key: '/assets/skills', icon: <BookOutlined />, label: '技能' },
        ],
      },
      {
        key: 'tasks',
        icon: <ProfileOutlined />,
        label: '任务中心',
        children: [
          {
            key: '/tasks',
            icon: <UnorderedListOutlined />,
            label: '任务列表',
          },
          {
            key: '/tasks/scheduled',
            icon: <ClockCircleOutlined />,
            label: '定时任务',
          },
        ],
      },
      { key: '/observability', icon: <MonitorOutlined />, label: '可观测中心' },
      ...(settingsChildren.length
        ? [
            {
              key: 'settings',
              icon: <SettingOutlined />,
              label: '配置中心',
              children: settingsChildren,
            },
          ]
        : []),
    ];
  }, [isAdmin, isManager]);

  const [openKeys, setOpenKeys] = useState<string[]>(() => {
    const group = groupOf(pathname);
    return group ? [group] : [];
  });
  useEffect(() => {
    const group = groupOf(pathname);
    if (group)
      setOpenKeys((keys) => (keys.includes(group) ? keys : [...keys, group]));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pathname]);

  const accountMenu: MenuProps['items'] = useMemo(
    () =>
      user
        ? [
            {
              key: 'profile',
              icon: <SettingOutlined />,
              label: <Link href="/settings/profile">个人设置</Link>,
            },
            ...(isAdmin
              ? [
                  {
                    key: 'users',
                    icon: <TeamOutlined />,
                    label: <Link href="/settings/users">用户管理</Link>,
                  },
                ]
              : []),
            { type: 'divider' as const },
            {
              key: 'logout',
              icon: <LogoutOutlined />,
              label: '退出登录',
              danger: true,
            },
          ]
        : [
            {
              key: 'login',
              icon: <LoginOutlined />,
              label: <Link href="/login">登录</Link>,
            },
            {
              key: 'register',
              icon: <LoginOutlined />,
              label: <Link href="/register">注册</Link>,
            },
          ],
    [user, isAdmin],
  );

  return (
    <Layout style={{ minHeight: '100vh', background: 'var(--platform-bg)' }}>
      <Sider
        width={232}
        theme="light"
        style={{
          background: '#fbfbfe',
          borderRight: '1px solid #ececf3',
          position: 'sticky',
          top: 0,
          height: '100vh',
          overflow: 'auto',
        }}
      >
        <div style={{ padding: '20px 20px 12px' }}>
          <Title
            level={4}
            style={{ color: BRAND_COLOR, margin: 0, fontWeight: 700 }}
          >
            AI运维智能平台
          </Title>
        </div>
        <Menu
          mode="inline"
          selectedKeys={[pathname]}
          openKeys={openKeys}
          onOpenChange={(keys) => setOpenKeys(keys as string[])}
          items={menuItems}
          onClick={({ key }) => {
            if (key.startsWith('/')) router.push(key);
          }}
          style={{ borderInlineEnd: 'none', background: 'transparent' }}
        />
      </Sider>
      <Layout style={{ background: 'transparent' }}>
        <Header
          style={{
            background: 'transparent',
            padding: '0 24px',
            height: 64,
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'flex-end',
          }}
        >
          <Dropdown
            trigger={['click']}
            menu={{
              items: accountMenu,
              onClick: ({ key }) => {
                if (key === 'logout')
                  void logout().then(() => router.replace('/login'));
              },
            }}
          >
            <Space style={{ cursor: 'pointer' }}>
              <Avatar
                style={{ background: BRAND_COLOR }}
                src={user?.avatar_url || undefined}
              >
                {user?.nickname?.slice(0, 1) || '游'}
              </Avatar>
            </Space>
          </Dropdown>
        </Header>
        <Content style={{ padding: '0 24px 24px' }}>{children}</Content>
      </Layout>
    </Layout>
  );
};

export default PlatformLayout;
