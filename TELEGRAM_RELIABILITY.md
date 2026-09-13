# Telegram bot — reliability audit & fixes

> Goal: **a user messages the bot and gets a proper reply, every time.**
> Everything below was found by reading the real inbound → reply path,
> then proven twice: 175 unit/integration tests and a 45-check
> end-to-end run over real HTTP (message in → investigation → persona
> reply out).

---

## The headline bug (why the bot never answered)

**Nothing ever started the reply loop.**

The polling worker only ran if someone called
`POST /api/telegram/conversation/start` — no UI called it, boot never
started it, and `POST /api/integrations/telegram/connect` did not either.
So the dashboard could say *“Telegram: Connected”* while the bot read
nothing: exactly the *“I message it and nothing happens”* complaint.

**Fixed.** The loop now comes up by itself:

| Trigger | Behaviour |
|---|---|
| Backend boots with a working `TELEGRAM_BOT_TOKEN` | background thread verifies the token (`getMe`) and starts polling |
| `POST /api/integrations/telegram/connect` succeeds | loop starts immediately; the response reports `worker.running` |
| `POST /api/integrations/telegram/disconnect` | loop stops (a disconnected bot must not keep polling) |
| `TELEGRAM_AUTO_START_WORKER=0` | opt out and control it manually |

---

## Other bugs found and fixed

### Could lose or block messages

1. **A registered webhook silently blocks polling.**
   Telegram forbids `getUpdates` while a webhook exists (HTTP 409).
   If the bot ever had one, the loop failed forever. `connect()` now
   calls `deleteWebhook` *before* declaring the bot connected, refuses
   to report "connected" if it cannot clear it, and the status exposes
   `webhook_cleared`.
2. **`/start` had no handler.** `_handle_start_command` did not exist, so
   the worker logged `AttributeError` and stayed silent for the very
   first message Telegram sends when someone opens the bot. `/start`,
   `/START` and `/start@your_bot` are now answered with a fixed
   greeting — **no LLM needed**, and it never opens a case.
3. **Fetch mode was dead after a stop.** `run_once()` checked the
   worker-wide stop event, so once the operator stopped the loop, every
   later `POST /conversation/fetch` answered nothing. It now takes an
   explicit abort callback (the background loop passes its stop event;
   the fetch endpoint always answers what it finds).
4. **Overlapping polls could split updates.** The polling thread and a
   fetch call could both call `getUpdates`; Telegram answers the older
   request with 409 and that update is lost. All polls now share one
   process-wide lock, and `start()` refuses to run during a fetch.
5. **A revoked token made the loop hammer Telegram forever.** After 5
   consecutive 401/403 answers the loop stops loudly with
   `stopped_reason: telegram_authorization_failed`.

### Could silently drop a reply

6. **Length checks used Python characters, Telegram counts UTF-16 code
   units.** A 3000-emoji reply passed validation and was rejected by
   Telegram (HTTP 400) — reply lost. Replies are now measured and
   truncated in Telegram's own unit (`truncate_for_telegram`).
7. **No rate-limit handling.** A `429 Too Many Requests` reply was
   dropped permanently. Sends now retry **once** after Telegram's own
   `retry_after` (capped at 5s).
8. **No "typing…" indicator.** A slow LLM turn looked like a dead bot.
   `sendChatAction` is now sent before the persona writes (best effort).
9. **Thread-unsafe archive.** `MemoryManager.save()` did a non-atomic
   read-modify-write shared by the API and the worker thread, so
   concurrent saves silently dropped records. It now uses one lock, an
   atomic replace, and preserves a corrupt file instead of crashing.

### Observability

10. `GET /api/telegram/conversation/status` could 500 on an
    integration lacking `get_connection_info`; it is defensive now and
    reports far more: `delivery_mode`, worker counters (polls, answered,
    greetings, failed), `last_poll_at`, `last_delivery`, recent errors.
11. Worker heartbeats are logged every 60 polls, and the first poll of a
    fresh process reports a pending backlog instead of guessing.
12. `setMyCommands` publishes `/start` so Telegram shows a command menu
    — a guaranteed liveness probe for the operator.

---

## New endpoints

| Endpoint | Why it exists |
|---|---|
| `POST /api/telegram/conversation/fetch` | Poll **once** and answer everything — the same brain for hosts that freeze background threads (cron / uptime pinger). Never double-answers. |
| `POST /api/telegram/conversation/wake` | Send the liveness greeting to one chat — proves the outbound path with no LLM and no inbound message. |
| `GET /api/telegram/conversation/commands` | Documents what the bot understands. |

`POST /conversation/message` with `/start` is answered without the LLM,
and `/conversation/start` + `/conversation/fetch` no longer require the
LLM key (they answer `/start` and record per-message errors honestly).

---

## Dashboard

The Telegram card in **Connected Apps** now shows the loop's honest
state — running (with poll/answer counters and the last error) or
stopped — plus **Start loop**, **Stop loop**, **Recheck loop** and a
**Send test hello** box (chat id + one click). Nothing is invented
locally: every value comes from the server.

---

## How to use it with a real bot

```bash
# 1. a bot from @BotFather
TELEGRAM_BOT_TOKEN=123456:ABC-your-token        # in the server-side .env
# 2. restart the backend - the loop starts by itself
# 3. optional: Connect in the dashboard; the modal shows the loop running
# 4. message the bot from Telegram - it investigates and replies
```

Hosts that suspend the process (serverless): leave the loop stopped and
call `POST /api/telegram/conversation/fetch` from cron / an uptime
pinger. One bot token must poll **one** process — with several replicas
use fetch mode, not `/conversation/start`.

---

## Verification (all reproducible)

```bash
OPENROUTER_API_KEY=test-key .venv/bin/python -m unittest discover -s tests   # Ran 175 tests, OK
.venv/bin/pyflakes integrations tools backend llm config.py tests app.py scripts  # clean
.venv/bin/python scripts/e2e_telegram_check.py    # PASSED: all 45 checks green
cd frontend && npm run build                      # Compiled successfully
```

The end-to-end script starts a fake Telegram Bot API (faithful
`getUpdates` offset + 409-on-webhook semantics), a fake OpenAI-compatible
gateway, and the **real** backend, then drives real HTTP: connect →
auto-start → message in → persona reply out → second turn (objective
ladder + risk accumulation) → `/start` (no LLM) → wake → stop → fetch
mode → single-turn endpoint → guard rails → disconnect.

**Live demo run in this workspace:** message
*“Sir aapka KYC pending hai, is liye Aadhaar daaliye …”* was pushed into
the bot, investigated (`Banking Phishing`, risk **87 HIGH**) and answered
from chat 424242 — with a `typing…` action first and the reply
*“Sir, I did not understand. How do I verify my account?”* delivered by
`sendMessage`.

### Test coverage added (`tests/test_telegram_delivery.py`, 44 tests)

Webhook cleared on connect · honest failure when it cannot be ·
`/start` in all three spellings answered without the LLM and without
opening a case · UTF-16 truncation (incl. 3000 emoji) · 429 retry once ·
non-429 not retried · revoked token stops the loop · typing action ·
pending backlog answered (incl. an automatic `/start`) · fetch survives
`stop()` · no double answers · auto-start on connect · auto-stop on
disconnect · auto-start can be disabled · boot bootstrap (silent without
a token, connects + starts when configured) · concurrent archive writes
never lose records · corrupt archive preserved.

---

## Local testing without a bot token

`scripts/telegram_simulator.py` runs a faithful fake Bot API + fake LLM
gateway, and the running workspace demo is already pointed at it: open
**Connected Apps → Telegram** and you will see the loop live. Push a
message the way a scammer would:

```bash
curl -X POST http://127.0.0.1:8099/_push -H 'Content-Type: application/json' \
     -d '{"chat_id": 424242, "text": "Your card is blocked, verify now"}'
curl http://127.0.0.1:8099/_state      # what the bot answered
```
