// IntegrationsModal.jsx
// =====================
// "Connected Apps" modal for SCAMNET: shows the honest connection
// status of the three external applications (Telegram, Google Sheets,
// Google Drive) served by GET /api/integrations.
//
// Honesty rules (mirroring integrations/base.py on the server):
//  * Statuses are ALWAYS fetched from the backend - never invented
//    locally. A card only shows "Connected" when the server reports
//    connected=true (a real authenticated session).
//  * If the backend is unreachable the cards show "Status unknown" -
//    never a fake connected/disconnected state.
//  * The Connect button forwards to POST /api/integrations/{id}/connect
//    and renders the server's honest outcome: 501 -> "Setup required"
//    (auth flow unavailable), 409 -> missing/invalid server-side
//    credentials, 502 -> a real attempt failed (e.g. Google rejected the
//    key). It never simulates a successful authentication.
//  * The server's ``setup_instructions`` are shown for apps that are not
//    connected yet, so an operator can see exactly what to put in the
//    server-side .env instead of guessing.
//  * Telegram gets a reply-loop panel: a connected bot that is NOT
//    polling looks identical (from the scammer's side) to a broken bot,
//    so the modal reports whether the loop is running (push mode),
//    waiting for fetch calls, or stopped - and offers Start/Stop plus a
//    one-click "send hello" (POST /conversation/wake) that needs no LLM.
//
// Visual pattern: same modal shell as ReportModal (modal-overlay /
// modal-card / modal-header / modal-body / modal-footer) + namespaced
// .integ-* card styles in globals.css.
// -------------------------------------------------------------------

import React, { useCallback, useEffect, useState } from 'react';
import {
  INTEGRATION_FALLBACKS,
  fetchIntegrationStatuses,
  connectIntegration,
  disconnectIntegration,
  fetchTelegramLoopStatus,
  startTelegramLoop,
  stopTelegramLoop,
  wakeTelegramChat
} from '@/lib/integrations';

// Per-app icons (inline stroke SVGs, same style as the rest of the UI).
const INTEGRATION_ICONS = {
  // Telegram: paper plane (send)
  telegram: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M22 2L11 13" />
      <path d="M22 2l-7 20-4-9-9-4 20-7z" />
    </svg>
  ),
  // Google Sheets: spreadsheet grid
  google_sheets: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <rect x="3" y="3" width="18" height="18" rx="2" />
      <line x1="3" y1="9" x2="21" y2="9" />
      <line x1="3" y1="15" x2="21" y2="15" />
      <line x1="9" y1="9" x2="9" y2="21" />
    </svg>
  ),
  // Google Drive: upload cloud (report archiving)
  google_drive: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <polyline points="16 16 12 12 8 16" />
      <line x1="12" y1="12" x2="12" y2="21" />
      <path d="M20.39 18.39A5 5 0 0 0 18 9h-1.26A8 8 0 1 0 3 16.3" />
    </svg>
  ),
  // Gmail: envelope (evidence inbox + report delivery)
  gmail: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <rect x="2" y="4" width="20" height="16" rx="2" />
      <polyline points="22 6 12 13 2 6" />
    </svg>
  )
};

export default function IntegrationsModal({ isOpen, onClose }) {
  // id -> status object from the backend (null until first load).
  const [statuses, setStatuses] = useState(null);
  const [isLoading, setIsLoading] = useState(false);
  const [loadError, setLoadError] = useState(null);
  // id -> true while its connect request is in flight.
  const [busyMap, setBusyMap] = useState({});

  // Telegram reply loop: { status, worker, diagnostics } or null.
  const [telegramLoop, setTelegramLoop] = useState(null);
  const [loopBusy, setLoopBusy] = useState(false);
  const [loopNotice, setLoopNotice] = useState(null);
  const [wakeChatId, setWakeChatId] = useState('');
  // id -> { kind: 'setup' | 'error', text } from the last connect attempt.
  const [attempts, setAttempts] = useState({});

  // (Re)fetch the honest statuses from the backend.
  const refresh = useCallback(async () => {
    setIsLoading(true);
    setLoadError(null);
    try {
      const data = await fetchIntegrationStatuses();
      setStatuses(data);
    } catch (err) {
      console.error('Integrations status error:', err);
      setStatuses(null);
      setLoadError(
        'Cannot reach the SCAMNET backend - integration status is unknown.'
      );
    } finally {
      setIsLoading(false);
    }
  }, []);

  // The Telegram reply loop's honest state (never 409s on the server).
  const refreshLoop = useCallback(async () => {
    try {
      const data = await fetchTelegramLoopStatus();
      setTelegramLoop(data);
    } catch (err) {
      // A status failure must not break the modal: keep the last known
      // state and let the card say "unknown".
      console.error('Telegram loop status error:', err);
      setTelegramLoop(null);
    }
  }, []);

  // Load statuses whenever the modal opens.
  useEffect(() => {
    if (isOpen) {
      refresh();
      refreshLoop();
    }
  }, [isOpen, refresh, refreshLoop]);

  // Forward a real connect attempt to the backend and render its
  // honest outcome. No local success state is ever fabricated.
  const handleConnect = async (id) => {
    setBusyMap((prev) => ({ ...prev, [id]: true }));
    setAttempts((prev) => ({ ...prev, [id]: null }));

    try {
      const { ok, status: httpStatus, body } = await connectIntegration(id);

      if (ok) {
        // The server confirmed a genuine connection - resync statuses
        // so the card reflects the authoritative server state.
        await refresh();
        return;
      }

      const detail = body?.detail || {};
      const message =
        typeof detail === 'string' ? detail : detail.message || null;

      if (httpStatus === 501) {
        setAttempts((prev) => ({
          ...prev,
          [id]: {
            kind: 'setup',
            text:
              message ||
              'Authentication flow not implemented yet - server-side setup required.'
          }
        }));
      } else if (httpStatus === 409) {
        setAttempts((prev) => ({
          ...prev,
          [id]: {
            kind: 'setup',
            text: message || 'Server-side credentials are missing.'
          }
        }));
      } else {
        setAttempts((prev) => ({
          ...prev,
          [id]: {
            kind: 'error',
            text: message || `Connect failed (HTTP ${httpStatus}).`
          }
        }));
      }
    } catch (err) {
      console.error('Connect request error:', err);
      setAttempts((prev) => ({
        ...prev,
        [id]: {
          kind: 'error',
          text: 'Backend unreachable - cannot connect right now.'
        }
      }));
    } finally {
      setBusyMap((prev) => ({ ...prev, [id]: false }));
    }
  };

  // Start/stop the Telegram reply loop (the bot's polling worker).
  const handleLoopToggle = async (shouldRun) => {
    setLoopBusy(true);
    setLoopNotice(null);

    try {
      const result = shouldRun
        ? await startTelegramLoop()
        : await stopTelegramLoop();

      if (!result.ok) {
        setLoopNotice({ kind: 'error', text: result.error });
      } else if (shouldRun) {
        setLoopNotice({
          kind: 'ok',
          text: 'Reply loop running - inbound messages are answered automatically.'
        });
      } else {
        setLoopNotice({
          kind: 'setup',
          text:
            'Loop stopped. Messages now wait in Telegram until the loop ' +
            'is started again (or a fetch call picks them up).'
        });
      }

      await refreshLoop();
    } catch (err) {
      console.error('Telegram loop request error:', err);
      setLoopNotice({
        kind: 'error',
        text: 'Backend unreachable - cannot change the reply loop right now.'
      });
    } finally {
      setLoopBusy(false);
    }
  };

  // One-click outbound proof: sends the liveness greeting to a chat id.
  const handleWake = async () => {
    const chatId = Number(wakeChatId);

    if (!Number.isInteger(chatId) || chatId === 0) {
      setLoopNotice({
        kind: 'error',
        text: 'Enter the numeric chat id the bot should greet.'
      });
      return;
    }

    setLoopBusy(true);
    setLoopNotice(null);

    try {
      const result = await wakeTelegramChat(chatId);

      setLoopNotice(
        result.ok
          ? {
              kind: 'ok',
              text: `Hello sent to chat ${chatId} (message id ${result.body?.message_id}).`
            }
          : { kind: 'error', text: result.error }
      );

      await refreshLoop();
    } catch (err) {
      console.error('Telegram wake error:', err);
      setLoopNotice({
        kind: 'error',
        text: 'Backend unreachable - cannot send a test message right now.'
      });
    } finally {
      setLoopBusy(false);
    }
  };

  // Disconnect an established session (never touches credentials).
  const handleDisconnect = async (id) => {
    setBusyMap((prev) => ({ ...prev, [id]: true }));
    setAttempts((prev) => ({ ...prev, [id]: null }));

    try {
      const { ok, status: httpStatus, body } = await disconnectIntegration(id);

      if (ok) {
        await refresh();
        return;
      }

      const detail = body?.detail || {};
      const message =
        typeof detail === 'string' ? detail : detail.message || null;

      setAttempts((prev) => ({
        ...prev,
        [id]: {
          kind: httpStatus === 404 ? 'setup' : 'error',
          text: message || `Disconnect failed (HTTP ${httpStatus}).`
        }
      }));
    } catch (err) {
      console.error('Disconnect request error:', err);
      setAttempts((prev) => ({
        ...prev,
        [id]: {
          kind: 'error',
          text: 'Backend unreachable - cannot disconnect right now.'
        }
      }));
    } finally {
      setBusyMap((prev) => ({ ...prev, [id]: false }));
    }
  };

  // Render nothing unless open (same contract as ReportModal).
  if (!isOpen) return null;

  // Derive the honest per-card view: badge/dot kind + labels.
  const getCardView = (id) => {
    const fallback = INTEGRATION_FALLBACKS.find((f) => f.id === id) || {};
    const st = statuses?.[id] || null;
    const attempt = attempts[id] || null;

    const name = st?.name || fallback.name || id;
    const purpose = st?.purpose || fallback.purpose || '';

    const setup = st?.setup_instructions || '';
    const configured = Boolean(st?.configured);

    if (isLoading && !st) {
      return { name, purpose, kind: 'unknown', label: 'Checking...', detail: '', setup: '', configured: false, attempt: null };
    }
    if (!st) {
      // Backend unreachable / not loaded: honest "unknown" - never
      // presented as connected.
      return { name, purpose, kind: 'unknown', label: 'Status unknown', detail: '', setup: '', configured: false, attempt: null };
    }
    if (st.connected) {
      return { name, purpose, kind: 'connected', label: 'Connected', detail: st.detail || '', setup, configured, attempt: null };
    }
    if (attempt?.kind === 'setup') {
      return { name, purpose, kind: 'setup', label: 'Setup required', detail: st.detail || '', setup, configured, attempt };
    }
    if (attempt?.kind === 'error') {
      return { name, purpose, kind: 'error', label: 'Connection failed', detail: st.detail || '', setup, configured, attempt };
    }
    return { name, purpose, kind: 'off', label: 'Not connected', detail: st.detail || '', setup, configured, attempt: null };
  };

  // Honest summary of the Telegram reply loop for the card panel.
  const loopRunning = Boolean(telegramLoop?.running);
  const worker = telegramLoop?.worker || null;
  const lastError = worker?.last_error || null;

  const loopLabel = !telegramLoop
    ? 'Loop status unknown'
    : loopRunning
      ? `Reply loop running (polling${worker?.poll_timeout != null ? `, ${worker.poll_timeout}s long-poll` : ''})`
      : 'Reply loop stopped - messages wait until it is started';

  return (
    <div className="modal-overlay" id="integrationsModal" onClick={onClose}>
      {/* Stop propagation so clicks inside the card don't close it */}
      <div className="modal-card" onClick={(e) => e.stopPropagation()}>
        <div className="modal-header">
          <h3>Connected Apps</h3>
          <button className="modal-close-btn" onClick={onClose} type="button" aria-label="Close">
            &times;
          </button>
        </div>

        <div className="modal-body">
          <p className="integ-intro">
            External applications SCAMNET uses during an investigation.
            Connections are authenticated on the server - credentials
            never reach your browser.
          </p>

          {loadError && (
            <div className="integ-error-banner" role="alert">
              {loadError}
            </div>
          )}

          {INTEGRATION_FALLBACKS.map(({ id }) => {
            const view = getCardView(id);
            const busy = Boolean(busyMap[id]);

            return (
              <div className="integ-card" key={id}>
                <div className={`integ-icon ${id}`}>
                  {INTEGRATION_ICONS[id]}
                </div>

                <div className="integ-main">
                  <div className="integ-title-row">
                    <h4>{view.name.toUpperCase()}</h4>
                    <span className={`integ-badge ${view.kind}`}>
                      {view.kind === 'connected' ? 'Connected' : view.kind === 'setup' ? 'Setup required' : view.kind === 'error' ? 'Error' : view.kind === 'unknown' ? 'Unknown' : 'Not connected'}
                    </span>
                  </div>

                  <p className="integ-purpose">{view.purpose}</p>

                  <p className="integ-status-line">
                    <span className={`integ-dot ${view.kind}`} />
                    {view.label}
                  </p>

                  {/* Server-provided, secret-free explanation of the state */}
                  {view.detail && (
                    <p className="integ-detail">{view.detail}</p>
                  )}

                  {/* Honest outcome of the last connect attempt */}
                  {view.attempt && (
                    <p className={`integ-message ${view.attempt.kind}`}>
                      {view.attempt.text}
                    </p>
                  )}

                  {/* Server-reported, secret-free facts about a live
                      session (bot username, Google account, ...) */}
                  {view.kind === 'connected' && st?.connection_info &&
                    Object.keys(st.connection_info).length > 0 && (
                      <p className="integ-connection-info">
                        {Object.entries(st.connection_info)
                          .filter(([, value]) => value !== null && value !== undefined && value !== '')
                          .map(([key, value]) => `${key.replace(/_/g, ' ')}: ${value}`)
                          .join(' · ')}
                      </p>
                    )}

                  {/* Telegram reply loop: a connected bot that is not
                      polling never answers anything, so its state and
                      controls live right on the card. */}
                  {id === 'telegram' && view.kind === 'connected' && (
                    <div className="integ-loop">
                      <p className="integ-loop-status">
                        <span
                          className={`integ-dot ${loopRunning ? 'connected' : 'off'}`}
                        />
                        {loopLabel}
                      </p>

                      {worker && (
                        <p className="integ-loop-stats">
                          {worker.polls} poll(s) · {worker.processed} answered
                          {worker.failed ? ` · ${worker.failed} failed` : ''}
                          {worker.greetings ? ` · ${worker.greetings} greeting(s)` : ''}
                          {worker.last_poll_at ? ` · last poll ${worker.last_poll_at}` : ''}
                        </p>
                      )}

                      {lastError && (
                        <p className="integ-message error">{lastError}</p>
                      )}

                      {loopNotice && (
                        <p className={`integ-message ${loopNotice.kind}`}>
                          {loopNotice.text}
                        </p>
                      )}

                      <div className="integ-loop-actions">
                        <button
                          className="btn-outline integ-loop-btn"
                          type="button"
                          disabled={loopBusy || loopRunning}
                          onClick={() => handleLoopToggle(true)}
                        >
                          {loopRunning ? 'Loop running' : 'Start loop'}
                        </button>

                        <button
                          className="btn-outline integ-loop-btn"
                          type="button"
                          disabled={loopBusy || !loopRunning}
                          onClick={() => handleLoopToggle(false)}
                        >
                          Stop loop
                        </button>

                        <button
                          className="btn-outline integ-loop-btn"
                          type="button"
                          disabled={loopBusy}
                          onClick={refreshLoop}
                        >
                          Recheck loop
                        </button>
                      </div>

                      <div className="integ-loop-actions">
                        <input
                          className="integ-loop-input"
                          type="text"
                          inputMode="numeric"
                          placeholder="chat id"
                          value={wakeChatId}
                          onChange={(event) => setWakeChatId(event.target.value)}
                          aria-label="Chat id to greet"
                        />
                        <button
                          className="btn-outline integ-loop-btn"
                          type="button"
                          disabled={loopBusy}
                          onClick={handleWake}
                        >
                          Send test hello
                        </button>
                      </div>

                      <p className="integ-loop-hint">
                        Replies go to the chat that messaged the bot.
                        &quot;Send test hello&quot; proves the outbound path
                        immediately (it needs no LLM); sending
                        <code> /start</code> to the bot in Telegram does the
                        same thing. On hosts that suspend background
                        threads, leave the loop stopped and call
                        <code> POST /api/telegram/conversation/fetch</code>{' '}
                        from a scheduler instead.
                      </p>
                    </div>
                  )}

                  {/* What the operator must do on the SERVER to make
                      this app connect (env vars + credentials file). */}
                  {view.kind !== 'connected' && view.setup && (
                    <details className="integ-setup">
                      <summary>
                        {view.configured
                          ? 'How to finish connecting'
                          : 'Setup instructions'}
                      </summary>
                      <p>{view.setup}</p>
                    </details>
                  )}
                </div>

                <div className="integ-action">
                  {view.kind === 'connected' ? (
                    // Only shown when the SERVER reports a real,
                    // health-verified authenticated session.
                    <>
                      <span className="integ-connected-chip">
                        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="3" strokeLinecap="round" strokeLinejoin="round">
                          <polyline points="20 6 9 17 4 12" />
                        </svg>
                        Connected
                      </span>
                      <button
                        className="btn-outline integ-disconnect-btn"
                        type="button"
                        disabled={busy}
                        onClick={() => handleDisconnect(id)}
                      >
                        {busy ? 'Working...' : 'Disconnect'}
                      </button>
                    </>
                  ) : (
                    <button
                      className="btn-outline integ-connect-btn"
                      type="button"
                      disabled={busy}
                      onClick={() => handleConnect(id)}
                    >
                      {busy ? 'Connecting...' : 'Connect'}
                    </button>
                  )}
                </div>
              </div>
            );
          })}
        </div>

        <div className="modal-footer">
          <p className="integ-note">
            Statuses reflect real server-side configuration. An app is
            only &quot;Connected&quot; after genuine authentication.
          </p>
          <button
            className="btn-outline"
            type="button"
            onClick={refresh}
            disabled={isLoading}
          >
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round" style={{ width: 13, height: 13 }}>
              <polyline points="23 4 23 10 17 10" />
              <path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10" />
            </svg>
            {isLoading ? 'Checking...' : 'Recheck status'}
          </button>
        </div>
      </div>
    </div>
  );
}
