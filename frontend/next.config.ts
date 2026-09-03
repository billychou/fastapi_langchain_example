import type { NextConfig } from 'next';

const nextConfig: NextConfig = {
  // Docker 部署使用最小化产物: .next/standalone + server.js
  output: 'standalone',
  images: {
    // 侧栏 Logo 为远程 CDN 图片, next/image 需显式放行来源
    remotePatterns: [
      {
        protocol: 'https',
        hostname: 'mdn.alipayobjects.com',
        pathname: '/huamei_iwk9zp/**',
      },
    ],
  },
};

export default nextConfig;
