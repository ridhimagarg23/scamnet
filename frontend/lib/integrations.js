// integrations.js
// ===============
// API helpers for the SCAMNET "Connected Apps" (integrations) UI.
//
// SECURITY MODEL:
//  * All requests go through the SAME-ORIGIN /backend-api/* proxy
//    (see next.config.mjs rewrites + lib/api.js) - the browser never
//    talks to the backend cross-origin and never sees credentials.
//  * The backend only ever returns honest, secret-free status
//    (integrations/base.py). The UI must never invent a "connected"
//    state on its own: if the backend cannot be reached, the status
//    is UNKNOWN - not connected, not disconnected.
// -------------------------------------------------------------------

import { apiUrl } from '@/lib/api';

// Fixed card order + copy used when the backend is unreachable, so the
// modal still explains each app's purpose honestly (status = unknown).
export const INTEGRATION_FALLBACKS = [
  {
    id: 'telegram',
    name: 'Telegram',
    purpose: 'Communication & intelligence gathering'
  },
  {
    id: 'google_sheets',
    name: 'Google Sheets',
    purpose: 'Live investigation evidence'
  },
  {
    id: 'google_drive',
    name: 'Google Drive',
    purpose: 'Investigation reports'
  },
  {
    id: 'gmail',
    name: 'Gmail',
    purpose: 'Evidence inbox & report delivery'
  }
];

// GET /api/integrations -> { telegram: {...}, google_sheets: {...}, ... }
// Throws on any non-OK response so callers show an honest error state.
export async function fetchIntegrationStatuses() {
  const response = await fetch(apiUrl('/api/integrations'), {
    cache: 'no-store'
  });
  if (!response.ok) {
    throw new Error(`Integrations API returned HTTP ${response.status}`);
  }
  return response.json();
}

// POST /api/integrations/{id}/connect
// Resolves (never throws on HTTP errors) with:
//   { ok, status, body }
// Honest backend outcomes: 200 connected | 409 not_configured |
// 501 setup_required | 404 unknown. Network failures throw.
export async function connectIntegration(integrationId) {
  const response = await fetch(
    apiUrl(`/api/integrations/${encodeURIComponent(integrationId)}/connect`),
    { method: 'POST' }
  );

  let body = null;
  try {
    body = await response.json();
  } catch {
    // Non-JSON body (e.g. proxy error page) - keep body null.
  }

  return { ok: response.ok, status: response.status, body };
}

// POST /api/integrations/{id}/disconnect
// Drops a live session without touching server-side credentials.
// Same { ok, status, body } contract as connectIntegration.
export async function disconnectIntegration(integrationId) {
  const response = await fetch(
    apiUrl(`/api/integrations/${encodeURIComponent(integrationId)}/disconnect`),
    { method: 'POST' }
  );

  let body = null;
  try {
    body = await response.json();
  } catch {
    // Non-JSON body - keep body null.
  }

  return { ok: response.ok, status: response.status, body };
}
