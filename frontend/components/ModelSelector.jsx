// ModelSelector.jsx
// =================
// Dashboard model picker: provider toggle (OpenRouter / NVIDIA NIM)
// + model dropdown + live-model refresh.
//
// The component is self-contained: it loads GET /api/llm/status and
// GET /api/llm/models on mount, switches the backend's runtime-active
// selection via POST /api/llm/select, and reports the effective
// (provider, model) to the parent through onSelectionChange so the
// next POST /analyze carries the same choice.
//
// Resilience rules:
//  * The picker NEVER blocks the dashboard - a failed load degrades to
//    a small warning line while /analyze keeps using the server-side
//    active selection.
//  * Unconfigured providers are shown honestly (with the exact .env key
//    to add) instead of failing on click.
//  * Any model id may be selected, even one missing from the catalog -
//    the backend tries it first and auto-falls-back to spares.
// -------------------------------------------------------------------

"use client";

import React, { useCallback, useEffect, useState } from 'react';
import { apiFetch } from '@/lib/api';

const PROVIDERS = [
  { id: 'openrouter', label: 'OpenRouter' },
  { id: 'nvidia', label: '⚡ NVIDIA NIM' },
];

const SPEED_SUFFIX = {
  lightning: ' ⚡ fastest',
  fast: ' · fast',
  balanced: '',
  powerful: ' · powerful',
  unknown: '',
};

const STORAGE_KEY = 'traceai-llm-selection';

function loadStoredSelection() {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    return raw ? JSON.parse(raw) : {};
  } catch {
    return {};
  }
}

function storeSelection(provider, model) {
  try {
    const prev = loadStoredSelection();
    localStorage.setItem(
      STORAGE_KEY,
      JSON.stringify({ ...prev, [provider]: model })
    );
  } catch {
    // Private-mode browsers: selection simply does not persist.
  }
}

export default function ModelSelector({ onSelectionChange }) {
  const [status, setStatus] = useState(null);
  const [models, setModels] = useState({ openrouter: [], nvidia: [] });
  const [sources, setSources] = useState({});
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [selecting, setSelecting] = useState(false);
  const [notice, setNotice] = useState(null);

  const notifyParent = useCallback(
    (provider, model) => {
      if (typeof onSelectionChange === 'function') {
        onSelectionChange(provider, model);
      }
    },
    [onSelectionChange]
  );

  // ---- initial load: status + both catalogs (each best-effort) ----
  useEffect(() => {
    let cancelled = false;

    async function load() {
      setLoading(true);
      setNotice(null);

      try {
        const statusRes = await apiFetch('/api/llm/status');
        const statusData = await statusRes.json();
        if (cancelled) return;
        setStatus(statusData);

        const nextModels = { openrouter: [], nvidia: [] };
        const nextSources = {};

        await Promise.all(
          PROVIDERS.map(async ({ id }) => {
            try {
              const res = await apiFetch(
                `/api/llm/models?provider=${id}`
              );
              const data = await res.json();
              nextModels[id] = data.models || [];
              nextSources[id] = data.source || 'catalog';
            } catch {
              // One provider's catalog failing must not hide the other.
              nextModels[id] = [];
            }
          })
        );

        if (cancelled) return;
        setModels(nextModels);
        setSources(nextSources);

        // Restore the analyst's last per-provider pick when it still
        // exists; otherwise the server-side active selection wins.
        const stored = loadStoredSelection();
        const activeProvider = statusData.active_provider;
        const storedModel = stored[activeProvider];
        const known = (nextModels[activeProvider] || []).some(
          (m) => m.id === storedModel
        );
        const effectiveModel = known
          ? storedModel
          : statusData.active_model;

        if (known && storedModel !== statusData.active_model) {
          try {
            const res = await apiFetch('/api/llm/select', {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({
                provider: activeProvider,
                model: storedModel,
              }),
            });
            const updated = await res.json();
            if (!cancelled) setStatus(updated);
          } catch {
            // Restoring is best-effort; the server default still works.
          }
        }

        notifyParent(activeProvider, effectiveModel);
      } catch (err) {
        if (!cancelled) {
          setNotice(
            'Could not reach the backend model service - the dashboard keeps using the server default.'
          );
        }
      } finally {
        if (!cancelled) setLoading(false);
      }
    }

    load();

    return () => {
      cancelled = true;
    };
  }, [notifyParent]);

  // ---- switch provider ----
  const handleProvider = async (providerId) => {
    if (!status || selecting || status.active_provider === providerId) return;

    const provider = status.providers?.[providerId];

    if (provider && !provider.configured) {
      setNotice(
        `${provider.label} is not configured on the server. Add ${provider.key_name} to the backend .env and restart the backend to enable it.`
      );
      return;
    }

    setSelecting(true);
    setNotice(null);

    // Prefer the analyst's last model for this provider, else default.
    const stored = loadStoredSelection()[providerId];
    const known = (models[providerId] || []).some((m) => m.id === stored);
    const model = known ? stored : undefined;

    try {
      const res = await apiFetch('/api/llm/select', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(
          model ? { provider: providerId, model } : { provider: providerId }
        ),
      });
      const updated = await res.json();
      setStatus(updated);
      storeSelection(updated.active_provider, updated.active_model);
      notifyParent(updated.active_provider, updated.active_model);

      if (updated.model_hint) setNotice(updated.model_hint);
    } catch (err) {
      setNotice(err?.message || 'Could not switch AI provider.');
    } finally {
      setSelecting(false);
    }
  };

  // ---- switch model within the active provider ----
  const handleModel = async (modelId) => {
    if (!status || selecting || modelId === status.active_model) return;

    setSelecting(true);
    setNotice(null);

    try {
      const res = await apiFetch('/api/llm/select', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          provider: status.active_provider,
          model: modelId,
        }),
      });
      const updated = await res.json();
      setStatus(updated);
      storeSelection(updated.active_provider, updated.active_model);
      notifyParent(updated.active_provider, updated.active_model);

      if (updated.model_hint) setNotice(updated.model_hint);
    } catch (err) {
      setNotice(err?.message || 'Could not switch AI model.');
    } finally {
      setSelecting(false);
    }
  };

  // ---- live refresh: ask the provider what IT can serve right now ----
  const handleRefresh = async () => {
    if (!status || refreshing) return;

    const providerId = status.active_provider;
    const provider = status.providers?.[providerId];

    if (provider && !provider.configured) {
      setNotice(
        `Live refresh needs ${provider.key_name} on the server - showing the curated catalog instead.`
      );
      return;
    }

    setRefreshing(true);
    setNotice(null);

    try {
      const res = await apiFetch(
        `/api/llm/models?provider=${providerId}&refresh=true`
      );
      const data = await res.json();

      setModels((prev) => ({ ...prev, [providerId]: data.models || [] }));
      setSources((prev) => ({ ...prev, [providerId]: data.source }));

      if (String(data.source || '').startsWith('catalog (')) {
        setNotice(
          'Live model list was unreachable - showing the curated catalog. Paste new NVIDIA ids into llm_models.json.'
        );
      }
    } catch (err) {
      setNotice(err?.message || 'Live refresh failed - catalog kept.');
    } finally {
      setRefreshing(false);
    }
  };

  const activeProvider = status?.active_provider || 'openrouter';
  const activeModels = models[activeProvider] || [];
  const activeInfo = status?.providers?.[activeProvider];
  const source = sources[activeProvider] || 'catalog';
  const isLive = source === 'live' || source === 'live-cache';
  const spareCount = (activeInfo?.fallback_models || []).length;
  const crossProvider =
    status?.cross_provider_fallback &&
    status?.providers &&
    Object.values(status.providers).filter((p) => p.configured).length > 1;

  // The active model may be a custom id missing from the catalog -
  // still show it as the selected option (never a blank select).
  const selectOptions = [...activeModels];
  if (
    status?.active_model &&
    !selectOptions.some((m) => m.id === status.active_model)
  ) {
    selectOptions.unshift({
      id: status.active_model,
      label: status.active_model,
      speed: 'unknown',
    });
  }

  return (
    <div className="llm-bar" id="llmSelector">
      <div className="llm-brand">
        <span className="llm-bolt" aria-hidden="true">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round">
            <polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2" />
          </svg>
        </span>
        <span>AI&nbsp;Engine</span>
      </div>

      {loading ? (
        <span className="llm-status-line">Loading AI providers…</span>
      ) : !status ? (
        <span className="llm-status-line llm-warn">
          {notice || 'Model service unavailable.'}
        </span>
      ) : (
        <>
          <div className="llm-toggle" role="tablist" aria-label="LLM provider">
            {PROVIDERS.map(({ id, label }) => {
              const info = status.providers?.[id];
              const configured = info?.configured;
              const active = activeProvider === id;

              return (
                <button
                  key={id}
                  type="button"
                  role="tab"
                  aria-selected={active}
                  className={
                    `llm-toggle-btn${active ? ' active' : ''}` +
                    (configured ? '' : ' unconfigured')
                  }
                  title={
                    configured
                      ? `Use ${info?.label || label}`
                      : `${info?.label || label} needs ${info?.key_name || 'its API key'} in the backend .env`
                  }
                  onClick={() => handleProvider(id)}
                  disabled={selecting}
                >
                  {configured ? label : `${label} 🔒`}
                </button>
              );
            })}
          </div>

          <select
            className="llm-select"
            aria-label="LLM model"
            value={status.active_model || ''}
            onChange={(e) => handleModel(e.target.value)}
            disabled={selecting || selectOptions.length === 0}
            title={status.active_model || 'Select model'}
          >
            {selectOptions.map((m) => (
              <option key={m.id} value={m.id} title={m.id}>
                {(m.label || m.id) + (SPEED_SUFFIX[m.speed] || '')}
              </option>
            ))}
          </select>

          <button
            type="button"
            className="llm-refresh"
            onClick={handleRefresh}
            disabled={refreshing || selecting}
            title="Fetch the live model list from the provider (NVIDIA NIM /models)"
          >
            {refreshing ? '…' : '⟳'}
          </button>

          <div className="llm-meta">
            <span
              className={`llm-dot ${isLive ? 'live' : 'catalog'}`}
              title={isLive ? 'Live provider list' : 'Curated catalog'}
            />
            <span title="Automatic fallback chain for this provider">
              {selecting
                ? 'Switching…'
                : `Fallback: ${spareCount} spare${spareCount === 1 ? '' : 's'}${
                    crossProvider ? ' + other provider' : ''
                  }`}
            </span>
          </div>
        </>
      )}

      {notice && status && (
        <div className="llm-hint" role="status">
          {notice}
        </div>
      )}
    </div>
  );
}
