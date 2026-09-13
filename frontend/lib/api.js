// api.js
// ======
// Single place that decides where the browser sends API calls.
//
// The dashboard normally uses the SAME-ORIGIN Next.js proxy
// (`/backend-api/...`), which next.config.mjs forwards to the FastAPI
// backend. This works in local dev, Vercel preview/production and
// behind a reverse proxy - the browser does not need CORS and never
// embeds backend credentials.
//
// Override with NEXT_PUBLIC_API_URL only for deployments where the
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
        message =
          'The backend reached but the investigation pipeline failed. ' +
          'Check the server logs or the backend URL in BACKEND_INTERNAL_URL.';
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
