# syntax=docker/dockerfile:1.7
#
# ai-native-erp WebChat widget image: builds the IIFE bundle with Vite and
# serves it from nginx on :3001 with permissive CORS for embedding on any
# merchant storefront (Shopify app embed, custom theme, static site).
#
# Build from the repository root:
#   docker build -f infra/docker/widget.Dockerfile -t ai-native-erp/widget:local .

ARG NODE_VERSION=22
ARG PNPM_VERSION=9.15.4

# Hard cap mirrored from the CI gate: gzip payload must stay under 35 KiB
# (35,840 bytes) so merchant storefronts keep a small LCP budget.
ARG WIDGET_GZIP_LIMIT_BYTES=35840

FROM node:${NODE_VERSION}-alpine AS toolchain
# Re-declared inside the stage: a global ARG is only usable in FROM lines.
ARG PNPM_VERSION=9.15.4
ENV PNPM_HOME=/pnpm \
    PATH="/pnpm:${PATH}" \
    NEXT_TELEMETRY_DISABLED=1
RUN npm install --global "pnpm@${PNPM_VERSION}" && pnpm --version

FROM toolchain AS deps
WORKDIR /repo
COPY pnpm-workspace.yaml pnpm-lock.yaml ./
COPY apps/web/package.json apps/web/package.json
COPY apps/widget/package.json apps/widget/package.json
COPY tests/e2e/package.json tests/e2e/package.json
RUN pnpm install --frozen-lockfile

FROM toolchain AS builder
ARG WIDGET_GZIP_LIMIT_BYTES
WORKDIR /repo
COPY --from=deps /repo/node_modules ./node_modules
COPY --from=deps /repo/apps/web/node_modules ./apps/web/node_modules
COPY --from=deps /repo/apps/widget/node_modules ./apps/widget/node_modules
COPY apps/widget apps/widget
COPY pnpm-workspace.yaml pnpm-lock.yaml ./
RUN pnpm --filter @erp/widget build \
    && bytes=$(gzip -c apps/widget/dist/livechat-widget.iife.js | wc -c) \
    && printf 'livechat-widget.iife.js: %s gzip bytes (limit %s)\n' "$bytes" "$WIDGET_GZIP_LIMIT_BYTES" \
    && if [ "$bytes" -ge "$WIDGET_GZIP_LIMIT_BYTES" ]; then echo "widget bundle exceeds gzip budget" >&2; exit 1; fi

FROM nginx:1.27-alpine AS runtime
COPY infra/docker/widget-nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=builder /repo/apps/widget/dist/livechat-widget.iife.js /usr/share/nginx/html/livechat-widget.iife.js
EXPOSE 3001
HEALTHCHECK --interval=15s --timeout=5s --start-period=5s --retries=5 \
    CMD wget --quiet --output-document=/dev/null http://127.0.0.1:3001/healthz || exit 1
