# 三阶段构建: 依赖 → 编译 → standalone 运行时
FROM node:22-slim AS deps
RUN corepack enable
WORKDIR /app
COPY package.json pnpm-lock.yaml ./
RUN corepack pnpm install --frozen-lockfile

FROM node:22-slim AS build
RUN corepack enable
WORKDIR /app
COPY --from=deps /app/node_modules ./node_modules
COPY . .
ENV NEXT_TELEMETRY_DISABLED=1
# NEXT_PUBLIC_* 在编译期内联, 浏览器需可达的后端地址(宿主端口 5001)
ARG NEXT_PUBLIC_API_BASE=http://localhost:5001
ENV NEXT_PUBLIC_API_BASE=${NEXT_PUBLIC_API_BASE}
RUN corepack pnpm build

FROM node:22-slim AS runtime
ENV NODE_ENV=production PORT=3000 HOSTNAME=0.0.0.0
WORKDIR /app
RUN useradd --create-home appuser
COPY --from=build --chown=appuser /app/.next/standalone ./
COPY --from=build --chown=appuser /app/.next/static ./.next/static
COPY --from=build --chown=appuser /app/public ./public
USER appuser
EXPOSE 3000
CMD ["node", "server.js"]
