import { AntdRegistry } from '@ant-design/nextjs-registry';
import type { Metadata } from 'next';
import type React from 'react';
import { AuthProvider } from '@/lib/auth-context';

export const metadata: Metadata = {
  title: 'AI运维智能平台',
  description:
    '企业级 AI 运维智能平台: 多智能体协作调度、运维连接器、资产与任务管理、可观测一体',
};

const RootLayout = ({ children }: React.PropsWithChildren) => (
  <html lang="zh-CN">
    <body>
      <AntdRegistry>
        <AuthProvider>{children}</AuthProvider>
      </AntdRegistry>
    </body>
  </html>
);

export default RootLayout;
