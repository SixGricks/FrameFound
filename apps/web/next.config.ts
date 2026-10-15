import type { NextConfig } from "next";

// In production Caddy routes /api/* to the api container and everything else
// here. For local development there is no Caddy, so set
// FRAMEFOUND_API_PROXY=http://localhost:8000 (apps/web/.env.local) and the
// dev server forwards the API itself. Unset — as in the Docker image — this
// adds nothing.
const apiProxy = process.env.FRAMEFOUND_API_PROXY;

const nextConfig: NextConfig = {
  output: "standalone", // small runtime image for Docker
  poweredByHeader: false,
  async rewrites() {
    if (!apiProxy) return [];
    return [{ source: "/api/:path*", destination: `${apiProxy}/api/:path*` }];
  },
};

export default nextConfig;
