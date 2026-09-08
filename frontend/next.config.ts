import type { NextConfig } from 'next';

const nextConfig: NextConfig = {
  // Docker 部署使用最小化产物: .next/standalone + server.js
  output: 'standalone',
  // 首页图标(logo/welcome)全部来自 public/, 无远程图片源, 因此不需要 images.remotePatterns
};

export default nextConfig;
