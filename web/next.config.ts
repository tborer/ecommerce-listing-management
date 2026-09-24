import type { NextConfig } from "next";

// The Python API is a separate Vercel project. Proxying /api/* through this
// app keeps the browser on one origin, so the httpOnly session cookie is
// first-party and no CORS is needed. /api/waitlist is handled by this app
// itself (filesystem routes win over rewrites).
//
// On Vercel, the proxy only exists once API_ORIGIN is set, so the website can
// go live before the API project does. Locally it defaults to the dev API.
const apiOrigin = (process.env.API_ORIGIN || (process.env.VERCEL ? "" : "http://localhost:8000")).replace(/\/$/, "");

const nextConfig: NextConfig = {
  async rewrites() {
    return apiOrigin ? [{ source: "/api/:path*", destination: `${apiOrigin}/api/:path*` }] : [];
  },
};

export default nextConfig;
