# 🐞 TraceAI / SCAMNET — Code Audit & Bug Report

**Date:** 2026-09-13 · **Scope:** whole repository (`ridhimagarg23/scamnet`, branch `arena/01a09c0f-scamnet`)
**How to reproduce:** `pip install -r requirements.txt`, then
`OPENROUTER_API_KEY=test-key python -m unittest discover -s tests` (131 tests, all green after the fixes).

---

## TL;DR — why "Gmail / Drive kuch connect nahi hota"

Two independent bugs caused it, and both are fixed:

| # | Root cause | Where | Status |
|---|---|---|---|
| **A1** | Google Drive + Google Sheets were **honest stubs**: `connect_implemented = False` and `connect()` always raised `IntegrationNotImplementedError` → the API answered **HTTP 501 “Setup required”** for *every* Connect click, no matter what credentials the user configured. | `integrations/google_drive/client.py`, `integrations/google_sheets/client.py` | ✅ Real OAuth + API flows implemented |
| **A2** | **Gmail did not exist at all** in the integration layer (no client, no registry entry, no UI card). | `integrations/` | ✅ `integrations/gmail/` added (real Gmail API) |
| **A3** | The dashboard called the backend at **`http://127.0.0.1:8001`** (localhost) or a **hard-coded Railway URL** (every other host), while the Connected-Apps modal used the Next.js `/backend-api` proxy. Two halves → two different backends → on any host that is not localhost neither the dashboard nor the integrations could ever reach the API. | `frontend/app/page.jsx`, `frontend/lib/integrations.js`, `frontend/next.config.mjs` | ✅ Single same-origin API base (`frontend/lib/api.js`) |
| **A4** | `config.py` **refused to import without `OPENROUTER_API_KEY`**, so a server whose only purpose was to test the Google/Telegram setup crashed at boot — `/api/integrations` never answered. | `config.py` | ✅ Warns + `degraded` health, `503` only on LLM endpoints |

The other 20 issues found are listed below with severity, evidence (file:line) and fix status.

---

## 🔴 Critical

### 1. Google Drive / Sheets could never connect (“Setup required” forever)
* **Where:** `integrations/google_drive/client.py:62`, `integrations/google_sheets/client.py:60`
* **Symptom:** clicking **Connect** always returned HTTP 501 → UI badge *“Setup required”*, even with `GOOGLE_DRIVE_CREDENTIALS_FILE` correctly set.
* **Cause:** `connect_implemented = False`; `connect()` raised `IntegrationNotImplementedError`; `check_health()` returned `False` unconditionally. The docstrings admitted it ("NO real Drive API calls are made yet").
* **Fix:** new shared base `integrations/google_api.py` (service-account *and* authorized-user credentials, token refresh via an injectable HTTP transport, sanitised `GoogleAPIError`, TTL-cached health). Real implementations:
  * **Drive** – `about.get` + optional destination-folder verification on connect; `upload_report()` creates the markdown artefact and **updates the same file** on later turns.
  * **Sheets** – verifies a configured spreadsheet (or *creates* one when no id is set), guarantees the `Evidence` worksheet, `append_rows()` with automatic header row, `upsert_case()` keyed by case id.
* **Verified by:** `tests/test_google_integrations.py` (34 tests, stub transport, no network).

### 2. Gmail integration missing entirely
* **Where:** registry `integrations/__init__.py`, UI `frontend/lib/integrations.js`
* **Symptom:** “Gmail” could not be connected because it was never implemented.
* **Fix:** `integrations/gmail/client.py` (`users.getProfile` verification, `list_messages()`, `get_message()`, `send_email()`), registered as a 4th integration, plus a Gmail card in the modal. Gmail requires an **authorized-user** credentials file — a service account needs Workspace domain-wide delegation (documented in the card’s setup instructions).

### 3. Frontend talked to two different backends
* **Where:** `frontend/app/page.jsx:62-80` (removed) vs `frontend/lib/integrations.js:16`
* **Symptom:** on Vercel/preview/LAN, `/analyze` went to a hard-coded Railway host and `/api/integrations` to `/backend-api` → which the Next proxy forwarded to `http://127.0.0.1:8001` (a non-existent host in the cloud) → “Cannot reach the SCAMNET backend”, so **no app could connect**.
* **Fix:** new `frontend/lib/api.js` — one `apiUrl()` helper used by every call site, defaulting to the same-origin `/backend-api` proxy; `NEXT_PUBLIC_API_URL` remains an explicit override. Verified with a production build (`npm run build` ✅).

### 4. Missing `OPENROUTER_API_KEY` crashed the whole server
* **Where:** `config.py:...` (old `raise ValueError("OPENROUTER_API_KEY not found in .env")`)
* **Symptom:** `uvicorn backend.api:app` died at import → even `/health` and `/api/integrations` were unreachable; the Connect buttons had nothing to talk to.
* **Fix:** boot always succeeds; `settings.llm_configured` drives an explicit HTTP **503** on `/analyze` and on the Telegram conversation endpoints, `GET /health` reports `{"status": "degraded", "llm_configured": false}`. `TRACEAI_STRICT_CONFIG=1` restores fail-fast for those who want it. (`LLMClient` now raises `LLMNotConfiguredError`.)

---

## 🟠 High

### 5. Hard-coded CORS allow-list (one origin) broke every other frontend host
* **Where:** `backend/api.py:104-112`
* **Fix:** origins come from `settings.CORS_ALLOW_ORIGINS` (`CORS_ALLOW_ORIGINS` env, defaults include the local dev origins); README no longer tells operators to edit code.

### 6. `PromptLoader` resolved `prompts/` against the process CWD
* **Where:** `tools/prompt_loader.py:22` (`PROMPTS_DIR = Path("prompts")`)
* **Symptom:** every agent failed with `FileNotFoundError: Prompt not found` when uvicorn/CLI was started from any directory other than the repo root (e.g. `uvicorn backend.api:app` through a service manager with `WorkingDirectory=/`).
* **Fix:** path is now resolved from `__file__` (same pattern as `MemoryManager`). Regression test: `tests/test_config_and_resilience.py::TestPromptLoaderPaths`.

### 7. Connect outcomes were unusable in the UI (no setup guidance)
* **Where:** `frontend/components/IntegrationsModal.jsx`
* **Symptom:** cards showed “Setup required” but never the server’s `setup_instructions`, so an operator could not tell *which* env var to set; `connection_info` (bot name, Google account) was fetched but never displayed.
* **Fix:** not-connected cards render the server-provided setup instructions in a collapsible section; connected cards show the secret-free `connection_info` and a **Disconnect** button (new `POST /api/integrations/{id}/disconnect`).

### 8. Failed Google credentials produced no useful status
* **Where:** Google clients’ `configuration_issues()`
* **Fix:** validity now covers file-missing, unreadable, non-JSON, wrong `type`, missing `private_key` / `refresh_token`, etc. Messages are actionable **and never contain the path or secret values** (the old code only checked `os.path.isfile`).

---

## 🟡 Medium

| # | Issue | Where | Fix |
|---|---|---|---|
| 9 | `sessions` dict grew without bound (memory leak on a public deployment) | `backend/api.py:151` | FIFO cap `MAX_SESSIONS = 200`, oldest evicted |
| 10 | “Report Ready” progress step was **never** marked `done` (only `current`), so the stepper lied about the state | `backend/api.py` `build_progress()` | report existence is now part of the step state |
| 11 | Integration status/report never exported anywhere — Drive/Sheets were decorative | `backend/api.py` `/analyze` | new `tools/evidence_archive.py` pushes the report to Drive (create → update) and upserts the case row in Sheets, best-effort, per-app status returned as `archive` in the response |
| 12 | Google API calls had no shared error sanitisation | new `integrations/google_api.py` | `GoogleAPIError` + regex/secret-value redaction (`ya29.`, `1//`, `GOCSPX-`, credentials-file path) |
| 13 | `.env.example` documented Sheets/Drive as “not implemented yet” | `.env.example` | rewritten: shared `GOOGLE_CREDENTIALS_FILE`, Gmail, CORS, strict-config |
| 14 | README marketed the platform as “production-ready” and never mentioned the integration layer, its env vars or its endpoints | `README.md` | integration section + env vars + endpoint table + honest limitations |
| 15 | `integrations/google_sheets` and `integrations/google_drive` `__init__.py` did not re-export their config constants, so consumers could not import them | `integrations/*/__init__.py` | constants exported |

## 🟢 Low (lint / dead code)

| # | Issue | Fix |
|---|---|---|
| 16 | `backend/api.py`: unused `fastapi.Response` import | removed |
| 17 | `backend/telegram_routes.py`: unused `IntegrationNotConfiguredError` import | removed |
| 18 | `tools/telegram_conversation_worker.py`: useless `global _WORKER` in `stop_worker()` | removed |
| 19 | `integrations/google_sheets/client.py`: stray `import json` + unused `_json_dumps()` debug helper | removed |
| 20 | `frontend/app/page.jsx`: now-unused `useCallback` after the API-base refactor | removed |

Whole tree is now **pyflakes-clean** (`pyflakes integrations tools backend llm config.py tests app.py`).

---

## ✅ What was verified

* `OPENROUTER_API_KEY=test-key python -m unittest discover -s tests` → **131 tests, OK** (was 84).
* New `tests/test_google_integrations.py` (34 tests) runs the *complete* Google flow offline — credentials JSON → token refresh → authorized call → connect / health / upload / upsert / send — and pins the honest-status contract (401/403/404 → sanitised error, never `connected=True`, never a leaked token or path).
* `cd frontend && npm run build` → compiled successfully, lint/type check clean.
* Manual smoke test with a stub transport: Drive connect + upload, Sheets connect + append (`Evidence!A1:M3`), Gmail connect — the same scenarios are pinned in `tests/test_google_integrations.py`.
* Live server check: with a well-formed (fake) Google credentials file the server reports `configured=true, available=true` for Drive/Sheets/Gmail and `Connect` performs a real HTTPS attempt against Google (`502 connection_failed` with a sanitised message when it fails) — previously it answered `501 setup_required` without ever trying.

## 🧭 Still open / by design (not bugs)

* **In-memory sessions and chats** — state resets on restart (documented).
* **`database/threat_memory.json`** — per-run archive on ephemeral hosts.
* **Gmail with a service account** — Google requires Workspace domain-wide delegation; SCAMNET reports the honest failure instead of pretending.
* **Static personas / stub UI actions** (History, Saved Cases) — future features, not broken code.
