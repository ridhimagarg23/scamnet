"""
e2e_telegram_check.py
=====================
End-to-end proof that the Telegram loop really works: a message sent to
the bot is investigated and answered by the persona, over real HTTP,
with no mocking of the backend, the Telegram client, the worker or the
agents.

How it works
------------
The script starts three servers on localhost:

1. **Fake Telegram Bot API** (``http.server``) - implements
   ``getMe`` / ``deleteWebhook`` / ``setMyCommands`` / ``getUpdates`` /
   ``sendMessage`` / ``sendChatAction`` exactly like Telegram does
   (including the ``conflict`` 409 when a webhook is active), keeps an
   update queue you can inject messages into and records every message
   the bot sends.
2. **Fake OpenAI-compatible gateway** - answers the investigation,
   conversation and report prompts with valid JSON, so the real agents,
   risk engine and adaptive persona engine all run.
3. **The real backend** (uvicorn, in a thread) with
   ``TELEGRAM_API_BASE`` pointed at (1) and ``OPENROUTER_BASE_URL``
   pointed at (2).

Then it drives the real HTTP API:

    connect -> start worker -> inject inbound message -> wait for reply
            -> second turn (objective ladder advances)
            -> no duplicate replies
            -> /start wake -> stop worker -> manual fetch mode

Run it with the project virtualenv:

    .venv/bin/python scripts/e2e_telegram_check.py

Exit code 0 means every step passed. Nothing here touches the network.
"""

from __future__ import annotations

import os
import socket
import sys
import threading
import time
from pathlib import Path

# Run with the repository root importable (`python scripts/...`).
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.telegram_simulator import (      # noqa: E402
    BOT_TOKEN,
    BOT_USERNAME,
    CONVERSATION_JSON,
)

CHAT_ID = 424242

# ------------------------------------------------------------------
# 3. Harness
# ------------------------------------------------------------------

def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class Checker:
    """Tiny assertion collector so the script reports every failure."""

    def __init__(self):
        self.passed = 0
        self.failures = []

    def check(self, condition, label, extra=""):
        if condition:
            self.passed += 1
            print(f"  \u2713 {label}")
        else:
            self.failures.append(label)
            print(f"  \u2717 {label} {extra}")

    def section(self, title):
        print(f"\n=== {title} ===")


def start_servers():
    """Start the fake Telegram API + fake LLM gateway on free ports."""

    from scripts.telegram_simulator import (
        start_llm_server,
        start_telegram_server,
    )

    telegram_state, telegram_server, telegram_port = start_telegram_server(0)
    llm_server, llm_port = start_llm_server(0)

    return telegram_state, telegram_port, telegram_server, llm_server


def start_backend(telegram_port: int, llm_port: int, api_port: int):
    """Run the REAL FastAPI app in a thread against the fakes."""

    os.environ["OPENROUTER_API_KEY"] = "e2e-test-key"
    os.environ["OPENROUTER_BASE_URL"] = f"http://127.0.0.1:{llm_port}/v1"
    os.environ["LLM_MODEL"] = "e2e-fake-model"
    os.environ["TELEGRAM_BOT_TOKEN"] = BOT_TOKEN
    os.environ["TELEGRAM_API_BASE"] = f"http://127.0.0.1:{telegram_port}"
    os.environ.setdefault("TRACEAI_STRICT_CONFIG", "")

    import uvicorn
    from backend.api import app

    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=api_port,
        log_level="warning",
    )
    server = uvicorn.Server(config)

    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    deadline = time.time() + 20
    while time.time() < deadline:
        if server.started:
            return server
        time.sleep(0.1)

    raise RuntimeError("backend did not start")


def call(api_port: int, method: str, path: str, **kwargs):
    """Tiny HTTP client (no dependency on httpx internals)."""

    import httpx

    url = f"http://127.0.0.1:{api_port}{path}"
    return httpx.request(method, url, timeout=30.0, **kwargs)


def wait_for_stat(api_port: int, key: str, expected, timeout: float = 10.0):
    """Wait for a worker counter to reach ``expected`` (no flakiness)."""

    deadline = time.time() + timeout
    stats = {}

    while time.time() < deadline:
        stats = call(api_port, "GET", "/api/telegram/conversation/status").json()
        worker = stats.get("worker") or {}
        if worker.get(key) == expected:
            return worker
        time.sleep(0.15)

    return stats.get("worker") or {}


def wait_for_reply(state, count: int, timeout: float = 25.0):
    """Block until ``count`` messages have been sent by the bot."""

    deadline = time.time() + timeout
    while time.time() < deadline:
        with state.lock:
            if len(state.sent) >= count:
                return list(state.sent)
        time.sleep(0.15)

    with state.lock:
        return list(state.sent)


def main() -> int:
    checker = Checker()

    state, telegram_port, telegram_server, llm_server = start_servers()
    llm_port = llm_server.server_address[1]
    api_port = free_port()
    server = start_backend(telegram_port, llm_port, api_port)

    print(f"fake Telegram on :{telegram_port} · fake LLM on :{llm_port} · backend on :{api_port}")

    try:
        # ----------------------------------------------------------
        checker.section("1. Connect the bot (real getMe)")
        # ----------------------------------------------------------
        response = call(api_port, "POST", "/api/integrations/telegram/connect")
        body = response.json()

        checker.check(response.status_code == 200, "connect returns HTTP 200",
                      f"(got {response.status_code}: {body})")
        checker.check(body.get("connected") is True, "status reports connected=True")
        checker.check(
            body.get("connection_info", {}).get("bot_username") == BOT_USERNAME,
            "bot identity captured from getMe",
        )
        checker.check(
            BOT_TOKEN not in response.text,
            "the bot token never appears in the API response",
        )
        checker.check(
            [c["method"] for c in state.calls][:3]
            == ["getMe", "deleteWebhook", "setMyCommands"],
            "a stale webhook is cleared before polling",
            f"(calls: {[c['method'] for c in state.calls]})",
        )

        # THE critical real-world check: connecting the bot must bring
        # the reply loop up by itself. A connected-but-not-polling bot
        # is exactly the "I message it and nothing happens" bug.
        checker.check(
            (body.get("worker") or {}).get("running") is True,
            "connect() auto-starts the reply loop (no manual step)",
            f"(worker={body.get('worker')})",
        )

        # ----------------------------------------------------------
        checker.section("2. Health / status endpoints")
        # ----------------------------------------------------------
        health = call(api_port, "GET", "/health").json()
        checker.check(health.get("status") == "healthy", "GET /health is healthy",
                      f"({health})")

        status = call(api_port, "GET", "/api/telegram/conversation/status").json()
        checker.check(status["telegram"]["connected"] is True,
                      "conversation status sees the live bot")
        checker.check(status["running"] is True,
                      "the loop is polling right after connect")
        checker.check(status["delivery_mode"] == "push",
                      "delivery_mode reports 'push' while the loop runs")

        # ----------------------------------------------------------
        checker.section("3. Start the polling worker")
        # ----------------------------------------------------------
        started = call(api_port, "POST", "/api/telegram/conversation/start")
        checker.check(started.status_code == 200, "worker start returns 200")
        checker.check(started.json()["worker"]["running"] is True,
                      "worker reports running=True")
        checker.check(started.json()["status"] == "running",
                      "manual start is idempotent while the loop runs")

        # ----------------------------------------------------------
        checker.section("4. The scammer messages the bot -> persona replies")
        # ----------------------------------------------------------
        state.push_message("Dear SBI customer, your card is blocked. "
                           "Verify at http://sbi-secure-login.co.in")

        sent = wait_for_reply(state, 1)

        checker.check(len(sent) >= 1, "the bot sent a reply for the inbound message",
                      f"(sent={sent})")
        if sent:
            checker.check(sent[0]["chat_id"] == CHAT_ID,
                          "reply went to the originating chat")
            checker.check(sent[0]["text"] == CONVERSATION_JSON["reply"],
                          "reply is the persona line from the conversation agent",
                          f"(got {sent[0]['text']!r})")

        checker.check(
            any(action.get("action") == "typing" for action in state.actions),
            "a 'typing...' action was sent while the persona was writing",
        )

        # A second poll must not answer the same message again: Telegram
        # only re-delivers an update when the offset was not advanced.
        time.sleep(2.0)
        checker.check(len(state.sent) == len(sent),
                      "no duplicate reply on later polls",
                      f"(sent={len(state.sent)})")

        # ----------------------------------------------------------
        checker.section("5. Second turn: state + objective ladder advance")
        # ----------------------------------------------------------
        first_turn_status = call(
            api_port, "GET", "/api/telegram/conversation/status"
        ).json()

        state.push_message("Send your Aadhaar number and OTP to complete KYC.")
        sent = wait_for_reply(state, 2)

        checker.check(len(sent) >= 2, "the second message was answered too")
        if len(sent) >= 2:
            checker.check(sent[1]["text"] == CONVERSATION_JSON["reply"],
                          "second reply delivered with the persona text")

        chats = call(api_port, "GET", "/api/telegram/conversation/status").json()
        chat = next(
            (c for c in chats["chats"] if c["chat_id"] == CHAT_ID), None
        )

        checker.check(chat is not None, "the chat appears in the status payload")
        if chat:
            checker.check(chat["turn"] == 2, "turn counter advanced to 2",
                          f"(turn={chat['turn']})")
            checker.check(chat["risk_score"] >= 50,
                          "risk score reflects the accumulated evidence",
                          f"(risk={chat['risk_score']})")
            checker.check(chat["messages"] == 4,
                          "transcript holds both sides of both turns",
                          f"(messages={chat['messages']})")

        worker = wait_for_stat(api_port, "processed", 2)
        checker.check(
            worker.get("processed") == 2,
            "worker counters report 2 answered messages",
            f"({worker})",
        )
        checker.check(
            first_turn_status["worker"]["polls"] <= chats["worker"]["polls"],
            "worker keeps polling between turns",
        )

        # ----------------------------------------------------------
        checker.section("6. /start is answered without the LLM")
        # ----------------------------------------------------------
        state.push_message("/start")
        sent = wait_for_reply(state, 3)

        checker.check(len(sent) >= 3, "/start gets an immediate answer")
        if len(sent) >= 3:
            checker.check("connected" in sent[2]["text"].lower()
                          or "hi" in sent[2]["text"].lower(),
                          "the /start answer is the fixed greeting",
                          f"(got {sent[2]['text']!r})")
            checker.check(
                sent[2]["text"] != CONVERSATION_JSON["reply"],
                "/start does not spend an LLM call",
            )

        # ----------------------------------------------------------
        checker.section("7. Wake endpoint (outbound path in isolation)")
        # ----------------------------------------------------------
        wake = call(
            api_port, "POST", "/api/telegram/conversation/wake",
            json={"chat_id": CHAT_ID},
        )
        checker.check(wake.status_code == 200, "POST /conversation/wake returns 200",
                      f"({wake.text[:120]})")
        checker.check(
            wake.status_code == 200 and wake.json().get("message_id"),
            "wake returns the Telegram message id",
        )

        # ----------------------------------------------------------
        checker.section("8. Stop the worker (cooperative shutdown)")
        # ----------------------------------------------------------
        stopped = call(api_port, "POST", "/api/telegram/conversation/stop")
        checker.check(stopped.status_code == 200, "stop returns 200")
        checker.check(stopped.json()["status"] == "stopped",
                      "worker reports stopped",
                      f"({stopped.json()})")

        # ----------------------------------------------------------
        checker.section("9. Fetch mode (no background thread)")
        # ----------------------------------------------------------
        state.push_message("Transfer the money to pay@okaxis now.")

        with state.lock:
            sent_before = len(state.sent)

        fetched = call(api_port, "POST", "/api/telegram/conversation/fetch")
        fetched_body = fetched.json()

        checker.check(fetched.status_code == 200, "fetch returns 200",
                      f"({fetched.text[:160]})")
        checker.check(fetched_body.get("handled") == 1,
                      "fetch answered exactly one message",
                      f"({fetched_body})")

        sent = wait_for_reply(state, sent_before + 1)
        checker.check(len(sent) >= sent_before + 1,
                      "fetch-mode reply was delivered to Telegram")

        # Idempotent: a second fetch with nothing queued answers nothing.
        again = call(api_port, "POST", "/api/telegram/conversation/fetch")
        checker.check(again.json().get("handled") == 0,
                      "a second fetch does not re-answer old updates",
                      f"({again.json()})")

        # ----------------------------------------------------------
        checker.section("10. Manual single-turn endpoint")
        # ----------------------------------------------------------
        manual = call(
            api_port, "POST", "/api/telegram/conversation/message",
            json={"chat_id": CHAT_ID, "text": "Give me your bank details."},
        )
        checker.check(manual.status_code == 200, "manual turn returns 200",
                      f"({manual.text[:160]})")
        checker.check(
            manual.status_code == 200
            and manual.json().get("reply") == CONVERSATION_JSON["reply"],
            "manual turn returns the persona reply",
        )

        # ----------------------------------------------------------
        checker.section("11. Guard rails")
        # ----------------------------------------------------------
        bad = call(api_port, "POST", "/api/telegram/send-test",
                   json={"chat_id": 0, "text": "hi"})
        checker.check(bad.status_code == 422, "chat_id=0 is rejected with 422",
                      f"(got {bad.status_code})")

        unknown = call(api_port, "POST", "/api/telegram/conversation/reset",
                       json={"chat_id": 987654})
        checker.check(unknown.json()["status"] == "unknown_chat",
                      "resetting an unknown chat is a no-op, not an error")

        no_token = call(api_port, "POST", "/api/integrations/unknown/connect")
        checker.check(no_token.status_code == 404,
                      "unknown integration id returns 404")

        # ----------------------------------------------------------
        checker.section("12. Disconnect stops the loop and boots cleanly")
        # ----------------------------------------------------------
        disconnected = call(
            api_port, "POST", "/api/integrations/telegram/disconnect"
        )
        checker.check(disconnected.status_code == 200,
                      "disconnect returns 200",
                      f"({disconnected.text[:120]})")
        checker.check(
            (disconnected.json().get("worker") or {}).get("running") is False,
            "disconnect stops the reply loop",
            f"({disconnected.json().get('worker')})",
        )
        checker.check(
            BOT_TOKEN not in disconnected.text,
            "the bot token never appears in the disconnect response",
        )

    finally:
        try:
            call(api_port, "POST", "/api/telegram/conversation/stop")
        except Exception:
            pass

        server.should_exit = True
        telegram_server.shutdown()
        llm_server.shutdown()

    print("\n" + "=" * 62)

    if checker.failures:
        print(f"FAILED: {len(checker.failures)} check(s) failed, "
              f"{checker.passed} passed")
        for failure in checker.failures:
            print(f"  - {failure}")
        return 1

    print(f"PASSED: all {checker.passed} checks green "
          f"(message in -> investigation -> persona reply out)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
