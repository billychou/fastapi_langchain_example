import { AntdRegistry } from '@ant-design/nextjs-registry';
import type { Metadata } from 'next';
import type React from 'react';
import { AuthProvider } from '@/lib/auth-context';

export const metadata: Metadata = {
  title: 'Ant Design X 智能对话',
  description: 'FastAPI + LangChain Agent 对话演示',
};

const RootLayout = ({ children }: React.PropsWithChildren) => (
  <html lang="en">
    <body>
      <AntdRegistry>
        <AuthProvider>{children}</AuthProvider>
      </AntdRegistry>
    </body>
  </html>
);

export default RootLayout;
