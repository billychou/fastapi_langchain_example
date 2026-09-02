'use client';

import { LockOutlined, UserOutlined } from '@ant-design/icons';
import {
  Button,
  Card,
  Form,
  Input,
  message,
  Segmented,
  Typography,
} from 'antd';
import Link from 'next/link';
import { useRouter } from 'next/navigation';
import { useEffect, useState } from 'react';
import { useAuth } from '@/lib/auth-context';

const { Title, Text } = Typography;

interface LoginForm {
  identity_type: 'phone' | 'email';
  identifier: string;
  password: string;
}

const LoginPage: React.FC = () => {
  const router = useRouter();
  const { user, initializing, login } = useAuth();
  const [submitting, setSubmitting] = useState(false);
  const [identityType, setIdentityType] = useState<'phone' | 'email'>('phone');

  // 已登录则直接回首页
  useEffect(() => {
    if (!initializing && user) router.replace('/');
  }, [initializing, user, router]);

  const onFinish = async (values: LoginForm) => {
    setSubmitting(true);
    try {
      await login({ ...values, identity_type: identityType });
      message.success('登录成功');
      router.replace('/');
    } catch (err) {
      message.error(err instanceof Error ? err.message : '登录失败');
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div
      style={{
        minHeight: '100vh',
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        background: 'linear-gradient(135deg, #f0f5ff 0%, #e6fffb 100%)',
      }}
    >
      <Card style={{ width: 400, boxShadow: '0 8px 24px rgba(0,0,0,0.08)' }}>
        <Title level={3} style={{ textAlign: 'center', marginBottom: 4 }}>
          欢迎回来
        </Title>
        <Text
          type="secondary"
          style={{ display: 'block', textAlign: 'center', marginBottom: 24 }}
        >
          登录后即可使用 AI 助手对话
        </Text>

        <Form<LoginForm>
          layout="vertical"
          onFinish={onFinish}
          initialValues={{ identity_type: 'phone' }}
        >
          <Form.Item label="登录方式">
            <Segmented
              block
              value={identityType}
              onChange={(v) => setIdentityType(v as 'phone' | 'email')}
              options={[
                { label: '手机号', value: 'phone' },
                { label: '邮箱', value: 'email' },
              ]}
            />
          </Form.Item>

          <Form.Item
            name="identifier"
            label={identityType === 'phone' ? '手机号' : '邮箱'}
            rules={[
              { required: true, message: '请输入账号' },
              identityType === 'email'
                ? { type: 'email', message: '邮箱格式不正确' }
                : {},
            ]}
          >
            <Input
              size="large"
              prefix={<UserOutlined />}
              placeholder={
                identityType === 'phone' ? '请输入手机号' : '请输入邮箱'
              }
              autoComplete="username"
            />
          </Form.Item>

          <Form.Item
            name="password"
            label="密码"
            rules={[{ required: true, message: '请输入密码' }]}
          >
            <Input.Password
              size="large"
              prefix={<LockOutlined />}
              placeholder="请输入密码"
              autoComplete="current-password"
            />
          </Form.Item>

          <Form.Item>
            <Button
              type="primary"
              htmlType="submit"
              size="large"
              block
              loading={submitting}
            >
              登 录
            </Button>
          </Form.Item>

          <div style={{ textAlign: 'center' }}>
            <Text type="secondary">还没有账号？</Text>
            <Link href="/register" style={{ marginLeft: 4 }}>
              立即注册
            </Link>
          </div>
        </Form>
      </Card>
    </div>
  );
};

export default LoginPage;
