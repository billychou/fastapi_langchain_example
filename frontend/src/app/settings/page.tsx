'use client';

/**
 * 个人设置: 基本资料 / 账号安全 / 设备管理。
 * 全部为自服务接口(有 Access Token 即可), 前端仅做体验层守卫。
 */
import {
  ArrowLeftOutlined,
  LaptopOutlined,
  LockOutlined,
  SettingOutlined,
  UserOutlined,
} from '@ant-design/icons';
import {
  Avatar,
  Button,
  Card,
  Descriptions,
  Flex,
  Form,
  Input,
  Modal,
  message,
  Radio,
  Space,
  Spin,
  Table,
  Tabs,
  Tag,
  Tooltip,
  Typography,
} from 'antd';
import type { ColumnsType } from 'antd/es/table';
import Link from 'next/link';
import { useRouter } from 'next/navigation';
import { useCallback, useEffect, useState } from 'react';
import {
  ApiError,
  authApi,
  type LoginPolicy,
  type SessionInfo,
} from '@/lib/api';
import { useAuth } from '@/lib/auth-context';

const { Title, Text } = Typography;

const pad = (n: number) => String(n).padStart(2, '0');

/** 会话时间戳: 后端给的是 epoch 秒字符串, 这里同时兼容 ISO 字符串。 */
const fmtTime = (v?: string | null) => {
  if (!v) return '—';
  const digits = /^\d{9,13}$/.test(v) ? Number(v) : Number.NaN;
  const date = Number.isNaN(digits)
    ? new Date(v)
    : new Date(digits * (digits > 1e11 ? 1 : 1000));
  if (Number.isNaN(date.getTime())) return v;
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(
    date.getHours(),
  )}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
};

/** 与后端 security.assert_password_strength 保持一致的前端预校验。 */
const passwordStrengthError = (value: string): string | null => {
  if (value.length < 8 || value.length > 128)
    return '密码长度必须在 8-128 位之间';
  const classes =
    (/[a-z]/.test(value) ? 1 : 0) +
    (/[A-Z]/.test(value) ? 1 : 0) +
    (/\d/.test(value) ? 1 : 0) +
    (/[^A-Za-z0-9]/.test(value) ? 1 : 0);
  return classes < 3
    ? '密码必须至少包含大写字母、小写字母、数字、特殊字符中的三类'
    : null;
};

const SettingsPage: React.FC = () => {
  const router = useRouter();
  const { user, initializing, refreshUser, logout } = useAuth();

  // ---------------- 基本资料 ----------------
  const [profileForm] = Form.useForm<{
    nickname: string;
    avatar_url?: string;
  }>();
  const watchedAvatar = Form.useWatch('avatar_url', profileForm);
  const [savingProfile, setSavingProfile] = useState(false);

  // ---------------- 账号安全 ----------------
  const [pwdForm] = Form.useForm<{
    old_password: string;
    new_password: string;
    confirm_password: string;
  }>();
  const [changingPwd, setChangingPwd] = useState(false);
  const [policy, setPolicy] = useState<LoginPolicy>('multi_device');
  const [savingPolicy, setSavingPolicy] = useState(false);

  // ---------------- 设备管理 ----------------
  const [sessions, setSessions] = useState<SessionInfo[]>([]);
  const [loadingSessions, setLoadingSessions] = useState(false);

  const loadSessions = useCallback(async () => {
    setLoadingSessions(true);
    try {
      setSessions(await authApi.listSessions());
    } catch (err) {
      message.error(err instanceof ApiError ? err.message : '加载会话列表失败');
    } finally {
      setLoadingSessions(false);
    }
  }, []);

  // 用户加载完成后回填表单
  useEffect(() => {
    if (!user) return;
    profileForm.setFieldsValue({
      nickname: user.nickname ?? '',
      avatar_url: user.avatar_url ?? '',
    });
    setPolicy(
      user.login_policy === 'single_device' ? 'single_device' : 'multi_device',
    );
    void loadSessions();
  }, [user, profileForm, loadSessions]);

  // ---------------- 守卫 ----------------
  if (initializing) {
    return (
      <Flex align="center" justify="center" style={{ minHeight: '100vh' }}>
        <Spin size="large" />
      </Flex>
    );
  }
  if (!user) {
    router.replace('/login');
    return null;
  }

  const saveProfile = async () => {
    const values = await profileForm.validateFields();
    setSavingProfile(true);
    try {
      await authApi.updateProfile({
        nickname: values.nickname,
        avatar_url: values.avatar_url ?? '',
      });
      await refreshUser();
      message.success('资料已更新');
    } catch (err) {
      message.error(err instanceof ApiError ? err.message : '保存失败');
    } finally {
      setSavingProfile(false);
    }
  };

  const changePassword = async () => {
    const values = await pwdForm.validateFields();
    setChangingPwd(true);
    try {
      await authApi.changePassword(values.old_password, values.new_password);
      message.success('密码已修改，请重新登录');
      // 后端已全端下线; 走 logout() 以同时清空本地 Token 与 context 里的 user,
      // 否则 /login 会因 user 仍存在而被重定向回首页。
      await logout();
      router.replace('/login');
    } catch (err) {
      message.error(err instanceof ApiError ? err.message : '修改密码失败');
    } finally {
      setChangingPwd(false);
    }
  };

  const applyPolicy = async (next: LoginPolicy) => {
    setSavingPolicy(true);
    try {
      const data = await authApi.updateLoginPolicy(next);
      setPolicy(next);
      message.success(
        data.kicked_sessions > 0
          ? `登录策略已更新，并下线 ${data.kicked_sessions} 个其他会话`
          : '登录策略已更新',
      );
      void loadSessions();
    } catch (err) {
      message.error(err instanceof ApiError ? err.message : '保存失败');
    } finally {
      setSavingPolicy(false);
    }
  };

  const savePolicy = (next: LoginPolicy) => {
    if (next === policy) return;
    if (next === 'single_device') {
      Modal.confirm({
        title: '切换为单端互踢？',
        content: '保存后将立即下线除当前浏览器外的其他设备会话。',
        okText: '确认切换',
        cancelText: '取消',
        onOk: () => applyPolicy(next),
      });
      return;
    }
    void applyPolicy(next);
  };

  const kickSession = (record: SessionInfo) => {
    Modal.confirm({
      title: '下线该会话？',
      content: '被下线的设备下次请求时将被要求重新登录。',
      okText: '下线',
      okButtonProps: { danger: true },
      cancelText: '取消',
      onOk: async () => {
        try {
          await authApi.kickSession(record.session_id);
          message.success('该会话已下线');
          void loadSessions();
        } catch (err) {
          message.error(err instanceof ApiError ? err.message : '下线失败');
        }
      },
    });
  };

  const sessionColumns: ColumnsType<SessionInfo> = [
    {
      title: '设备',
      dataIndex: 'device_id',
      render: (_: string | null, record) => (
        <Space direction="vertical" size={0}>
          <Text strong>{record.device_id || '未知设备'}</Text>
          <Tooltip title={record.user_agent}>
            <Text type="secondary" style={{ fontSize: 12 }}>
              {(record.user_agent || '—').slice(0, 48)}
            </Text>
          </Tooltip>
        </Space>
      ),
    },
    {
      title: 'IP',
      dataIndex: 'ip',
      width: 140,
      render: (v: string | null) => v || '—',
    },
    {
      title: '最近活跃',
      dataIndex: 'last_seen',
      width: 180,
      render: (v: string | null) => fmtTime(v),
    },
    {
      title: '登录时间',
      dataIndex: 'created_at',
      width: 180,
      render: (v: string | null) => fmtTime(v),
    },
    {
      title: '状态',
      dataIndex: 'current',
      width: 90,
      render: (current: boolean) =>
        current ? <Tag color="green">当前</Tag> : <Tag>其他</Tag>,
    },
    {
      title: '操作',
      key: 'actions',
      width: 100,
      render: (_, record) =>
        record.current ? (
          <Tooltip title="当前会话请使用退出登录">
            <Button type="link" size="small" disabled>
              下线
            </Button>
          </Tooltip>
        ) : (
          <Button
            type="link"
            size="small"
            danger
            onClick={() => kickSession(record)}
          >
            下线
          </Button>
        ),
    },
  ];

  const profileTab = (
    <Space direction="vertical" size={24} style={{ width: '100%' }}>
      <Form form={profileForm} layout="vertical" style={{ maxWidth: 480 }}>
        <Form.Item
          name="nickname"
          label="昵称"
          rules={[{ required: true, whitespace: true, message: '请输入昵称' }]}
        >
          <Input maxLength={64} showCount placeholder="最多 64 个字符" />
        </Form.Item>
        <Form.Item
          name="avatar_url"
          label="头像地址"
          extra="留空表示清除头像；仅支持 http(s) 链接"
        >
          <Input placeholder="https://example.com/avatar.png" allowClear />
        </Form.Item>
        <Form.Item label="头像预览">
          <Space>
            <Avatar
              size={48}
              src={watchedAvatar || undefined}
              icon={<UserOutlined />}
            />
            <Text type="secondary">保存后侧栏头像同步更新</Text>
          </Space>
        </Form.Item>
        <Form.Item>
          <Button
            type="primary"
            loading={savingProfile}
            onClick={() => void saveProfile()}
          >
            保存资料
          </Button>
        </Form.Item>
      </Form>

      <Descriptions
        title="账号信息"
        column={1}
        size="small"
        items={[
          {
            key: 'uuid',
            label: '账号 ID',
            children: <Text copyable>{user.account_uuid}</Text>,
          },
          {
            key: 'phone',
            label: '手机号',
            children: user.phone_masked || <Text type="secondary">未绑定</Text>,
          },
          {
            key: 'email',
            label: '邮箱',
            children: user.email_masked || <Text type="secondary">未绑定</Text>,
          },
          {
            key: 'roles',
            label: '角色',
            children: user.roles?.length ? (
              <Space size={4}>
                {user.roles.map((r) => (
                  <Tag key={r} color={r === 'admin' ? 'gold' : 'blue'}>
                    {r}
                  </Tag>
                ))}
              </Space>
            ) : (
              <Text type="secondary">—</Text>
            ),
          },
        ]}
      />
    </Space>
  );

  const securityTab = (
    <Space direction="vertical" size={32} style={{ width: '100%' }}>
      <div>
        <Title level={5}>
          <LockOutlined /> 修改密码
        </Title>
        <Form form={pwdForm} layout="vertical" style={{ maxWidth: 480 }}>
          <Form.Item
            name="old_password"
            label="当前密码"
            rules={[{ required: true, message: '请输入当前密码' }]}
          >
            <Input.Password autoComplete="current-password" />
          </Form.Item>
          <Form.Item
            name="new_password"
            label="新密码"
            rules={[
              { required: true, message: '请输入新密码' },
              {
                validator: (_, value: string) => {
                  if (!value) return Promise.resolve();
                  const reason = passwordStrengthError(value);
                  return reason
                    ? Promise.reject(new Error(reason))
                    : Promise.resolve();
                },
              },
            ]}
            extra="8–128 位，且包含大写字母、小写字母、数字、特殊字符中的三类"
          >
            <Input.Password autoComplete="new-password" />
          </Form.Item>
          <Form.Item
            name="confirm_password"
            label="确认新密码"
            dependencies={['new_password']}
            rules={[
              { required: true, message: '请再次输入新密码' },
              ({ getFieldValue }) => ({
                validator: (_, value: string) =>
                  !value || value === getFieldValue('new_password')
                    ? Promise.resolve()
                    : Promise.reject(new Error('两次输入的密码不一致')),
              }),
            ]}
          >
            <Input.Password autoComplete="new-password" />
          </Form.Item>
          <Form.Item>
            <Button
              type="primary"
              danger
              loading={changingPwd}
              onClick={() => void changePassword()}
            >
              修改密码
            </Button>
            <Text type="secondary" style={{ marginInlineStart: 12 }}>
              修改成功后所有设备需重新登录
            </Text>
          </Form.Item>
        </Form>
      </div>

      <div>
        <Title level={5}>
          <LaptopOutlined /> 登录策略
        </Title>
        <Space direction="vertical" size={12}>
          <Radio.Group
            value={policy}
            disabled={savingPolicy}
            onChange={(e) => savePolicy(e.target.value as LoginPolicy)}
            options={[
              { label: '多端在线（手机/电脑同时登录）', value: 'multi_device' },
              { label: '单端互踢（新登录顶掉旧设备）', value: 'single_device' },
            ]}
          />
          <Text type="secondary">
            切换为单端互踢时，除当前浏览器外的其他会话会立即下线。
          </Text>
        </Space>
      </div>
    </Space>
  );

  const deviceTab = (
    <Space direction="vertical" size={12} style={{ width: '100%' }}>
      <Flex justify="flex-end">
        <Button onClick={() => void loadSessions()} loading={loadingSessions}>
          刷新
        </Button>
      </Flex>
      <Table<SessionInfo>
        rowKey="session_id"
        columns={sessionColumns}
        dataSource={sessions}
        loading={loadingSessions}
        pagination={false}
        locale={{ emptyText: '暂无在线会话' }}
      />
    </Space>
  );

  return (
    <div style={{ minHeight: '100vh', background: '#f5f5f5', padding: 24 }}>
      <Card style={{ maxWidth: 960, margin: '0 auto' }}>
        <Flex
          justify="space-between"
          align="center"
          style={{ marginBottom: 16 }}
        >
          <Space>
            <SettingOutlined style={{ fontSize: 20 }} />
            <Title level={4} style={{ margin: 0 }}>
              个人设置
            </Title>
          </Space>
          <Link href="/">
            <Button icon={<ArrowLeftOutlined />}>返回对话</Button>
          </Link>
        </Flex>

        <Tabs
          items={[
            { key: 'profile', label: '基本资料', children: profileTab },
            { key: 'security', label: '账号安全', children: securityTab },
            { key: 'devices', label: '设备管理', children: deviceTab },
          ]}
        />
      </Card>
    </div>
  );
};

export default SettingsPage;
