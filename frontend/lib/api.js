// api.js
// ======
// Single place that decides where the browser sends API calls.
//
// Two modes, one helper (`apiUrl()`), used by every call site:
//
// * DIRECT (local `run_all` runs): `NEXT_PUBLIC_API_URL` points the
//   browser straight at FastAPI. This is the default locally because
//   POST /analyze runs three sequential LLM calls (~35-50 s on a slow
//   model) while the Next.js dev rewrite proxy drops proxied requests
//   after ~30 s ("Failed to proxy ... socket hang up", ECONNRESET) -
//   the backend keeps working, but the dashboard sees a 500. Browser
//   fetch has no such ceiling. run_all.py also pre-fills the backend's
//   CORS_ALLOW_ORIGINS, so no manual CORS setup is needed.
// * PROXY (hosted/Vercel): without NEXT_PUBLIC_API_URL the browser
//   stays same-origin (`/backend-api/...`), which next.config.mjs
//   forwards to the FastAPI backend - no CORS needed at all.
//
// Override with NEXT_PUBLIC_API_URL for any deployment where the
// browser should call the backend directly. The backend's
// CORS_ALLOW_ORIGINS must then include this frontend's origin.
// -------------------------------------------------------------------

/** Prefix of the same-origin proxy defined in next.config.mjs. */
export const BACKEND_PROXY_PREFIX = '/backend-api';

/** Historical hosted backend used as the server-side proxy target on Vercel. */
export const HOSTED_BACKEND_URL =
  'https://traceai-backend-rg.up.railway.app';

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
 * @returns {string}    Browser-usable URL.
 */
export function apiUrl(path) {
  const normalized = path.startsWith('/') ? path : `/${path}`;
  return `${getApiBase()}${normalized}`;
}

/**
 * Extract FastAPI's human-readable error from a failed response.
 *
 * FastAPI commonly returns:
 *   {"detail": "plain message"}
 * or:
 *   {"detail": {"status": "...", "message": "..."}}
 *
 * @param {Response} response Failed fetch response.
 * @returns {Promise<Error>} Error with `.status` and `.body` attached.
 */
export async function createApiError(response) {
  let body = null;

  try {
    body = await response.json();
  } catch {
    // HTML proxy/host error pages are common when the backend cannot
    // be reached from the Next server; fall back to status-specific text.
  }

  const detail = body?.detail;
  let message =
    typeof detail === 'string'
      ? detail
      : detail?.message || body?.message || '';

  if (!message) {
    switch (response.status) {
      case 400:
        message = 'The backend rejected that message as invalid.';
        break;
      case 404:
        message = 'Backend endpoint not found. Check the API proxy path.';
        break;
      case 429:
        message = 'The AI or messaging provider is rate-limiting requests. Wait a moment and retry.';
        break;
      case 500:
        // A 500 WITH a FastAPI JSON body is a genuine pipeline failure
        // and already produced `message` above. Reaching this branch
        // means the body was NOT JSON - typically the Next.js proxy's
        // plain-text "Internal Server Error" after it drops a slow
        // /analyze request at ~30 s (ECONNRESET "socket hang up") while
        // the backend is still working through the AI calls. Point at
        // the backend logs, not at the (usually correct) proxy URL.
        message = body
          ? 'The backend failed while running the investigation pipeline. ' +
            'Check the backend [api] server logs for the traceback.'
          : 'Lost contact with the backend during analysis. ' +
            'Check the backend [api] server logs - it may still be working.';
        break;
      case 502:
      case 503:
      case 504:
        message =
          'The backend could not complete the upstream AI/Telegram request. ' +
          'Check that the backend, OPENROUTER_API_KEY, and integrations are configured.';
        break;
      default:
        message = `Trace request failed (HTTP ${response.status}).`;
    }
  }

  const error = new Error(message);
  error.status = response.status;
  error.body = body;
  return error;
}

/**
 * Fetch a backend JSON endpoint and throw an actionable error on failure.
 *
 * @param {string} path Endpoint path.
 * @param {RequestInit} options Standard fetch options.
 * @returns {Promise<Response>} Successful response.
 */
export async function apiFetch(path, options = {}) {
  let response;

  try {
    response = await fetch(apiUrl(path), options);
  } catch (networkError) {
    const base = getApiBase();
    const error = new Error(
      `Cannot reach the TraceAI backend at ${base}. ` +
        (base.startsWith(BACKEND_PROXY_PREFIX)
          ? 'If running locally, start FastAPI on port 8001; when hosted, set BACKEND_INTERNAL_URL to the backend URL.'
          : 'Check the backend URL, browser network, and CORS settings.')
    );
    error.cause = networkError;
    throw error;
  }

  if (!response.ok) {
    throw await createApiError(response);
  }

  return response;
}
