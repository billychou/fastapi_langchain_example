import { AntdRegistry } from '@ant-design/nextjs-registry';
import type React from 'react';
import { AuthProvider } from '@/lib/auth-context';

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
