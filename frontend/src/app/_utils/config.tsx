import {
  AppstoreAddOutlined,
  CommentOutlined,
  FileSearchOutlined,
  HeartOutlined,
  PaperClipOutlined,
  ProductOutlined,
  ScheduleOutlined,
  SmileOutlined,
} from '@ant-design/icons';
import type { ActionsFeedbackProps } from '@ant-design/x';
import { Prompts } from '@ant-design/x';
import type { DefaultMessageInfo, XModelMessage } from '@ant-design/x-sdk';
import type { GetProp } from 'antd';
import locale from './local';

// ==================== Type ====================
export interface ChatMessage extends XModelMessage {
  extraInfo?: {
    feedback: ActionsFeedbackProps['value'];
  };
}

// ==================== Static Config ====================
export const HISTORY_MESSAGES: {
  [key: string]: DefaultMessageInfo<ChatMessage>[];
} = {
  'default-1': [
    {
      message: { role: 'user', content: locale.howToQuicklyInstallAndImportComponents },
      status: 'success',
    },
    {
      message: {
        role: 'assistant',
        content: locale.aiMessage_2,
      },
      status: 'success',
    },
  ],
  'default-2': [
    { message: { role: 'user', content: locale.newAgiHybridInterface }, status: 'success' },
    {
      message: {
        role: 'assistant',
        content: locale.aiMessage_1,
      },
      status: 'success',
    },
  ],
};

export const DEFAULT_CONVERSATIONS_ITEMS = [
  {
    key: 'default-0',
    label: locale.whatIsAntDesignX,
    group: locale.today,
  },
];

export const HOT_TOPICS = {
  key: '1',
  label: locale.hotTopics,
  children: [
    {
      key: '1-1',
      description: locale.whatComponentsAreInAntDesignX,
      icon: <span style={{ color: '#f93a4a', fontWeight: 700 }}>1</span>,
    },
    {
      key: '1-2',
      description: locale.newAgiHybridInterface,
      icon: <span style={{ color: '#ff6565', fontWeight: 700 }}>2</span>,
    },
    {
      key: '1-3',
      description: locale.whatComponentsAreInAntDesignX,
      icon: <span style={{ color: '#ff8f1f', fontWeight: 700 }}>3</span>,
    },
    {
      key: '1-4',
      description: locale.comeAndDiscoverNewDesignParadigm,
      icon: <span style={{ color: '#00000040', fontWeight: 700 }}>4</span>,
    },
    {
      key: '1-5',
      description: locale.howToQuicklyInstallAndImportComponents,
      icon: <span style={{ color: '#00000040', fontWeight: 700 }}>5</span>,
    },
  ],
};

export const DESIGN_GUIDE = {
  key: '2',
  label: locale.designGuide,
  children: [
    {
      key: '2-1',
      icon: <HeartOutlined />,
      label: locale.intention,
      description: locale.aiUnderstandsUserNeedsAndProvidesSolutions,
    },
    {
      key: '2-2',
      icon: <SmileOutlined />,
      label: locale.role,
      description: locale.aiPublicPersonAndImage,
    },
    {
      key: '2-3',
      icon: <CommentOutlined />,
      label: locale.chat,
      description: locale.howAICanExpressItselfWayUsersUnderstand,
    },
    {
      key: '2-4',
      icon: <PaperClipOutlined />,
      label: locale.interface,
      description: locale.aiBalances,
    },
  ],
};

export const SENDER_PROMPTS: GetProp<typeof Prompts, 'items'> = [
  {
    key: '1',
    description: locale.upgrades,
    icon: <ScheduleOutlined />,
  },
  {
    key: '2',
    description: locale.components,
    icon: <ProductOutlined />,
  },
  {
    key: '3',
    description: locale.richGuide,
    icon: <FileSearchOutlined />,
  },
  {
    key: '4',
    description: locale.installationIntroduction,
    icon: <AppstoreAddOutlined />,
  },
];

export const THOUGHT_CHAIN_CONFIG = {
  loading: {
    title: locale.modelIsRunning,
    status: 'loading',
  },
  updating: {
    title: locale.modelIsRunning,
    status: 'loading',
  },
  success: {
    title: locale.modelExecutionCompleted,
    status: 'success',
  },
  error: {
    title: locale.executionFailed,
    status: 'error',
  },
  abort: {
    title: locale.aborted,
    status: 'abort',
  },
};
