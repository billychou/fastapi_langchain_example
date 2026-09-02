import type { NextConfig } from 'next';

const nextConfig: NextConfig = {
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
