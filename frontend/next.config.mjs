/** @type {import('next').NextConfig} */

// Same-origin proxy target for the SCAMNET FastAPI backend.
// The Next server (NOT the browser) forwards /backend-api/* to the
// backend, so client code only ever makes relative, same-origin
// requests: no CORS configuration and no credentials in the bundle.
// Override with BACKEND_INTERNAL_URL when the backend runs elsewhere
// (e.g. BACKEND_INTERNAL_URL=https://traceai-backend-rg.up.railway.app).
const BACKEND_INTERNAL_URL =
  process.env.BACKEND_INTERNAL_URL || 'http://127.0.0.1:8001';

const nextConfig = {
  reactStrictMode: true,
  async rewrites() {
    return [
      {
        source: '/backend-api/:path*',
        destination: `${BACKEND_INTERNAL_URL}/:path*`,
      },
    ];
  },
};

export default nextConfig;
