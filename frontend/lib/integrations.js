// integrations.js
// ===============
// API helpers for the SCAMNET "Connected Apps" (integrations) UI.
//
// SECURITY MODEL:
//  * All requests go through lib/api.js - directly to the backend in
//    local `run_all` runs, via the SAME-ORIGIN /backend-api/* proxy
//    (next.config.mjs rewrites) when hosted. Either way the browser
//    never sees credentials.
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

// -------------------------------------------------------------------
// Telegram reply loop (the bot's polling worker)
// -------------------------------------------------------------------
// A connected Telegram bot is only useful while its reply loop is
// running, so the dashboard exposes the loop's honest state and lets an
// operator start/stop it without reaching for curl.
//
// GET /api/telegram/conversation/status never 409s: it reports whether
// the bot polls on a background thread (delivery_mode "push"), falls
// back to fetch mode ("fetch"), and includes the worker's counters.
export async function fetchTelegramLoopStatus() {
  const response = await fetch(apiUrl('/api/telegram/conversation/status'), {
    cache: 'no-store'
  });

  if (!response.ok) {
    throw new Error(`Telegram status API returned HTTP ${response.status}`);
  }

  return response.json();
}

// POST /api/telegram/conversation/{start|stop} -> { ok, status, body }
// Same contract as connectIntegration: HTTP errors resolve (never
// throw) so the card can render the server's own message.
async function telegramLoopAction(action) {
  const response = await fetch(
    apiUrl(`/api/telegram/conversation/${action}`),
    { method: 'POST' }
  );

  let body = null;
  try {
    body = await response.json();
  } catch {
    // Non-JSON body (proxy error page) - keep body null.
  }

  if (!response.ok) {
    const detail = body?.detail;
    const message =
      typeof detail === 'string' ? detail : detail?.message || null;

    return {
      ok: false,
      status: response.status,
      body,
      error: message || `Request failed (HTTP ${response.status}).`
    };
  }

  return { ok: true, status: response.status, body, error: null };
}

export function startTelegramLoop() {
  return telegramLoopAction('start');
}

export function stopTelegramLoop() {
  return telegramLoopAction('stop');
}

// POST /api/telegram/conversation/wake - sends the liveness greeting to
// one chat without needing an LLM. Useful as a one-click "can the bot
// actually send?" check from the dashboard.
export async function wakeTelegramChat(chatId) {
  const response = await fetch(apiUrl('/api/telegram/conversation/wake'), {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ chat_id: Number(chatId) })
  });

  let body = null;
  try {
    body = await response.json();
  } catch {
    // Non-JSON body - keep body null.
  }

  if (!response.ok) {
    const detail = body?.detail;
    const message =
      typeof detail === 'string' ? detail : detail?.message || null;

    return {
      ok: false,
      status: response.status,
      error: message || `Request failed (HTTP ${response.status}).`
    };
  }

  return { ok: true, status: response.status, body, error: null };
}
