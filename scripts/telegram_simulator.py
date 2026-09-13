"""
telegram_simulator.py
=====================
A local stand-in for Telegram's Bot API plus a fake OpenAI-compatible
gateway, so the whole Telegram loop can be exercised without a real bot
token and without spending LLM credits.

Use it to watch the bot answer messages from the dashboard:

    # terminal 1 - the fakes (prints the env vars to export)
    .venv/bin/python scripts/telegram_simulator.py

    # terminal 2 - the real backend pointed at the fakes
    TELEGRAM_BOT_TOKEN="99999:SIMULATOR" \
    TELEGRAM_API_BASE="http://127.0.0.1:8099" \
    OPENROUTER_API_KEY="simulator-key" \
    OPENROUTER_BASE_URL="http://127.0.0.1:8098/v1" \
    .venv/bin/uvicorn backend.api:app --port 8001

    # terminal 3 - the scammer writes to the bot
    curl -X POST http://127.0.0.1:8099/_push \
         -H 'Content-Type: application/json' \
         -d '{"chat_id": 424242, "text": "Your card is blocked, verify now"}'

    # ...and see what the bot answered:
    curl http://127.0.0.1:8099/_state

The Bot API surface is faithful where it matters:

* ``getMe`` / ``deleteWebhook`` / ``setMyCommands`` / ``getUpdates`` /
  ``sendMessage`` / ``sendChatAction``;
* a registered webhook makes ``getUpdates`` answer HTTP 409 exactly like
  Telegram does (the failure mode that makes a bot look mute);
* ``getUpdates`` without an ``offset`` drains the queue; with an offset
  it only returns newer updates and re-delivers the rest next time -
  the same cursor semantics the real API has.

Nothing here is imported by the application: it exists for demos,
manual testing and ``scripts/e2e_telegram_check.py``.
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DEFAULT_TELEGRAM_PORT = 8099
DEFAULT_LLM_PORT = 8098

BOT_TOKEN = "99999:SIMULATOR-TOKEN"
BOT_USERNAME = "scamnet_simulator_bot"

# Valid JSON payloads for the three agents the pipeline runs.
INVESTIGATION_JSON = {
    "is_scam": True,
    "confidence": 92,
    "threat_type": "Banking Phishing",
    "summary": "Fake SBI verification portal used to harvest credentials.",
}

CONVERSATION_JSON = {
    "reply": "Sir, I did not understand. How do I verify my account?",
    "objective": "Collect official verification website",
    "expected_outcome": "Scammer sends the verification link.",
}

REPORT_JSON = {
    "title": "TraceAI Investigation Report - Banking Phishing",
    "markdown": "# Investigation Report\n\nRisk: HIGH\n",
}


# ------------------------------------------------------------------
# Fake Telegram Bot API
# ------------------------------------------------------------------

class FakeTelegramState:
    """In-memory stand-in for Telegram's update queue + outbox."""

    def __init__(self):
        self.lock = threading.Lock()
        self.updates = []          # raw Bot API updates waiting to be fetched
        self.sent = []             # sendMessage calls the bot made
        self.actions = []          # sendChatAction calls the bot made
        self.calls = []            # every Bot API method the bot called
        self.webhook_active = False
        self.next_update_id = 1000
        self.next_message_id = 5000
        self.fetch_offset = None   # what the bot asked for last

    def push_message(self, text, chat_id=424242):
        """Queue an inbound text message as Telegram would."""

        with self.lock:
            self.next_update_id += 1
            update_id = self.next_update_id
            self.next_message_id += 1

            self.updates.append({
                "update_id": update_id,
                "message": {
                    "message_id": self.next_message_id,
                    "from": {
                        "id": chat_id,
                        "is_bot": False,
                        "first_name": "Scammer",
                        "username": "scammer_sim",
                        "language_code": "en",
                    },
                    "chat": {
                        "id": chat_id,
                        "first_name": "Scammer",
                        "username": "scammer_sim",
                        "type": "private",
                    },
                    "date": int(time.time()),
                    "text": text,
                },
            })

            return update_id

    def drain(self, offset):
        """Apply Telegram's offset semantics to the queue."""

        with self.lock:
            self.fetch_offset = offset

            if offset is None:
                batch = list(self.updates)
                self.updates = []
                return batch

            batch = [
                update for update in self.updates
                if update["update_id"] >= offset
            ]
            self.updates = [
                update for update in self.updates
                if update["update_id"] < offset
            ]
            return batch

    def wait_for_updates(self, timeout: float) -> None:
        """
        Block like real long polling does.

        Telegram holds a ``getUpdates`` request open for up to
        ``timeout`` seconds when the queue is empty, which is what keeps
        a polling loop from spinning. The simulator honours that: it
        waits in small steps and returns as soon as something arrives.
        """

        deadline = time.time() + max(0.0, float(timeout))

        while time.time() < deadline:
            with self.lock:
                if self.updates:
                    return
            time.sleep(0.05)


def make_fake_telegram_handler(state: FakeTelegramState, bot_token: str = BOT_TOKEN):
    """Build a Bot API handler bound to ``state``."""

    class Handler(BaseHTTPRequestHandler):

        protocol_version = "HTTP/1.1"

        def log_message(self, *args):  # keep the output readable
            return

        # --- helpers ---
        def _send_json(self, payload, status=200):
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _error(self, code, description, status=200):
            self._send_json(
                {"ok": False, "error_code": code, "description": description},
                status=status,
            )

        def _read_body(self):
            length = int(self.headers.get("Content-Length") or 0)
            if not length:
                return {}
            try:
                return json.loads(self.rfile.read(length) or b"{}")
            except ValueError:
                return {}

        def do_POST(self):
            path = self.path

            # Test-only helper: inject an inbound message.
            if path == "/_push":
                payload = self._read_body()
                update_id = state.push_message(
                    payload.get("text", ""),
                    payload.get("chat_id", 424242),
                )
                return self._send_json({"ok": True, "update_id": update_id})

            if not path.startswith(f"/bot{bot_token}/"):
                return self._error(404, "Not Found", status=404)

            method = path.rsplit("/", 1)[-1]
            payload = self._read_body()

            with state.lock:
                state.calls.append({"method": method, "payload": payload})

            if method == "getMe":
                return self._send_json({
                    "ok": True,
                    "result": {
                        "id": 99999,
                        "is_bot": True,
                        "first_name": "SCAMNET Simulator",
                        "username": BOT_USERNAME,
                    },
                })

            if method == "deleteWebhook":
                with state.lock:
                    state.webhook_active = False
                return self._send_json({"ok": True, "result": True})

            if method == "setWebhook":
                with state.lock:
                    state.webhook_active = True
                return self._send_json({"ok": True, "result": True})

            if method == "setMyCommands":
                return self._send_json({"ok": True, "result": True})

            if method == "getUpdates":
                # A real Telegram server refuses getUpdates while a
                # webhook is registered - the exact failure this project
                # used to hit (the bot silently never replied).
                if state.webhook_active:
                    return self._error(
                        409,
                        "Conflict: can't use getUpdates method while "
                        "webhook is active",
                        status=409,
                    )

                offset = payload.get("offset")

                with state.lock:
                    has_pending = bool(state.updates)

                # Long polling: hold the request open when idle, exactly
                # like Telegram (keeps the loop from busy-spinning).
                if not has_pending:
                    state.wait_for_updates(payload.get("timeout") or 0)

                return self._send_json({
                    "ok": True,
                    "result": state.drain(offset),
                })

            if method == "sendMessage":
                chat_id = payload.get("chat_id")
                text = payload.get("text") or ""
                with state.lock:
                    state.next_message_id += 1
                    message_id = state.next_message_id
                    state.sent.append({
                        "chat_id": chat_id,
                        "text": text,
                        "message_id": message_id,
                        "at": time.time(),
                    })
                return self._send_json({
                    "ok": True,
                    "result": {
                        "message_id": message_id,
                        "date": int(time.time()),
                        "chat": {"id": chat_id, "type": "private"},
                        "text": text,
                    },
                })

            if method == "sendChatAction":
                with state.lock:
                    state.actions.append(payload)
                return self._send_json({"ok": True, "result": True})

            return self._error(404, f"Unknown method {method}", status=404)

        def do_GET(self):
            # Test-only inspection endpoint (not part of the Bot API).
            if self.path == "/_state":
                with state.lock:
                    return self._send_json({
                        "sent": state.sent,
                        "actions": state.actions,
                        "calls": [call["method"] for call in state.calls],
                        "pending": len(state.updates),
                        "webhook_active": state.webhook_active,
                    })

            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()

    return Handler


# ------------------------------------------------------------------
# Fake OpenAI-compatible gateway
# ------------------------------------------------------------------

def make_fake_llm_handler():
    """A tiny /v1/chat/completions server for the three agents."""

    class Handler(BaseHTTPRequestHandler):

        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            return

        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            request = json.loads(self.rfile.read(length) or b"{}")

            prompt = ""
            for message in request.get("messages", []):
                prompt += str(message.get("content", ""))

            # The markers are unique per agent: the report and
            # conversation prompts BOTH contain "INVESTIGATION RESULT".
            if "HONEYPOT RESPONSE" in prompt:            # ReportAgent
                content = json.dumps(REPORT_JSON)
            elif "LATEST SCAMMER MESSAGE" in prompt:     # ConversationAgent
                content = json.dumps(CONVERSATION_JSON)
            elif "EXTRACTED ENTITIES" in prompt:         # InvestigationAgent
                content = json.dumps(INVESTIGATION_JSON)
            else:  # pragma: no cover - would mean a new agent was added
                content = json.dumps(INVESTIGATION_JSON)

            body = json.dumps({
                "id": "chatcmpl-simulator",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": "simulator-model",
                "choices": [{
                    "index": 0,
                    "message": {"role": "assistant", "content": content},
                    "finish_reason": "stop",
                }],
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 10,
                    "total_tokens": 20,
                },
            }).encode()

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return Handler


# ------------------------------------------------------------------
# Server helpers
# ------------------------------------------------------------------

def start_telegram_server(port: int = 0):
    """Start the fake Bot API. ``port=0`` picks a free port."""

    state = FakeTelegramState()

    server = ThreadingHTTPServer(
        ("127.0.0.1", port), make_fake_telegram_handler(state)
    )
    threading.Thread(target=server.serve_forever, daemon=True).start()

    return state, server, server.server_address[1]


def start_llm_server(port: int = 0):
    """Start the fake OpenAI-compatible gateway."""

    server = ThreadingHTTPServer(("127.0.0.1", port), make_fake_llm_handler())
    threading.Thread(target=server.serve_forever, daemon=True).start()

    return server, server.server_address[1]


def main() -> int:
    telegram_state, telegram_server, telegram_port = start_telegram_server(
        DEFAULT_TELEGRAM_PORT
    )
    _, llm_port = 0, DEFAULT_LLM_PORT
    llm_server, llm_port = start_llm_server(DEFAULT_LLM_PORT)

    print("Telegram simulator running")
    print("--------------------------")
    print(f"  Bot API      : http://127.0.0.1:{telegram_port}   (bot token {BOT_TOKEN})")
    print(f"  Fake LLM     : http://127.0.0.1:{llm_port}/v1")
    print()
    print("Point a real backend at it:")
    print()
    print(f'  TELEGRAM_BOT_TOKEN="{BOT_TOKEN}" \\')
    print(f'  TELEGRAM_API_BASE="http://127.0.0.1:{telegram_port}" \\')
    print('  OPENROUTER_API_KEY="simulator-key" \\')
    print(f'  OPENROUTER_BASE_URL="http://127.0.0.1:{llm_port}/v1" \\')
    print("  .venv/bin/uvicorn backend.api:app --port 8001")
    print()
    print("Then, in another terminal:")
    print()
    print(f"  curl -X POST http://127.0.0.1:{telegram_port}/_push \\")
    print("       -H 'Content-Type: application/json' \\")
    print('       -d \'{"chat_id": 424242, "text": "Your card is blocked"}\'')
    print(f"  curl http://127.0.0.1:{telegram_port}/_state")
    print()
    print("Ctrl+C to stop.")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nStopping simulator...")
    finally:
        telegram_server.shutdown()
        llm_server.shutdown()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
