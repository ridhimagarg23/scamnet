// api.js
// ======
// Single place that decides where the browser sends API calls.
//
//  * Default: the SAME-ORIGIN Next.js proxy (`/backend-api/...`), which
//    next.config.mjs forwards to the FastAPI backend. This works in
//    local dev, on Vercel, in preview deployments and behind any reverse
//    proxy - the browser never needs CORS and never needs to know the
//    backend hostname.
//  * Override: NEXT_PUBLIC_API_URL, for deployments that want the browser
//    to call the backend directly (then the backend's CORS_ALLOW_ORIGINS
//    must include this frontend's origin).
//
// Before this module, the dashboard (page.jsx) called
// `http://127.0.0.1:8001` for localhost and a hard-coded Railway URL for
// every other host, while the Connected Apps modal used the
// `/backend-api` proxy (whose default target is also 127.0.0.1:8001).
// The two halves therefore talked to DIFFERENT backends, and on any host
// that was not localhost (Vercel, previews, a LAN IP) the dashboard and
// the integrations both failed - which is exactly the "Google Drive /
// Gmail / Sheeets will not connect" symptom.
// -------------------------------------------------------------------

/** Prefix of the same-origin proxy defined in next.config.mjs. */
export const BACKEND_PROXY_PREFIX = '/backend-api';

/**
 * Absolute base URL for backend calls, or the same-origin proxy prefix.
 *
 * @returns {string} e.g. `/backend-api` or `https://api.example.com`
 */
export function getApiBase() {
  const configured = process.env.NEXT_PUBLIC_API_URL;

  if (configured) {
    return configured.replace(/\/+$/, '');
  }

  return BACKEND_PROXY_PREFIX;
}

/**
 * Build the URL of one backend endpoint.
 *
 * @param {string} path Endpoint path, e.g. `/api/integrations`.
 * @returns {string}     Browser-usable URL.
 */
export function apiUrl(path) {
  const normalized = path.startsWith('/') ? path : `/${path}`;
  return `${getApiBase()}${normalized}`;
}
