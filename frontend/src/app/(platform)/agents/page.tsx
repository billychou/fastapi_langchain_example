'use client';

/**
 * 智能体管理页(设计稿 1:1): 搜索/状态筛选/新建 + 三列卡片网格。
 *
 * 卡片: 角色徽标(总管紫/执行蓝) + 名称 + ID + 状态 Tag + 两行描述 +
 * 四个引用计数 chips(本地工具/MCP远端工具/RAG检索工具/技能) + 「…」操作菜单。
 * 写操作(新建/编辑/启停/删除)按 agent:write 角色 gating, supervisor_main 禁删禁停。
 */
import {
  DeleteOutlined,
  EditOutlined,
  EllipsisOutlined,
  PauseCircleOutlined,
  PlayCircleOutlined,
  PlusOutlined,
  SearchOutlined,
} from '@ant-design/icons';
import type { MenuProps } from 'antd';
import {
  Button,
  Col,
  Dropdown,
  Empty,
  Form,
  Input,
  Modal,
  message,
  Row,
  Select,
  Space,
  Spin,
  Switch,
  Tag,
  Typography,
} from 'antd';
import { useCallback, useEffect, useMemo, useState } from 'react';
import {
  type AgentCatalog,
  type AgentDetail,
  type AgentStatus,
  type AgentSummary,
  agentApi,
} from '@/lib/api';
import { useAuth } from '@/lib/auth-context';

const { Text, Title } = Typography;

const SUPERVISOR_KEY = 'supervisor_main';

const ROLE_BADGE: Record<string, { label: string; bg: string; fg: string }> = {
  supervisor: { label: '总管', bg: '#efeafd', fg: '#5b5bd6' },
  executor: { label: '执行', bg: '#e8f1ff', fg: '#1677ff' },
};

const CHIP_STYLE: Record<string, { bg: string; fg: string }> = {
  local: { bg: '#e8f1ff', fg: '#1677ff' },
  mcp: { bg: '#f0eaff', fg: '#722ed1' },
  rag: { bg: '#fff7e6', fg: '#d48806' },
  skills: { bg: '#e6ffea', fg: '#389e0d' },
};

interface AgentFormValues {
  agent_key?: string;
  name: string;
  role: 'supervisor' | 'executor';
  description?: string;
  system_prompt?: string;
  tool_refs?: string[];
  skill_refs?: string[];
  mcp_refs?: { value: string }[];
  rag_refs?: { value: string }[];
  status?: boolean;
}

const RefList: React.FC<{
  value?: { value: string }[];
  onChange?: (value: { value: string }[]) => void;
  placeholder: string;
}> = ({ value = [], onChange, placeholder }) => (
  <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
    {value.map((item, index) => (
      // biome-ignore lint/suspicious/noArrayIndexKey: 表单行以序号标识即可
      <Space key={index} style={{ display: 'flex' }}>
        <Input
          value={item.value}
          placeholder={placeholder}
          onChange={(e) => {
            const next = value.map((row, i) =>
              i === index ? { value: e.target.value } : row,
            );
            onChange?.(next);
          }}
        />
        <Button
          danger
          type="text"
          icon={<DeleteOutlined />}
          onClick={() => onChange?.(value.filter((_, i) => i !== index))}
        />
      </Space>
    ))}
    <Button
      type="dashed"
      icon={<PlusOutlined />}
      onClick={() => onChange?.([...value, { value: '' }])}
    >
      添加引用
    </Button>
  </div>
);

const AgentsPage: React.FC = () => {
  const { user, isAdmin } = useAuth();
  const canWrite = useMemo(
    () => isAdmin || !!user?.roles?.includes('operator'),
    [isAdmin, user],
  );

  const [search, setSearch] = useState('');
  const [debouncedSearch, setDebouncedSearch] = useState('');
  const [status, setStatus] = useState<AgentStatus | undefined>();
  const [items, setItems] = useState<AgentSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [catalog, setCatalog] = useState<AgentCatalog | null>(null);
  const [modalOpen, setModalOpen] = useState(false);
  const [editing, setEditing] = useState<AgentDetail | null>(null);
  const [saving, setSaving] = useState(false);
  const [form] = Form.useForm<AgentFormValues>();

  useEffect(() => {
    const timer = setTimeout(() => setDebouncedSearch(search.trim()), 300);
    return () => clearTimeout(timer);
  }, [search]);

  const reload = useCallback(async () => {
    setLoading(true);
    try {
      const data = await agentApi.list({
        search: debouncedSearch || undefined,
        status,
      });
      setItems(data.items);
    } catch (err) {
      message.error(err instanceof Error ? err.message : '加载智能体失败');
      setItems([]);
    } finally {
      setLoading(false);
    }
  }, [debouncedSearch, status]);

  useEffect(() => {
    void reload();
  }, [reload]);

  useEffect(() => {
    agentApi
      .catalog()
      .then(setCatalog)
      .catch(() => setCatalog(null));
  }, []);

  const openCreate = () => {
    setEditing(null);
    form.resetFields();
    form.setFieldsValue({ role: 'executor', status: true });
    setModalOpen(true);
  };

  const openEdit = async (agentKey: string) => {
    try {
      const detail = await agentApi.get(agentKey);
      setEditing(detail);
      form.setFieldsValue({
        name: detail.name,
        role: detail.role,
        description: detail.description,
        system_prompt: detail.system_prompt,
        tool_refs: detail.tool_refs,
        skill_refs: detail.skill_refs,
        mcp_refs: detail.mcp_refs.map((value) => ({ value })),
        rag_refs: detail.rag_refs.map((value) => ({ value })),
        status: detail.status === 'active',
      });
      setModalOpen(true);
    } catch (err) {
      message.error(err instanceof Error ? err.message : '加载详情失败');
    }
  };

  const toggleStatus = async (agent: AgentSummary) => {
    const next: AgentStatus = agent.status === 'active' ? 'paused' : 'active';
    try {
      await agentApi.setStatus(agent.agent_key, next);
      message.success(next === 'active' ? '已启用' : '已暂停');
      await reload();
    } catch (err) {
      message.error(err instanceof Error ? err.message : '操作失败');
    }
  };

  const removeAgent = (agent: AgentSummary) => {
    Modal.confirm({
      title: `删除智能体 ${agent.name}?`,
      content: '删除后该智能体立即退出调度, 此操作不可恢复。',
      okText: '删除',
      okButtonProps: { danger: true },
      cancelText: '取消',
      onOk: async () => {
        try {
          await agentApi.remove(agent.agent_key);
          message.success('已删除');
          await reload();
        } catch (err) {
          message.error(err instanceof Error ? err.message : '删除失败');
        }
      },
    });
  };

  const submit = async () => {
    const values = await form.validateFields();
    const cleanRefs = (rows?: { value: string }[]) =>
      (rows ?? [])
        .map((row) => row.value.trim())
        .filter((row) => row.length > 0);
    const payload = {
      name: values.name.trim(),
      description: (values.description ?? '').trim(),
      system_prompt: values.system_prompt ?? '',
      tool_refs: values.tool_refs ?? [],
      skill_refs: values.skill_refs ?? [],
      mcp_refs: cleanRefs(values.mcp_refs),
      rag_refs: cleanRefs(values.rag_refs),
    };
    setSaving(true);
    try {
      if (editing) {
        await agentApi.update(editing.agent_key, payload);
        const nextStatus: AgentStatus = values.status ? 'active' : 'paused';
        if (nextStatus !== editing.status) {
          await agentApi.setStatus(editing.agent_key, nextStatus);
        }
        message.success('已保存');
      } else {
        const created = await agentApi.create({
          ...payload,
          agent_key: (values.agent_key ?? '').trim(),
          role: values.role,
        });
        if (!values.status) {
          await agentApi.setStatus(created.agent_key, 'paused');
        }
        message.success('智能体已创建');
      }
      setModalOpen(false);
      await reload();
    } catch (err) {
      message.error(err instanceof Error ? err.message : '保存失败');
    } finally {
      setSaving(false);
    }
  };

  const menuFor = (agent: AgentSummary): MenuProps['items'] => {
    const locked = agent.agent_key === SUPERVISOR_KEY;
    return [
      {
        key: 'edit',
        icon: <EditOutlined />,
        label: '编辑',
        disabled: !canWrite,
        onClick: () => void openEdit(agent.agent_key),
      },
      {
        key: 'status',
        icon:
          agent.status === 'active' ? (
            <PauseCircleOutlined />
          ) : (
            <PlayCircleOutlined />
          ),
        label: agent.status === 'active' ? '暂停' : '启用',
        disabled: !canWrite || locked,
        onClick: () => void toggleStatus(agent),
      },
      { type: 'divider' as const },
      {
        key: 'delete',
        icon: <DeleteOutlined />,
        label: '删除',
        danger: true,
        disabled: !canWrite || locked,
        onClick: () => removeAgent(agent),
      },
    ];
  };

  return (
    <div>
      <div
        style={{
          display: 'flex',
          gap: 12,
          alignItems: 'center',
          flexWrap: 'wrap',
          marginBottom: 16,
        }}
      >
        <Input
          allowClear
          prefix={<SearchOutlined style={{ color: 'rgba(0,0,0,0.35)' }} />}
          placeholder="搜索: 名称、ID"
          style={{ width: 280 }}
          value={search}
          onChange={(e) => setSearch(e.target.value)}
        />
        <Select
          allowClear
          placeholder="状态"
          style={{ width: 160 }}
          value={status}
          onChange={(value) => setStatus(value as AgentStatus | undefined)}
          options={[
            { label: '活动', value: 'active' },
            { label: '暂停', value: 'paused' },
          ]}
        />
        <div style={{ flex: 1 }} />
        {canWrite && (
          <Button type="primary" icon={<PlusOutlined />} onClick={openCreate}>
            新建智能体
          </Button>
        )}
      </div>

      <Spin spinning={loading}>
        {items.length === 0 && !loading ? (
          <div className="platform-card" style={{ padding: 48 }}>
            <Empty description="暂无匹配的智能体" />
          </div>
        ) : (
          <Row gutter={[16, 16]}>
            {items.map((agent) => {
              const badge = ROLE_BADGE[agent.role] ?? ROLE_BADGE.executor;
              return (
                <Col key={agent.agent_key} xs={24} md={12} xl={8}>
                  <div
                    className="platform-card"
                    style={{
                      padding: 20,
                      height: '100%',
                      display: 'flex',
                      flexDirection: 'column',
                      gap: 16,
                    }}
                  >
                    <div style={{ display: 'flex', gap: 12 }}>
                      <div
                        style={{
                          width: 44,
                          height: 44,
                          borderRadius: 10,
                          background: badge.bg,
                          color: badge.fg,
                          display: 'flex',
                          alignItems: 'center',
                          justifyContent: 'center',
                          fontWeight: 600,
                          flexShrink: 0,
                        }}
                      >
                        {badge.label}
                      </div>
                      <div style={{ flex: 1, minWidth: 0 }}>
                        <Title
                          level={5}
                          style={{ margin: 0, fontSize: 16, fontWeight: 600 }}
                          ellipsis={{ tooltip: agent.name }}
                        >
                          {agent.name}
                        </Title>
                        <Text
                          type="secondary"
                          style={{
                            fontSize: 12,
                            fontFamily: 'var(--font-mono)',
                          }}
                        >
                          ID: {agent.agent_key}
                        </Text>
                      </div>
                      <Tag
                        bordered={false}
                        color={agent.status === 'active' ? 'success' : 'gold'}
                        style={{ marginInlineEnd: 0, alignSelf: 'flex-start' }}
                      >
                        {agent.status === 'active' ? '活动' : '暂停'}
                      </Tag>
                    </div>

                    <div
                      style={{
                        color: 'rgba(0,0,0,0.55)',
                        fontSize: 13,
                        lineHeight: '22px',
                        minHeight: 44,
                        display: '-webkit-box',
                        WebkitLineClamp: 2,
                        WebkitBoxOrient: 'vertical',
                        overflow: 'hidden',
                      }}
                    >
                      {agent.description || '暂无描述'}
                    </div>

                    <div
                      style={{
                        marginTop: 'auto',
                        display: 'flex',
                        alignItems: 'center',
                        gap: 8,
                        flexWrap: 'wrap',
                      }}
                    >
                      {(
                        [
                          ['local', `${agent.counts.local} 本地工具`],
                          ['mcp', `${agent.counts.mcp} MCP远端工具`],
                          ['rag', `${agent.counts.rag} RAG检索工具`],
                          ['skills', `${agent.counts.skills} 技能`],
                        ] as const
                      ).map(([kind, label]) => (
                        <span
                          key={kind}
                          style={{
                            background: CHIP_STYLE[kind].bg,
                            color: CHIP_STYLE[kind].fg,
                            borderRadius: 6,
                            padding: '2px 8px',
                            fontSize: 12,
                            whiteSpace: 'nowrap',
                          }}
                        >
                          {label}
                        </span>
                      ))}
                      <div style={{ flex: 1 }} />
                      <Dropdown
                        trigger={['click']}
                        menu={{ items: menuFor(agent) }}
                      >
                        <Button type="text" icon={<EllipsisOutlined />} />
                      </Dropdown>
                    </div>
                  </div>
                </Col>
              );
            })}
          </Row>
        )}
      </Spin>

      <Modal
        open={modalOpen}
        title={editing ? `编辑智能体: ${editing.agent_key}` : '新建智能体'}
        width={640}
        okText="保存"
        cancelText="取消"
        confirmLoading={saving}
        onCancel={() => setModalOpen(false)}
        onOk={() => void submit()}
        destroyOnHidden
      >
        <Form form={form} layout="vertical" requiredMark="optional">
          <Row gutter={16}>
            <Col span={12}>
              <Form.Item
                name="name"
                label="名称"
                rules={[{ required: true, message: '请输入名称' }]}
              >
                <Input placeholder="如: K8s 集群诊断" maxLength={64} />
              </Form.Item>
            </Col>
            <Col span={12}>
              <Form.Item
                name="agent_key"
                label="ID (agent_key)"
                rules={
                  editing
                    ? []
                    : [
                        { required: true, message: '请输入 ID' },
                        {
                          pattern: /^[a-z0-9][a-z0-9_-]{0,63}$/,
                          message: '小写字母/数字开头, 仅含 a-z 0-9 _ -',
                        },
                      ]
                }
              >
                <Input
                  disabled={!!editing}
                  placeholder="如: k8s-agent"
                  style={{ fontFamily: 'var(--font-mono)' }}
                />
              </Form.Item>
            </Col>
          </Row>
          <Row gutter={16}>
            <Col span={12}>
              <Form.Item name="role" label="角色">
                <Select
                  disabled={!!editing}
                  options={[
                    { label: '总管 (supervisor)', value: 'supervisor' },
                    { label: '执行 (executor)', value: 'executor' },
                  ]}
                />
              </Form.Item>
            </Col>
            <Col span={12}>
              <Form.Item
                name="status"
                label="状态"
                valuePropName="checked"
                tooltip={
                  editing?.agent_key === SUPERVISOR_KEY
                    ? '内置总管智能体不可暂停'
                    : undefined
                }
              >
                <Switch
                  checkedChildren="活动"
                  unCheckedChildren="暂停"
                  disabled={editing?.agent_key === SUPERVISOR_KEY}
                />
              </Form.Item>
            </Col>
          </Row>
          <Form.Item name="description" label="描述">
            <Input.TextArea
              rows={2}
              maxLength={500}
              showCount
              placeholder="卡片上展示的职责说明"
            />
          </Form.Item>
          <Form.Item name="system_prompt" label="系统提示词">
            <Input.TextArea rows={3} placeholder="该智能体的 system prompt" />
          </Form.Item>
          <Row gutter={16}>
            <Col span={12}>
              <Form.Item name="tool_refs" label="本地工具">
                <Select
                  mode="multiple"
                  allowClear
                  placeholder="选择本地工具"
                  options={(catalog?.tools ?? []).map((tool) => ({
                    label: tool.name,
                    value: tool.name,
                    title: tool.description,
                  }))}
                />
              </Form.Item>
            </Col>
            <Col span={12}>
              <Form.Item name="skill_refs" label="技能">
                <Select
                  mode="multiple"
                  allowClear
                  placeholder="选择可见技能"
                  options={(catalog?.skills ?? []).map((skill) => ({
                    label: skill.name,
                    value: skill.name,
                    title: skill.description,
                  }))}
                />
              </Form.Item>
            </Col>
          </Row>
          <Form.Item
            name="mcp_refs"
            label="MCP远端工具 (即将接入)"
            tooltip="本期仅登记引用名称与计数, 真实 MCP client 下一阶段接入"
          >
            <RefList placeholder="MCP 工具名称" />
          </Form.Item>
          <Form.Item
            name="rag_refs"
            label="RAG检索工具 (即将接入)"
            tooltip="本期仅登记引用名称与计数, 向量库检索下一阶段接入"
          >
            <RefList placeholder="知识库/检索器名称" />
          </Form.Item>
        </Form>
      </Modal>
    </div>
  );
};

export default AgentsPage;
