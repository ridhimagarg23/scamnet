"""
run_all.py
==========
Start the WHOLE project with one command: FastAPI backend + Next.js
dashboard (and optionally the Streamlit UI), with proper shutdown on
Ctrl+C.

    python scripts/run_all.py                 # backend + dashboard
    python scripts/run_all.py --streamlit     # + Streamlit UI (port 8501)
    python scripts/run_all.py --no-frontend   # backend only
    python scripts/run_all.py --backend-port 9000 --frontend-port 4000

What it does for you
--------------------
* uses the project virtualenv when one exists (``.venv``), so the right
  dependencies are loaded no matter which Python started the script;
* checks the root ``.env`` and tells you exactly which keys are missing
  (it still starts - the backend runs in degraded mode without an LLM key);
* runs ``npm install`` in ``frontend/`` the first time (skip with
  ``--skip-install``);
* waits until each service actually answers before printing the URLs;
* streams every log line prefixed with ``[api]`` / ``[ui]`` / ``[streamlit]``;
* Ctrl+C (or a crash) stops every child process, including on Windows.

Nothing here is needed in production - hosting platforms start the
backend from the ``Procfile`` and the dashboard from ``frontend/``.
"""

from __future__ import annotations

import argparse
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
FRONTEND_DIR = REPO_ROOT / "frontend"

DEFAULT_BACKEND_PORT = 8001
DEFAULT_FRONTEND_PORT = 3000
DEFAULT_STREAMLIT_PORT = 8501

# Variables the backend needs to be fully functional (not to boot).
REQUIRED_KEYS = ("OPENROUTER_API_KEY", "TELEGRAM_BOT_TOKEN")

IS_WINDOWS = os.name == "nt"


# ------------------------------------------------------------------
# Small helpers
# ------------------------------------------------------------------

def venv_python() -> str:
    """Prefer the project virtualenv's interpreter when it exists."""

    candidates = [
        REPO_ROOT / ".venv" / ("Scripts" if IS_WINDOWS else "bin")
        / ("python.exe" if IS_WINDOWS else "python"),
        REPO_ROOT / "venv" / ("Scripts" if IS_WINDOWS else "bin")
        / ("python.exe" if IS_WINDOWS else "python"),
    ]

    for candidate in candidates:
        if candidate.exists():
            return str(candidate)

    return sys.executable


def npm_command() -> str:
    """npm is ``npm.cmd`` on Windows."""

    return "npm.cmd" if IS_WINDOWS else "npm"


def port_is_open(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket() as probe:
        probe.settimeout(0.5)
        return probe.connect_ex((host, port)) == 0


def read_env_file() -> dict:
    """Parse the root .env (values only, for the readiness report)."""

    values: dict = {}

    env_path = REPO_ROOT / ".env"

    if not env_path.exists():
        return values

    for line in env_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()

        if not line or line.startswith("#") or "=" not in line:
            continue

        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")

    return values


def preflight() -> None:
    """Print a short, honest readiness report before starting anything."""

    print("=" * 68)
    print("TraceAI / SCAMNET - starting the whole project")
    print("=" * 68)

    env_path = REPO_ROOT / ".env"

    if not env_path.exists():
        print("\n[!] No .env file at the repository root.")
        print("    Copy the template and fill it in:")
        print("        cp .env.example .env      (Windows: copy .env.example .env)")
        print("    The backend will still start in degraded mode.\n")
        return

    env = read_env_file()
    missing = [
        key for key in REQUIRED_KEYS if not env.get(key)
    ]

    if not missing:
        print("\n[ok] .env found - LLM key and Telegram bot token are set.")

        if env.get("TELEGRAM_API_BASE"):
            print(
                f"     TELEGRAM_API_BASE={env['TELEGRAM_API_BASE']} "
                "(pointing at a simulator, not real Telegram)"
            )
        print()
        return

    print(f"\n[!] .env found, but these keys are empty: {', '.join(missing)}")

    for key in missing:
        if key == "OPENROUTER_API_KEY":
            print(
                "    - OPENROUTER_API_KEY : agents answer HTTP 503 without it"
                " (get one at https://openrouter.ai/keys)"
            )
        else:
            print(
                "    - TELEGRAM_BOT_TOKEN : the Telegram bot cannot connect"
                " (get one from @BotFather)"
            )

    print("    Fill them in .env and restart. Continuing anyway.\n")


def encode_for_console(text: str) -> str:
    """Keep emoji output from crashing a legacy Windows console."""

    try:
        text.encode(sys.stdout.encoding or "utf-8")
        return text
    except (UnicodeEncodeError, LookupError):
        return text.encode("ascii", "replace").decode("ascii")


# ------------------------------------------------------------------
# Process supervision
# ------------------------------------------------------------------

class Service:
    """One child process whose output is streamed with a prefix."""

    def __init__(self, name: str, command, cwd: Path, env: dict):
        self.name = name
        self.prefix = f"[{name}]"
        self.command = command
        self.logs: list = []

        creationflags = 0
        if IS_WINDOWS:
            # Own process group so Ctrl+C / taskkill can stop the tree.
            creationflags = subprocess.CREATE_NEW_PROCESS_GROUP

        self.process = subprocess.Popen(
            command,
            cwd=str(cwd),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            creationflags=creationflags,
        )

        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self) -> None:
        stream = self.process.stdout

        if stream is None:
            return

        for line in stream:
            line = line.rstrip()

            if not line:
                continue

            self.logs.append(line)
            del self.logs[:-40]  # keep the tail only

            print(encode_for_console(f"{self.prefix} {line}"), flush=True)

    def is_alive(self) -> bool:
        return self.process.poll() is None

    def stop(self, timeout: float = 8.0) -> None:
        """Stop this process (and its children on Windows)."""

        if not self.is_alive():
            return

        try:
            if IS_WINDOWS:
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(self.process.pid)],
                    capture_output=True,
                    check=False,
                )
            else:
                self.process.terminate()

            self.process.wait(timeout=timeout)

        except Exception:  # noqa: BLE001 - best effort shutdown
            try:
                self.process.kill()
            except Exception:  # noqa: BLE001
                pass


def wait_for_port(port: int, service: Service, timeout: float = 90.0) -> bool:
    """Wait until ``port`` accepts connections (or the service died)."""

    deadline = time.time() + timeout

    while time.time() < deadline:

        if port_is_open(port):
            return True

        if not service.is_alive():
            return False

        time.sleep(0.4)

    return port_is_open(port)


# ------------------------------------------------------------------
# Main
# ------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:

    parser = argparse.ArgumentParser(
        description="Start the FastAPI backend, the Next.js dashboard and "
        "(optionally) the Streamlit UI with one command.",
    )

    parser.add_argument("--backend-port", type=int, default=DEFAULT_BACKEND_PORT)
    parser.add_argument("--frontend-port", type=int, default=DEFAULT_FRONTEND_PORT)
    parser.add_argument("--streamlit-port", type=int, default=DEFAULT_STREAMLIT_PORT)
    parser.add_argument(
        "--no-frontend",
        action="store_true",
        help="skip the Next.js dashboard (backend only)",
    )
    parser.add_argument(
        "--streamlit",
        action="store_true",
        help="also start the Streamlit UI (streamlit_app.py)",
    )
    parser.add_argument(
        "--skip-install",
        action="store_true",
        help="never run npm install (fail fast if node_modules is missing)",
    )
    parser.add_argument(
        "--reload",
        action="store_true",
        help="restart the backend automatically when Python files change",
    )

    return parser


def main() -> int:

    args = build_parser().parse_args()

    preflight()

    python = venv_python()

    if python != sys.executable:
        print(f"[ok] Using the project virtualenv: {python}")

    # ----------------------------------------------------------
    # Frontend dependencies (first run only)
    # ----------------------------------------------------------
    if not args.no_frontend:

        if not (FRONTEND_DIR / "node_modules").exists():

            if args.skip_install:
                print(
                    "[x] frontend/node_modules is missing. Run "
                    "'cd frontend && npm install' first (or drop "
                    "--skip-install)."
                )
                return 1

            print("[..] Installing dashboard dependencies (first run only)...")

            result = subprocess.run(
                [npm_command(), "install"], cwd=str(FRONTEND_DIR), check=False
            )

            if result.returncode != 0:
                print("[x] npm install failed - fix that and re-run.")
                return 1

    # ----------------------------------------------------------
    # Environment for the children
    # ----------------------------------------------------------
    env = os.environ.copy()

    # The dashboard's Next server proxies /backend-api/* here.
    env["BACKEND_INTERNAL_URL"] = f"http://127.0.0.1:{args.backend_port}"

    # Make sure the backend can import the project when started elsewhere.
    env["PYTHONPATH"] = str(REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", "")

    services: list = []

    def shutdown(*_args) -> None:
        print("\n[..] Stopping everything...")

        for service in reversed(services):
            service.stop()

        print("[ok] All services stopped.")
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)

    try:
        if not IS_WINDOWS:  # Windows handlers are limited; Ctrl+C still works
            signal.signal(signal.SIGTERM, shutdown)

        # ------------------------------------------------------
        # 1. Backend API
        # ------------------------------------------------------
        backend_command = [
            python, "-m", "uvicorn", "backend.api:app",
            "--host", "0.0.0.0", "--port", str(args.backend_port),
        ]

        if args.reload:
            backend_command.append("--reload")

        print(f"[..] Backend  : http://127.0.0.1:{args.backend_port}")
        services.append(
            Service("api", backend_command, cwd=REPO_ROOT, env=env)
        )

        if not wait_for_port(args.backend_port, services[-1]):
            print("[x] The backend did not start - see the [api] lines above.")
            shutdown()

        # ------------------------------------------------------
        # 2. Dashboard (Next.js)
        # ------------------------------------------------------
        if not args.no_frontend:

            print(f"[..] Dashboard: http://127.0.0.1:{args.frontend_port}")
            services.append(
                Service(
                    "ui",
                    [
                        npm_command(), "run", "dev", "--",
                        "-H", "0.0.0.0", "-p", str(args.frontend_port),
                    ],
                    cwd=FRONTEND_DIR,
                    env=env,
                )
            )

            if not wait_for_port(args.frontend_port, services[-1], timeout=120):
                print("[!] The dashboard did not come up - see [ui] lines above.")

        # ------------------------------------------------------
        # 3. Streamlit (optional)
        # ------------------------------------------------------
        if args.streamlit:

            print(f"[..] Streamlit: http://127.0.0.1:{args.streamlit_port}")
            services.append(
                Service(
                    "streamlit",
                    [
                        python, "-m", "streamlit", "run", "streamlit_app.py",
                        "--server.port", str(args.streamlit_port),
                        "--server.address", "0.0.0.0",
                        "--server.headless", "true",
                    ],
                    cwd=REPO_ROOT,
                    env=env,
                )
            )

            if not wait_for_port(args.streamlit_port, services[-1], timeout=60):
                print("[!] Streamlit did not come up - see [streamlit] lines.")

        # ------------------------------------------------------
        # Ready banner
        # ------------------------------------------------------
        env_values = read_env_file()

        print("\n" + "=" * 68)
        print("READY")
        print("=" * 68)
        print(f"  Dashboard      : http://localhost:{args.frontend_port}")
        print(f"  Backend API    : http://localhost:{args.backend_port}")
        print(f"  Health check   : http://localhost:{args.backend_port}/health")
        print(
            "  Telegram status: "
            f"http://localhost:{args.backend_port}/api/telegram/conversation/status"
        )

        if args.streamlit:
            print(f"  Streamlit UI   : http://localhost:{args.streamlit_port}")

        if env_values.get("TELEGRAM_BOT_TOKEN"):
            print(
                "\n  The Telegram bot connects on boot. Message it from "
                "Telegram,\nor watch the loop in the dashboard: "
                "Connected Apps -> Telegram."
            )
        else:
            print(
                "\n  No TELEGRAM_BOT_TOKEN yet - the bot stays offline "
                "(everything else works)."
            )

        print("\n  Ctrl+C stops every service.\n")

        # ------------------------------------------------------
        # Supervise: if a child dies unexpectedly, shut down cleanly
        # ------------------------------------------------------
        while True:
            time.sleep(1)

            for service in services:
                if not service.is_alive():
                    print(
                        f"\n[x] The '{service.name}' service exited "
                        f"(code {service.process.returncode})."
                    )
                    for line in service.logs[-8:]:
                        print(encode_for_console(f"    {line}"))
                    shutdown()

    except KeyboardInterrupt:
        shutdown()

    return 0


if __name__ == "__main__":
    sys.exit(main())
