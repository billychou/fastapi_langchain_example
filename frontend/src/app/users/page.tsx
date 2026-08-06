"use client";

/**
 * 用户管理(管理端): 账号分页列表 + 角色全量分配。
 * 服务端以 RBAC 权限点强制校验(account:read / rbac:read / rbac:assign),
 * 前端仅做体验层守卫。
 */
import { ArrowLeftOutlined, TeamOutlined } from "@ant-design/icons";
import {
  Button,
  Card,
  Checkbox,
  Flex,
  Modal,
  message,
  Result,
  Space,
  Spin,
  Table,
  Tag,
  Tooltip,
  Typography,
} from "antd";
import type { ColumnsType } from "antd/es/table";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useState } from "react";
import { type AdminAccountItem, authApi, type RoleItem } from "@/lib/api";
import { useAuth } from "@/lib/auth-context";

const { Title, Text } = Typography;

const STATUS_TAG: Record<number, { color: string; label: string }> = {
  1: { color: "green", label: "正常" },
  2: { color: "orange", label: "锁定" },
  3: { color: "default", label: "已注销" },
};

const UsersPage: React.FC = () => {
  const router = useRouter();
  const { user, initializing, isAdmin } = useAuth();

  const [loading, setLoading] = useState(false);
  const [items, setItems] = useState<AdminAccountItem[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(10);

  // 角色分配弹窗
  const [roles, setRoles] = useState<RoleItem[]>([]);
  const [editing, setEditing] = useState<AdminAccountItem | null>(null);
  const [selectedRoles, setSelectedRoles] = useState<string[]>([]);
  const [saving, setSaving] = useState(false);

  const loadAccounts = useCallback(async () => {
    setLoading(true);
    try {
      const data = await authApi.listAccounts(page, pageSize);
      setItems(data.items);
      setTotal(data.total);
    } catch (err) {
      message.error(err instanceof Error ? err.message : "加载账号列表失败");
    } finally {
      setLoading(false);
    }
  }, [page, pageSize]);

  useEffect(() => {
    if (!initializing && user && isAdmin) void loadAccounts();
  }, [initializing, user, isAdmin, loadAccounts]);

  // ---------------- 守卫 ----------------
  if (initializing) {
    return (
      <Flex align="center" justify="center" style={{ minHeight: "100vh" }}>
        <Spin size="large" />
      </Flex>
    );
  }
  if (!user) {
    router.replace("/login");
    return null;
  }
  if (!isAdmin) {
    return (
      <Result
        status="403"
        title="无权访问"
        subTitle="用户管理需要管理员角色(admin)"
        extra={
          <Link href="/">
            <Button type="primary">返回对话</Button>
          </Link>
        }
        style={{ marginTop: 80 }}
      />
    );
  }

  const openRoleModal = async (record: AdminAccountItem) => {
    setEditing(record);
    setSelectedRoles([]);
    try {
      const [roleList, current] = await Promise.all([
        roles.length ? Promise.resolve(roles) : authApi.listRoles(),
        authApi.getAccountRoles(record.account_uuid),
      ]);
      setRoles(roleList);
      setSelectedRoles(current.role_codes);
    } catch (err) {
      message.error(err instanceof Error ? err.message : "加载角色失败");
      setEditing(null);
    }
  };

  const saveRoles = async () => {
    if (!editing) return;
    if (selectedRoles.length === 0) {
      message.warning("请至少选择一个角色");
      return;
    }
    setSaving(true);
    try {
      await authApi.assignRoles(editing.account_uuid, selectedRoles);
      message.success("角色已更新");
      setEditing(null);
      void loadAccounts();
    } catch (err) {
      message.error(err instanceof Error ? err.message : "保存失败");
    } finally {
      setSaving(false);
    }
  };

  const columns: ColumnsType<AdminAccountItem> = [
    {
      title: "账号",
      dataIndex: "account_uuid",
      width: 140,
      render: (uuid: string, record) => (
        <Space direction="vertical" size={0}>
          <Text strong>{record.nickname || "—"}</Text>
          <Tooltip title={uuid}>
            <Text type="secondary" style={{ fontSize: 12 }}>
              {uuid.slice(0, 8)}…
            </Text>
          </Tooltip>
        </Space>
      ),
    },
    {
      title: "手机号",
      dataIndex: "phone_masked",
      width: 130,
      render: (v: string | null) => v || <Text type="secondary">未绑定</Text>,
    },
    {
      title: "邮箱",
      dataIndex: "email_masked",
      width: 180,
      render: (v: string | null) => v || <Text type="secondary">未绑定</Text>,
    },
    {
      title: "状态",
      dataIndex: "status",
      width: 90,
      render: (status: number) => {
        const tag = STATUS_TAG[status] || {
          color: "default",
          label: String(status),
        };
        return <Tag color={tag.color}>{tag.label}</Tag>;
      },
    },
    {
      title: "登录策略",
      dataIndex: "login_policy",
      width: 110,
      render: (policy: string) => (
        <Tag color={policy === "single_device" ? "volcano" : "blue"}>
          {policy === "single_device" ? "单端互踢" : "多端在线"}
        </Tag>
      ),
    },
    {
      title: "最近登录",
      dataIndex: "last_login_at",
      width: 180,
      render: (v: string | null) =>
        v ? v.replace("T", " ").slice(0, 19) : "—",
    },
    {
      title: "操作",
      key: "actions",
      width: 110,
      render: (_, record) => (
        <Button
          type="link"
          size="small"
          onClick={() => void openRoleModal(record)}
        >
          分配角色
        </Button>
      ),
    },
  ];

  return (
    <div style={{ minHeight: "100vh", background: "#f5f5f5", padding: 24 }}>
      <Card style={{ maxWidth: 1200, margin: "0 auto" }}>
        <Flex
          justify="space-between"
          align="center"
          style={{ marginBottom: 16 }}
        >
          <Space>
            <TeamOutlined style={{ fontSize: 20 }} />
            <Title level={4} style={{ margin: 0 }}>
              用户管理
            </Title>
          </Space>
          <Link href="/">
            <Button icon={<ArrowLeftOutlined />}>返回对话</Button>
          </Link>
        </Flex>

        <Table<AdminAccountItem>
          rowKey="account_uuid"
          columns={columns}
          dataSource={items}
          loading={loading}
          pagination={{
            current: page,
            pageSize,
            total,
            showSizeChanger: true,
            showTotal: (t) => `共 ${t} 个账号`,
            onChange: (p, ps) => {
              setPage(p);
              setPageSize(ps);
            },
          }}
        />
      </Card>

      <Modal
        title={`分配角色 — ${editing?.nickname || editing?.account_uuid?.slice(0, 8) || ""}`}
        open={!!editing}
        onCancel={() => setEditing(null)}
        onOk={() => void saveRoles()}
        confirmLoading={saving}
        okText="保存"
        cancelText="取消"
      >
        <Checkbox.Group
          style={{ display: "flex", flexDirection: "column", gap: 8 }}
          value={selectedRoles}
          onChange={(values) => setSelectedRoles(values as string[])}
          options={roles.map((r) => ({
            label: `${r.role_name} (${r.role_code})${r.description ? ` — ${r.description}` : ""}`,
            value: r.role_code,
          }))}
        />
      </Modal>
    </div>
  );
};

export default UsersPage;
