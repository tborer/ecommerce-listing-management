import type { NextConfig } from "next";

// The Python API is a separate Vercel project. Proxying /api/* through this
// app keeps the browser on one origin, so the httpOnly session cookie is
// first-party and no CORS is needed.
const apiOrigin = (process.env.API_ORIGIN || "http://localhost:8000").replace(/\/$/, "");

const nextConfig: NextConfig = {
  async rewrites() {
    return [{ source: "/api/:path*", destination: `${apiOrigin}/api/:path*` }];
  },
};

export default nextConfig;
