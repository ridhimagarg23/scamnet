/** @type {import('next').NextConfig} */

// Same-origin proxy target for the SCAMNET FastAPI backend.
// The Next server (NOT the browser) forwards /backend-api/* to the
// backend, so client code only needs relative, same-origin requests:
// no CORS configuration and no credentials in the browser bundle.
//
// Resolution order:
//   1. BACKEND_INTERNAL_URL - explicit override for any deployment
//   2. Vercel deployments   - the existing hosted Railway backend, so
//                             the dashboard works without forgetting
//                             project environment variables
//   3. local development    - http://127.0.0.1:8001
const HOSTED_BACKEND_FALLBACK_URL =
  'https://traceai-backend-rg.up.railway.app';

const isVercel =
  process.env.VERCEL === '1' ||
  Boolean(process.env.VERCEL_ENV) ||
  Boolean(process.env.NEXT_PUBLIC_VERCEL_URL);

const BACKEND_INTERNAL_URL = process.env.BACKEND_INTERNAL_URL
  ? process.env.BACKEND_INTERNAL_URL.replace(/\/+$/, '')
  : isVercel
    ? HOSTED_BACKEND_FALLBACK_URL
    : 'http://127.0.0.1:8001';

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
