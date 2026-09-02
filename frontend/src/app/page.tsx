'use client';

import {
  CloudUploadOutlined,
  DeleteOutlined,
  EditOutlined,
  EllipsisOutlined,
  GlobalOutlined,
  LogoutOutlined,
  PaperClipOutlined,
  QuestionCircleOutlined,
  ShareAltOutlined,
  SyncOutlined,
  TeamOutlined,
} from '@ant-design/icons';
import type { BubbleListProps, ThoughtChainItemProps } from '@ant-design/x';
import {
  Actions,
  Attachments,
  Bubble,
  Conversations,
  Prompts,
  Sender,
  Think,
  ThoughtChain,
  Welcome,
  XProvider,
} from '@ant-design/x';
import type { ComponentProps } from '@ant-design/x-markdown';
import XMarkdown from '@ant-design/x-markdown';
import type { DefaultMessageInfo } from '@ant-design/x-sdk';
import {
  DeepSeekChatProvider,
  type SSEFields,
  useXChat,
  useXConversations,
  type XModelParams,
  type XModelResponse,
  XRequest,
} from '@ant-design/x-sdk';
import {
  Avatar,
  Button,
  Dropdown,
  Flex,
  type GetProp,
  Input,
  Modal,
  message,
  Pagination,
  Space,
  Spin,
} from 'antd';
import Image from 'next/image';
import Link from 'next/link';
import { useRouter } from 'next/navigation';
import React, { useCallback, useEffect, useRef, useState } from 'react';
import '@ant-design/x-markdown/themes/light.css';
import '@ant-design/x-markdown/themes/dark.css';
import type { BubbleListRef } from '@ant-design/x/es/bubble';
import {
  API_BASE,
  ApiError,
  getValidAccessToken,
  type ThreadItem,
  threadApi,
} from '@/lib/api';
import { useAuth } from '@/lib/auth-context';
import { useMarkdownTheme } from '@/x-markdown/demo/_utils';
import {
  type ChatMessage,
  DESIGN_GUIDE,
  HOT_TOPICS,
  SENDER_PROMPTS,
  THOUGHT_CHAIN_CONFIG,
} from './_utils/config';
import locale from './_utils/local';
import { useStyle } from './_utils/styles';

// 后端 agent_threads.title 的默认值(首轮乐观更新标题与其保持一致)
const DEFAULT_THREAD_TITLE = '新会话';

// ==================== Context ====================
const ChatContext = React.createContext<{
  onReload?: ReturnType<typeof useXChat>['onReload'];
  setMessage?: ReturnType<typeof useXChat<ChatMessage>>['setMessage'];
}>({});

// ==================== Sub Component ====================

const ThinkComponent = React.memo((props: ComponentProps) => {
  const [title, setTitle] = React.useState(`${locale.deepThinking}...`);
  const [loading, setLoading] = React.useState(true);

  React.useEffect(() => {
    if (props.streamStatus === 'done') {
      setTitle(locale.completeThinking);
      setLoading(false);
    }
  }, [props.streamStatus]);

  return (
    <Think title={title} loading={loading}>
      {props.children}
    </Think>
  );
});

const Footer: React.FC<{
  id?: string | number;
  content: string;
  status?: string;
  extraInfo?: ChatMessage['extraInfo'];
}> = ({ id, content, extraInfo, status }) => {
  const context = React.useContext(ChatContext);
  const Items = [
    {
      key: 'pagination',
      actionRender: <Pagination simple total={1} pageSize={1} />,
    },
    {
      key: 'retry',
      label: locale.retry,
      icon: <SyncOutlined />,
      onItemClick: () => {
        if (id) {
          context?.onReload?.(id, {
            userAction: 'retry',
          });
        }
      },
    },
    {
      key: 'copy',
      actionRender: <Actions.Copy text={content} />,
    },
    {
      key: 'audio',
      actionRender: (
        <Actions.Audio
          onClick={() => {
            message.info(locale.isMock);
          }}
        />
      ),
    },
    {
      key: 'feedback',
      actionRender: (
        <Actions.Feedback
          styles={{
            liked: {
              color: '#f759ab',
            },
          }}
          value={extraInfo?.feedback || 'default'}
          key="feedback"
          onChange={(val) => {
            if (id) {
              context?.setMessage?.(id, () => ({
                extraInfo: {
                  feedback: val,
                },
              }));
              message.success(`${id}: ${val}`);
            } else {
              message.error('has no id!');
            }
          }}
        />
      ),
    },
  ];
  return status !== 'updating' && status !== 'loading' ? (
    <div style={{ display: 'flex' }}>{id && <Actions items={Items} />}</div>
  ) : null;
};

// ==================== Chat Provider ====================
/**
 * 🔔 Please replace the BASE_URL, MODEL with your own values.
 */
const providerCaches = new Map<string, DeepSeekChatProvider>();
const providerFactory = (conversationKey: string) => {
  if (!providerCaches.get(conversationKey)) {
    providerCaches.set(
      conversationKey,
      new DeepSeekChatProvider({
        request: XRequest<
          XModelParams,
          Partial<Record<SSEFields, XModelResponse>>
        >(`${API_BASE}/api/chat`, {
          manual: true,
          params: {},
          // 每次发起对话前注入最新 Access Token(临近过期自动无感续期);
          // body.messages 只保留最后一条(多轮记忆由后端 checkpointer 维护, 避免重复累积),
          // 并强制注入 conversation_id(对应 agent_threads.thread_id)。
          middlewares: {
            onRequest: async (url, options) => {
              const token = await getValidAccessToken();
              const headers: Record<string, string> = {
                ...(options.headers || {}),
              };
              if (token) headers.Authorization = `Bearer ${token}`;
              let body = options.body;
              try {
                const parsed = JSON.parse(String(options.body));
                if (
                  Array.isArray(parsed?.messages) &&
                  parsed.messages.length > 1
                ) {
                  parsed.messages = [
                    parsed.messages[parsed.messages.length - 1],
                  ];
                }
                parsed.conversation_id = conversationKey;
                body = JSON.stringify(parsed);
              } catch {
                /* body 非 JSON 时保持原样 */
              }
              return [url, { ...options, headers, body }];
            },
          },
        }),
      }),
    );
  }
  return providerCaches.get(conversationKey);
};

// 切换会话时从后端恢复历史消息(LangGraph checkpoint; 服务重启后为空)
const fetchThreadHistory = async (info?: {
  conversationKey?: string | number;
}): Promise<DefaultMessageInfo<ChatMessage>[]> => {
  const key = info?.conversationKey;
  if (typeof key !== 'string' || !key) return [];
  try {
    const msgs = await threadApi.messages(key);
    return msgs.map((m, idx) => ({
      id: `history-${idx}`,
      message: { role: m.role, content: m.content },
      status: 'success' as const,
    }));
  } catch {
    return [];
  }
};

const getRole = (className: string): BubbleListProps['role'] => ({
  assistant: {
    placement: 'start',
    header: (_, { status }) => {
      const config =
        THOUGHT_CHAIN_CONFIG[status as keyof typeof THOUGHT_CHAIN_CONFIG];
      return config ? (
        <ThoughtChain.Item
          style={{
            marginBottom: 8,
          }}
          status={config.status as ThoughtChainItemProps['status']}
          variant="solid"
          icon={<GlobalOutlined />}
          title={config.title}
        />
      ) : null;
    },
    footer: (content, { status, key, extraInfo }) => (
      <Footer
        content={content}
        status={status}
        extraInfo={extraInfo as ChatMessage['extraInfo']}
        id={key as string}
      />
    ),
    contentRender: (content: React.ReactNode, { status }) => {
      const text = typeof content === 'string' ? content : '';
      const newContent = text.replace(/\n\n/g, '<br/><br/>');
      return (
        <XMarkdown
          paragraphTag="div"
          components={{
            think: ThinkComponent,
          }}
          className={className}
          streaming={{
            hasNextChunk: status === 'updating',
            enableAnimation: true,
          }}
        >
          {newContent}
        </XMarkdown>
      );
    },
  },
  user: { placement: 'end' },
});

const Independent: React.FC = () => {
  const { styles } = useStyle();
  // ==================== State ====================

  const {
    conversations,
    activeConversationKey,
    setActiveConversationKey,
    addConversation,
    setConversations,
  } = useXConversations({
    defaultConversations: [],
  });

  const [className] = useMarkdownTheme();
  const [messageApi, contextHolder] = message.useMessage();
  const [attachmentsOpen, setAttachmentsOpen] = useState(false);
  const [attachedFiles, setAttachedFiles] = useState<
    GetProp<typeof Attachments, 'items'>
  >([]);

  const [inputValue, setInputValue] = useState('');

  // 会话元数据(agent_threads)相关状态
  const [threadsReady, setThreadsReady] = useState(false);
  const [renameTarget, setRenameTarget] = useState<{
    key: string;
    label: string;
  } | null>(null);
  const [renameValue, setRenameValue] = useState('');

  const listRef = useRef<BubbleListRef>(null);

  // ==================== Runtime ====================

  const {
    onRequest,
    messages,
    isRequesting,
    abort,
    onReload,
    setMessage,
    isDefaultMessagesRequesting,
  } = useXChat<ChatMessage>({
    // every conversation has its own provider
    provider: activeConversationKey
      ? providerFactory(activeConversationKey)
      : undefined,
    conversationKey: activeConversationKey,
    defaultMessages: fetchThreadHistory,
    requestPlaceholder: () => {
      return {
        content: locale.noData,
        role: 'assistant',
      };
    },
    requestFallback: (_, { error, errorInfo, messageInfo }) => {
      if (error.name === 'AbortError') {
        return {
          content: messageInfo?.message?.content || locale.requestAborted,
          role: 'assistant',
        };
      }
      return {
        content: errorInfo?.error?.message || locale.requestFailed,
        role: 'assistant',
      };
    },
  });

  // ==================== Auth ====================
  const router = useRouter();
  const { user, initializing, isAdmin, logout } = useAuth();

  useEffect(() => {
    if (!initializing && !user) router.replace('/login');
  }, [initializing, user, router]);

  // ==================== Threads (agent_threads 元数据) ====================
  const loadThreads = useCallback(async (): Promise<ThreadItem[]> => {
    const items = await threadApi.list();
    setConversations(items.map((t) => ({ key: t.thread_id, label: t.title })));
    return items;
  }, [setConversations]);

  const createNewConversation = useCallback(async () => {
    try {
      const thread = await threadApi.create();
      addConversation({ key: thread.thread_id, label: thread.title });
      setActiveConversationKey(thread.thread_id);
    } catch (err) {
      messageApi.error(
        err instanceof ApiError ? err.message : locale.requestFailed,
      );
    }
  }, [addConversation, setActiveConversationKey, messageApi]);

  useEffect(() => {
    if (!user) return;
    let cancelled = false;
    (async () => {
      try {
        const items = await loadThreads();
        if (cancelled) return;
        if (items.length > 0) {
          setActiveConversationKey(items[0].thread_id);
        } else {
          await createNewConversation();
        }
      } catch (err) {
        if (!cancelled) {
          messageApi.error(
            err instanceof ApiError ? err.message : locale.requestFailed,
          );
        }
      } finally {
        if (!cancelled) setThreadsReady(true);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [
    user,
    loadThreads,
    createNewConversation,
    messageApi,
    setActiveConversationKey,
  ]);

  if (initializing || !user || !threadsReady) {
    return (
      <Flex align="center" justify="center" style={{ minHeight: '100vh' }}>
        <Spin size="large" />
      </Flex>
    );
  }

  // ==================== Event ====================
  const onSubmit = (val: string) => {
    if (!val || !activeConversationKey) return;
    onRequest({
      messages: [{ role: 'user', content: val }],
      conversation_id: activeConversationKey,
    });
    // 首轮: 标题仍为默认值时本地乐观更新(与后端规则一致: 取首条用户消息截断)
    const active = conversations.find((c) => c.key === activeConversationKey);
    const newTitle = val.trim().slice(0, 50);
    if (active?.label === DEFAULT_THREAD_TITLE && newTitle) {
      setConversations(
        conversations.map((c) =>
          c.key === activeConversationKey ? { ...c, label: newTitle } : c,
        ),
      );
    }
    listRef.current?.scrollTo({ top: 'bottom' });
  };

  // ==================== Nodes ====================
  const chatSide = (
    <div className={styles.side}>
      {/* 🌟 Logo */}
      <div className={styles.logo}>
        <Image
          src="https://mdn.alipayobjects.com/huamei_iwk9zp/afts/img/A*eco6RrQhxbMAAAAAAAAAAAAADgCCAQ/original"
          draggable={false}
          alt="logo"
          width={24}
          height={24}
        />
        <span>Ant Design X</span>
      </div>
      {/* 🌟 会话管理 */}
      <Conversations
        creation={{
          onClick: () => {
            void createNewConversation();
          },
        }}
        items={conversations.map(({ key, label, ...other }) => ({
          key,
          label:
            key === activeConversationKey
              ? `[${locale.curConversation}]${label}`
              : label,
          ...other,
        }))}
        className={styles.conversations}
        activeKey={activeConversationKey}
        onActiveChange={setActiveConversationKey}
        groupable
        styles={{ item: { padding: '0 8px' } }}
        menu={(conversation) => ({
          items: [
            {
              label: locale.rename,
              key: 'rename',
              icon: <EditOutlined />,
              onClick: () => {
                setRenameValue(
                  typeof conversation.label === 'string'
                    ? conversation.label
                    : '',
                );
                setRenameTarget({
                  key: conversation.key,
                  label: String(conversation.label ?? ''),
                });
              },
            },
            {
              label: locale.delete,
              key: 'delete',
              icon: <DeleteOutlined />,
              danger: true,
              onClick: () => {
                threadApi.remove(conversation.key).catch((err) => {
                  messageApi.error(
                    err instanceof ApiError
                      ? err.message
                      : locale.requestFailed,
                  );
                });
                const newList = conversations.filter(
                  (item) => item.key !== conversation.key,
                );
                setConversations(newList);
                if (conversation.key === activeConversationKey) {
                  if (newList.length > 0)
                    setActiveConversationKey(newList[0].key);
                  else void createNewConversation();
                }
              },
            },
          ],
        })}
      />

      <div className={styles.sideFooter}>
        <Dropdown
          trigger={['click']}
          menu={{
            items: [
              ...(isAdmin
                ? [
                    {
                      key: 'users',
                      icon: <TeamOutlined />,
                      label: <Link href="/users">用户管理</Link>,
                    },
                  ]
                : []),
              {
                key: 'logout',
                icon: <LogoutOutlined />,
                label: '退出登录',
                danger: true,
              },
            ],
            onClick: ({ key }) => {
              if (key === 'logout') {
                void logout().then(() => router.replace('/login'));
              }
            },
          }}
        >
          <Space style={{ cursor: 'pointer' }}>
            <Avatar size={24}>{user?.nickname?.slice(0, 1) || 'U'}</Avatar>
            <span style={{ fontSize: 12 }}>{user?.nickname || '用户'}</span>
          </Space>
        </Dropdown>
        <Button type="text" icon={<QuestionCircleOutlined />} />
      </div>
    </div>
  );

  const chatList = (
    <div className={styles.chatList}>
      {isDefaultMessagesRequesting ? (
        <Flex
          align="center"
          justify="center"
          style={{ flex: 1, minHeight: 200 }}
        >
          <Spin />
        </Flex>
      ) : messages?.length ? (
        /* 🌟 消息列表 */
        <Bubble.List
          ref={listRef}
          items={messages?.map((i) => ({
            ...i.message,
            key: i.id,
            status: i.status,
            loading: i.status === 'loading',
            extraInfo: i.extraInfo,
          }))}
          styles={{
            root: {
              maxWidth: 940,
            },
          }}
          role={getRole(className)}
        />
      ) : (
        <Flex
          vertical
          style={{
            maxWidth: 840,
          }}
          gap={16}
          align="center"
          className={styles.placeholder}
        >
          <Welcome
            style={{
              width: '100%',
            }}
            variant="borderless"
            icon="https://mdn.alipayobjects.com/huamei_iwk9zp/afts/img/A*s5sNRo5LjfQAAAAAAAAAAAAADgCCAQ/fmt.webp"
            title={locale.welcome}
            description={locale.welcomeDescription}
            extra={
              <Space>
                <Button icon={<ShareAltOutlined />} />
                <Button icon={<EllipsisOutlined />} />
              </Space>
            }
          />
          <Flex
            gap={16}
            justify="center"
            style={{
              width: '100%',
            }}
          >
            <Prompts
              items={[HOT_TOPICS]}
              styles={{
                list: { height: '100%' },
                item: {
                  flex: 1,
                  backgroundImage:
                    'linear-gradient(123deg, #e5f4ff 0%, #efe7ff 100%)',
                  borderRadius: 12,
                  border: 'none',
                },
                subItem: { padding: 0, background: 'transparent' },
              }}
              onItemClick={(info) => {
                onSubmit(info.data.description as string);
              }}
              className={styles.chatPrompt}
            />

            <Prompts
              items={[DESIGN_GUIDE]}
              styles={{
                item: {
                  flex: 1,
                  backgroundImage:
                    'linear-gradient(123deg, #e5f4ff 0%, #efe7ff 100%)',
                  borderRadius: 12,
                  border: 'none',
                },
                subItem: { background: '#ffffffa6' },
              }}
              onItemClick={(info) => {
                onSubmit(info.data.description as string);
              }}
              className={styles.chatPrompt}
            />
          </Flex>
        </Flex>
      )}
    </div>
  );
  const senderHeader = (
    <Sender.Header
      title={locale.uploadFile}
      open={attachmentsOpen}
      onOpenChange={setAttachmentsOpen}
      styles={{ content: { padding: 0 } }}
    >
      <Attachments
        beforeUpload={() => false}
        items={attachedFiles}
        onChange={(info) => setAttachedFiles(info.fileList)}
        placeholder={(type) =>
          type === 'drop'
            ? { title: locale.dropFileHere }
            : {
                icon: <CloudUploadOutlined />,
                title: locale.uploadFiles,
                description: locale.clickOrDragFilesToUpload,
              }
        }
      />
    </Sender.Header>
  );
  const chatSender = (
    <Flex
      vertical
      gap={12}
      align="center"
      style={{
        margin: 8,
      }}
    >
      {/* 🌟 提示词 */}
      {!attachmentsOpen && (
        <Prompts
          items={SENDER_PROMPTS}
          onItemClick={(info) => {
            onSubmit(info.data.description as string);
          }}
          styles={{
            item: { padding: '6px 12px' },
          }}
          className={styles.senderPrompt}
        />
      )}
      {/* 🌟 输入框 */}
      <Sender
        value={inputValue}
        header={senderHeader}
        onSubmit={() => {
          onSubmit(inputValue);
          setInputValue('');
        }}
        onChange={setInputValue}
        onCancel={() => {
          abort();
        }}
        prefix={
          <Button
            type="text"
            icon={<PaperClipOutlined style={{ fontSize: 18 }} />}
            onClick={() => setAttachmentsOpen(!attachmentsOpen)}
          />
        }
        loading={isRequesting}
        className={styles.sender}
        allowSpeech
        placeholder={locale.askOrInputUseSkills}
      />
    </Flex>
  );

  // ==================== Render =================

  return (
    <XProvider locale={locale}>
      <ChatContext.Provider value={{ onReload, setMessage }}>
        {contextHolder}
        <Modal
          title={locale.rename}
          open={!!renameTarget}
          okButtonProps={{ disabled: !renameValue.trim() }}
          onCancel={() => setRenameTarget(null)}
          onOk={async () => {
            if (!renameTarget) return;
            const title = renameValue.trim();
            if (!title) return;
            try {
              const updated = await threadApi.rename(renameTarget.key, title);
              setConversations(
                conversations.map((c) =>
                  c.key === renameTarget.key
                    ? { ...c, label: updated.title }
                    : c,
                ),
              );
              setRenameTarget(null);
            } catch (err) {
              messageApi.error(
                err instanceof ApiError ? err.message : locale.requestFailed,
              );
            }
          }}
          destroyOnClose
        >
          <Input
            value={renameValue}
            maxLength={255}
            onChange={(e) => setRenameValue(e.target.value)}
          />
        </Modal>
        <div className={styles.layout}>
          {chatSide}
          <div className={styles.chat}>
            {chatList}
            {chatSender}
          </div>
        </div>
      </ChatContext.Provider>
    </XProvider>
  );
};

export default Independent;
