import type { NextConfig } from "next";

const apiOrigin = process.env.API_ORIGIN ?? "http://127.0.0.1:8000";

const nextConfig: NextConfig = {
  output: "standalone",
  reactStrictMode: true,
  rewrites: async () => [
    {
      source: "/backend/:path*",
      destination: `${apiOrigin}/:path*`,
    },
  ],
};

export default nextConfig;
