"use client";

import { LockOutlined, SmileOutlined, UserOutlined } from "@ant-design/icons";
import {
  Button,
  Card,
  Form,
  Input,
  message,
  Segmented,
  Typography,
} from "antd";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { useAuth } from "@/lib/auth-context";

const { Title, Text } = Typography;

interface RegisterForm {
  identifier: string;
  password: string;
  confirmPassword: string;
  nickname?: string;
}

const RegisterPage: React.FC = () => {
  const router = useRouter();
  const { user, initializing, register } = useAuth();
  const [submitting, setSubmitting] = useState(false);
  const [identityType, setIdentityType] = useState<"phone" | "email">("phone");

  useEffect(() => {
    if (!initializing && user) router.replace("/");
  }, [initializing, user, router]);

  const onFinish = async (values: RegisterForm) => {
    setSubmitting(true);
    try {
      await register({ ...values, identity_type: identityType });
      message.success("注册成功, 已自动登录");
      router.replace("/");
    } catch (err) {
      message.error(err instanceof Error ? err.message : "注册失败");
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div
      style={{
        minHeight: "100vh",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        background: "linear-gradient(135deg, #f0f5ff 0%, #e6fffb 100%)",
      }}
    >
      <Card style={{ width: 420, boxShadow: "0 8px 24px rgba(0,0,0,0.08)" }}>
        <Title level={3} style={{ textAlign: "center", marginBottom: 24 }}>
          创建账号
        </Title>

        <Form<RegisterForm> layout="vertical" onFinish={onFinish}>
          <Form.Item label="注册方式">
            <Segmented
              block
              value={identityType}
              onChange={(v) => setIdentityType(v as "phone" | "email")}
              options={[
                { label: "手机号", value: "phone" },
                { label: "邮箱", value: "email" },
              ]}
            />
          </Form.Item>

          <Form.Item
            name="identifier"
            label={identityType === "phone" ? "手机号" : "邮箱"}
            rules={[
              { required: true, message: "请输入账号" },
              identityType === "phone"
                ? { pattern: /^1[3-9]\d{9}$/, message: "手机号格式不正确" }
                : { type: "email", message: "邮箱格式不正确" },
            ]}
          >
            <Input
              size="large"
              prefix={<UserOutlined />}
              placeholder={
                identityType === "phone" ? "请输入手机号" : "请输入邮箱"
              }
              autoComplete="username"
            />
          </Form.Item>

          <Form.Item name="nickname" label="昵称">
            <Input
              size="large"
              prefix={<SmileOutlined />}
              placeholder="选填, 默认随机生成"
            />
          </Form.Item>

          <Form.Item
            name="password"
            label="密码"
            rules={[
              { required: true, message: "请输入密码" },
              { min: 8, message: "密码至少 8 位" },
              {
                validator: (_, value) => {
                  if (!value) return Promise.resolve();
                  const classes = [
                    /[a-z]/,
                    /[A-Z]/,
                    /\d/,
                    /[^a-zA-Z0-9]/,
                  ].filter((r) => r.test(value)).length;
                  return classes >= 3
                    ? Promise.resolve()
                    : Promise.reject(
                        new Error(
                          "需包含大小写字母、数字、特殊字符中的至少三类",
                        ),
                      );
                },
              },
            ]}
          >
            <Input.Password
              size="large"
              prefix={<LockOutlined />}
              placeholder="至少 8 位, 含大小写字母/数字/符号中三类"
              autoComplete="new-password"
            />
          </Form.Item>

          <Form.Item
            name="confirmPassword"
            label="确认密码"
            dependencies={["password"]}
            rules={[
              { required: true, message: "请再次输入密码" },
              ({ getFieldValue }) => ({
                validator(_, value) {
                  return !value || getFieldValue("password") === value
                    ? Promise.resolve()
                    : Promise.reject(new Error("两次输入的密码不一致"));
                },
              }),
            ]}
          >
            <Input.Password
              size="large"
              prefix={<LockOutlined />}
              placeholder="请再次输入密码"
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
              注 册
            </Button>
          </Form.Item>

          <div style={{ textAlign: "center" }}>
            <Text type="secondary">已有账号？</Text>
            <Link href="/login" style={{ marginLeft: 4 }}>
              去登录
            </Link>
          </div>
        </Form>
      </Card>
    </div>
  );
};

export default RegisterPage;
