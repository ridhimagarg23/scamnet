/** @type {import('next').NextConfig} */

// Same-origin proxy target for the SCAMNET FastAPI backend.
// The Next server (NOT the browser) forwards /backend-api/* to the
// backend, so client code only ever makes relative, same-origin
// requests: no CORS configuration and no credentials in the bundle.
// Override with BACKEND_INTERNAL_URL when the backend runs elsewhere
// (e.g. BACKEND_INTERNAL_URL=https://traceai-backend-rg.up.railway.app),
// set it in frontend/.env.local for `npm run dev` or in the Vercel
// project settings for a deployment.
//
// NOTE: everything the dashboard calls (both /analyze and
// /api/integrations) goes through this single proxy - see lib/api.js.
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
