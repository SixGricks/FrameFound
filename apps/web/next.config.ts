import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  output: "standalone", // small runtime image for Docker
  poweredByHeader: false,
};

export default nextConfig;
