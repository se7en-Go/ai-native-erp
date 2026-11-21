# syntax=docker/dockerfile:1.7
#
# ai-native-erp Next.js 15 web image (standalone output).
#
# Build from the repository root:
#   docker build -f infra/docker/web.Dockerfile \
#     --build-arg NEXT_PUBLIC_USE_MOCK_API=false -t ai-native-erp/web:local .
#
# Runtime configuration (NOT baked at build time):
#   BFF_BACKEND_URL   e.g. http://backend:8000   (read at request time by the
#                     /api/bff/[...path] route handler)

ARG NODE_VERSION=22
ARG PNPM_VERSION=9.15.4

FROM node:${NODE_VERSION}-alpine AS toolchain
# Re-declared inside the stage: a global ARG is only usable in FROM lines.
ARG PNPM_VERSION=9.15.4
ENV PNPM_HOME=/pnpm \
    PATH="/pnpm:${PATH}" \
    NEXT_TELEMETRY_DISABLED=1
RUN npm install --global "pnpm@${PNPM_VERSION}" && pnpm --version

# ---------------------------------------------------------------------------
# Dependencies: workspace manifests only, so this layer caches on lockfile
# changes and never re-runs for source edits.
# ---------------------------------------------------------------------------
FROM toolchain AS deps
WORKDIR /repo
COPY pnpm-workspace.yaml pnpm-lock.yaml ./
COPY apps/web/package.json apps/web/package.json
COPY apps/widget/package.json apps/widget/package.json
COPY tests/e2e/package.json tests/e2e/package.json
RUN pnpm install --frozen-lockfile

# ---------------------------------------------------------------------------
# Build: Next.js standalone output
# ---------------------------------------------------------------------------
FROM toolchain AS builder
WORKDIR /repo
COPY --from=deps /repo/node_modules ./node_modules
COPY --from=deps /repo/apps/web/node_modules ./apps/web/node_modules
COPY --from=deps /repo/apps/widget/node_modules ./apps/widget/node_modules
COPY . .

# Public (client-visible) build args. Defaults target the live stack.
ARG NEXT_PUBLIC_USE_MOCK_API=false
ARG NEXT_PUBLIC_BACKEND_SSE_URL=http://localhost:8000
ENV NEXT_PUBLIC_USE_MOCK_API=${NEXT_PUBLIC_USE_MOCK_API} \
    NEXT_PUBLIC_BACKEND_SSE_URL=${NEXT_PUBLIC_BACKEND_SSE_URL} \
    NODE_ENV=production

RUN pnpm --filter web build

# ---------------------------------------------------------------------------
# Runtime: distroless-ish, non-root `node` user, standalone server only
# ---------------------------------------------------------------------------
FROM node:${NODE_VERSION}-alpine AS runtime
ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    PORT=3000 \
    HOSTNAME=0.0.0.0 \
    BFF_BACKEND_URL=http://backend:8000

WORKDIR /app

# standalone/ holds server.js (at apps/web/server.js for this monorepo), the
# traced node_modules and the minimal package.json. Static assets and public/
# are NOT traced and must be copied alongside.
COPY --from=builder --chown=node:node /repo/apps/web/.next/standalone ./
COPY --from=builder --chown=node:node /repo/apps/web/.next/static ./apps/web/.next/static
COPY --from=builder --chown=node:node /repo/apps/web/public ./apps/web/public

USER node
EXPOSE 3000

HEALTHCHECK --interval=15s --timeout=5s --start-period=25s --retries=5 \
    CMD ["node", "-e", "fetch('http://127.0.0.1:3000/login').then((r) => process.exit(r.ok ? 0 : 1)).catch(() => process.exit(1))"]

CMD ["node", "apps/web/server.js"]
