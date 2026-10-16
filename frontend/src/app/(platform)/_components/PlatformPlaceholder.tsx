'use client';

/**
 * 平台模块占位页: 路由与菜单先就位, 功能按 docs/aiops-platform-design.md
 * 的路线图分阶段落地。统一视觉, 避免每个模块各写一个「敬请期待」。
 */
import { Empty, Typography } from 'antd';

const { Title, Text } = Typography;

export interface PlatformPlaceholderProps {
  title: string;
  description?: string;
}

const PlatformPlaceholder: React.FC<PlatformPlaceholderProps> = ({
  title,
  description,
}) => (
  <div
    className="platform-card"
    style={{
      padding: 48,
      display: 'flex',
      flexDirection: 'column',
      alignItems: 'center',
      justifyContent: 'center',
      minHeight: 320,
    }}
  >
    <Title level={4} style={{ marginBottom: 8 }}>
      {title}
    </Title>
    <Empty
      image={Empty.PRESENTED_IMAGE_SIMPLE}
      description={
        <Text type="secondary">
          {description ?? '该模块建设中, 敬请期待。'}
        </Text>
      }
    />
  </div>
);

export default PlatformPlaceholder;
