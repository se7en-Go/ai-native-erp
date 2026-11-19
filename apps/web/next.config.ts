import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  reactStrictMode: true,
  poweredByHeader: false,
  // Emit a self-contained server bundle (.next/standalone) so the production
  // Docker runtime image only ships the traced server, its node_modules and
  // the static/public assets instead of the whole monorepo.
  output: "standalone",
  async rewrites() {
    // Operator convenience alias: the container smoke test may POST to
    // /api/auth/login. The canonical BFF route stays /api/bff/v1/auth/login
    // (see apps/web/src/app/api/bff/[...path]/route.ts) and is the only path
    // the UI itself calls.
    return [
      {
        source: "/api/auth/login",
        destination: "/api/bff/v1/auth/login",
      },
    ];
  },
};

export default nextConfig;
