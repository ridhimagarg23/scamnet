#!/usr/bin/env python3
"""
google_oauth_setup.py
=====================
One-shot helper that produces the **authorized-user** credentials JSON
SCAMNET's Google integrations need - especially **Gmail**, which a plain
service account cannot use (it requires a real user's OAuth refresh
token, or Workspace domain-wide delegation).

What it does
------------
1. Reads your OAuth *desktop* client id/secret (the JSON you download
   from Google Cloud Console -> "Credentials" -> "OAuth client ID" ->
   "Desktop app", or pass them on the command line).
2. Starts a tiny local redirect server on ``localhost`` (no external
   service, no polling).
3. Prints an authorization URL, opens your browser, and waits for Google
   to redirect back with an authorization ``code``.
4. Exchanges that code for a **refresh token** and writes a standard
   ``{"type": "authorized_user", ...}`` JSON file.
5. Tells you exactly which ``.env`` variable to point at that file.

Then in the server-side ``.env``::

    GOOGLE_GMAIL_CREDENTIALS_FILE=credentials/gmail_token.json
    # or share ONE file across Drive + Sheets + Gmail:
    GOOGLE_CREDENTIALS_FILE=credentials/google_token.json

Restart the backend and press **Connect** in the dashboard's Connected
Apps modal - the integration mints an access token from this file and
verifies it with a real API call.

Dependencies
------------
Standard library only (``http.server``, ``webbrowser``, ``urllib``). No
extra packages, no network calls beyond Google's own OAuth endpoints.

Usage
-----
    # Interactive (prompts for the client JSON path, opens a browser):
    python scripts/google_oauth_setup.py --apps gmail

    # Fully non-interactive:
    python scripts/google_oauth_setup.py \
        --client-secrets credentials/oauth_client.json \
        --apps gmail,drive,sheets \
        --out credentials/google_token.json

    # Headless server (no browser): it prints the URL, you open it on
    # any machine, then paste the redirected URL back:
    python scripts/google_oauth_setup.py --apps gmail --no-browser
"""

from __future__ import annotations

import argparse
import json
import secrets
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import parse_qs, urlencode, urlparse

# Google's OAuth 2.0 endpoints (fixed, public).
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"

# Scope bundles per app. Keep in sync with integrations/*/client.py.
APP_SCOPES: Dict[str, List[str]] = {
    "gmail": [
        "https://www.googleapis.com/auth/gmail.readonly",
        "https://www.googleapis.com/auth/gmail.send",
    ],
    "drive": [
        "https://www.googleapis.com/auth/drive",
    ],
    "sheets": [
        "https://www.googleapis.com/auth/spreadsheets",
    ],
}

# The redirect URI Google expects for an installed/desktop OAuth client.
# "localhost" (any port) is allow-listed for desktop clients, so a
# throwaway local server on a random free port works.
REDIRECT_HOST = "localhost"


# --------------------------------------------------
# Small helpers
# --------------------------------------------------

def _eprint(*args) -> None:
    print(*args, file=sys.stderr)


def _load_client_secrets(path: Path) -> Dict[str, str]:
    """
    Read the OAuth *client* JSON downloaded from Google Cloud Console.

    Accepts both the raw ``{"client_id": ..., "client_secret": ...}``
    shape and the wrapped ``{"installed": {...}}`` / ``{"web": {...}}``
    shape the console exports.
    """

    if not path.is_file():
        raise SystemExit(
            f"Client secrets file not found: {path}\n"
            "Download it from Google Cloud Console -> Credentials -> "
            "OAuth client ID (Desktop app)."
        )

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise SystemExit(f"Client secrets file is not valid JSON: {exc}")

    block = data
    for wrapper in ("installed", "web"):
        if wrapper in data and isinstance(data[wrapper], dict):
            block = data[wrapper]
            break

    client_id = block.get("client_id")
    client_secret = block.get("client_secret")

    if not client_id or not client_secret:
        raise SystemExit(
            "Client secrets file must contain client_id and client_secret "
            "(inside the top-level object, or an 'installed'/'web' block)."
        )

    return {"client_id": client_id, "client_secret": client_secret}


def _resolve_scopes(apps: List[str]) -> List[str]:
    """Union of the requested apps' scopes, de-duplicated, order kept."""

    scopes: List[str] = []
    seen = set()

    for app in apps:
        for scope in APP_SCOPES.get(app, []):
            if scope not in seen:
                seen.add(scope)
                scopes.append(scope)

    if not scopes:
        raise SystemExit(
            f"No scopes resolved for apps={apps}. "
            f"Choose from: {', '.join(APP_SCOPES)}."
        )

    return scopes


def _free_port() -> int:
    """Ask the OS for an unused TCP port on localhost."""

    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind((REDIRECT_HOST, 0))
        return probe.getsockname()[1]


# --------------------------------------------------
# Local redirect server (captures the authorization code)
# --------------------------------------------------

class _RedirectHandler(BaseHTTPRequestHandler):
    """Single-shot handler: captures ``?code=...`` then shuts down."""

    # Filled in by the server wrapper before it starts serving.
    result: Dict[str, Optional[str]] = {"code": None, "error": None}

    def do_GET(self):  # noqa: N802 - http.server API
        query = parse_qs(urlparse(self.path).query)

        if "code" in query:
            _RedirectHandler.result["code"] = query["code"][0]
            body = (
                "<html><body style='font-family:sans-serif;text-align:center;"
                "padding-top:60px'><h2>&#10003; Authorization received</h2>"
                "<p>You can close this tab and return to the terminal.</p>"
                "</body></html>"
            )
            self.send_response(200)
        else:
            _RedirectHandler.result["error"] = query.get("error", ["unknown"])[0]
            body = (
                "<html><body style='font-family:sans-serif;text-align:center;"
                "padding-top:60px'><h2>&#10007; Authorization failed</h2>"
                f"<p>{_RedirectHandler.result['error']}</p></body></html>"
            )
            self.send_response(400)

        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(body.encode("utf-8"))

        # Stop serving after the first request.
        threading.Thread(target=self.server.shutdown, daemon=True).start()

    def log_message(self, *args):  # silence default stderr logging
        pass


def _exchange_code_for_token(
    code: str,
    client_id: str,
    client_secret: str,
    redirect_uri: str,
) -> Dict[str, str]:
    """POST the authorization code to Google's token endpoint."""

    import urllib.error
    import urllib.request

    payload = urlencode({
        "code": code,
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
    }).encode("utf-8")

    request = urllib.request.Request(
        TOKEN_URL,
        data=payload,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        raise SystemExit(
            f"Token exchange failed (HTTP {exc.code}): {detail}\n"
            "Common causes: the OAuth client is not a 'Desktop app' type, "
            "the client secret is wrong, or the redirect URI mismatch."
        )
    except urllib.error.URLError as exc:
        raise SystemExit(f"Could not reach Google's token endpoint: {exc}")


# --------------------------------------------------
# Main flow
# --------------------------------------------------

def run_oauth_flow(
    client_id: str,
    client_secret: str,
    scopes: List[str],
    port: int,
    open_browser: bool = True,
) -> Dict[str, str]:
    """
    Drive the authorization-code flow and return the token response.

    Returns the raw Google token JSON (contains ``refresh_token`` the
    first time a user consents to a new scope set).
    """

    redirect_uri = f"http://{REDIRECT_HOST}:{port}/"
    state = secrets.token_urlsafe(16)

    auth_params = urlencode({
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(scopes),
        "access_type": "offline",     # required to get a refresh_token
        "prompt": "consent",          # force re-consent -> refresh_token
        "state": state,
    })
    auth_link = f"{AUTH_URL}?{auth_params}"

    _RedirectHandler.result = {"code": None, "error": None}
    server = HTTPServer((REDIRECT_HOST, port), _RedirectHandler)

    print("\n" + "=" * 64)
    print("Authorize SCAMNET to act as this Google account")
    print("=" * 64)
    print(f"\nScopes requested:\n  - " + "\n  - ".join(scopes))
    print(f"\nOpen this URL in your browser:\n\n{auth_link}\n")

    if open_browser:
        try:
            webbrowser.open(auth_link)
            print("(A browser tab should have opened automatically.)")
        except Exception:
            print("(Could not open a browser automatically - use the URL above.)")

    print("\nWaiting for the authorization redirect on "
          f"{redirect_uri} ... (Ctrl+C to cancel)\n")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.server_close()
        raise SystemExit("\nCancelled before authorization completed.")

    server.server_close()

    if _RedirectHandler.result["error"]:
        raise SystemExit(
            f"Google returned an error: {_RedirectHandler.result['error']}"
        )

    code = _RedirectHandler.result["code"]
    if not code:
        raise SystemExit("No authorization code was received.")

    return _exchange_code_for_token(
        code, client_id, client_secret, redirect_uri
    )


def _authorized_user_payload(
    token: Dict[str, str],
    client_id: str,
    client_secret: str,
    scopes: List[str],
) -> Dict[str, str]:
    """
    Build the ``authorized_user`` JSON google-auth understands.

    google-auth's ``Credentials.from_authorized_user_info`` needs
    ``client_id``, ``client_secret`` and ``refresh_token``; ``scopes``
    is optional but helps it re-request the right permissions on refresh.
    """

    refresh_token = token.get("refresh_token")

    if not refresh_token:
        # Google only returns a refresh_token the FIRST time a user
        # consents to a given scope set (or when access_type=offline +
        # prompt=consent, which we send). If it is missing the user has
        # likely already authorized these exact scopes before.
        raise SystemExit(
            "Google did not return a refresh_token.\n"
            "This usually means the account already granted these exact "
            "scopes. Revoke the app at "
            "https://myaccount.google.com/permissions and run this script "
            "again, or add a new scope."
        )

    return {
        "type": "authorized_user",
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token,
        "token_uri": TOKEN_URL,
        "scopes": scopes,
    }


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Generate an authorized-user credentials JSON for SCAMNET's "
            "Google integrations (Gmail / Drive / Sheets)."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--client-secrets",
        type=Path,
        help=(
            "Path to the OAuth client JSON from Google Cloud Console "
            "(Desktop app). Prompted for if omitted."
        ),
    )
    parser.add_argument(
        "--client-id",
        help="OAuth client id (overrides --client-secrets).",
    )
    parser.add_argument(
        "--client-secret",
        help="OAuth client secret (overrides --client-secrets).",
    )
    parser.add_argument(
        "--apps",
        default="gmail,drive,sheets",
        help=(
            "Comma-separated apps to authorize: gmail, drive, sheets "
            "(default: all three)."
        ),
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("credentials/google_token.json"),
        help=(
            "Where to write the authorized-user JSON "
            "(default: credentials/google_token.json)."
        ),
    )
    parser.add_argument(
        "--port",
        type=int,
        default=0,
        help="Local redirect port (default: a free random port).",
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="Do not try to open a browser (headless servers).",
    )

    args = parser.parse_args(argv)

    apps = [app.strip().lower() for app in args.apps.split(",") if app.strip()]
    unknown = [app for app in apps if app not in APP_SCOPES]
    if unknown:
        raise SystemExit(
            f"Unknown app(s): {', '.join(unknown)}. "
            f"Choose from: {', '.join(APP_SCOPES)}."
        )

    scopes = _resolve_scopes(apps)

    # Resolve client id/secret.
    if args.client_id and args.client_secret:
        client_id = args.client_id
        client_secret = args.client_secret
    else:
        secrets_path = args.client_secrets
        if secrets_path is None:
            raw = input(
                "Path to your OAuth client JSON "
                "(Google Cloud Console -> Credentials): "
            ).strip().strip('"')
            secrets_path = Path(raw) if raw else None
        if secrets_path is None:
            raise SystemExit("No client secrets provided.")
        loaded = _load_client_secrets(secrets_path)
        client_id = loaded["client_id"]
        client_secret = loaded["client_secret"]

    port = args.port or _free_port()

    token = run_oauth_flow(
        client_id=client_id,
        client_secret=client_secret,
        scopes=scopes,
        port=port,
        open_browser=not args.no_browser,
    )

    payload = _authorized_user_payload(
        token, client_id, client_secret, scopes
    )

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    try:
        args.out.chmod(0o600)
    except OSError:
        pass

    out_path = args.out.resolve()

    print("\n" + "=" * 64)
    print("Credentials written")
    print("=" * 64)
    print(f"\n  File: {out_path}")
    print(f"  Apps authorized: {', '.join(apps)}")
    print("\nNow point the server-side .env at this file:")

    if "gmail" in apps:
        print(f"  GOOGLE_GMAIL_CREDENTIALS_FILE={out_path}")
    if "drive" in apps:
        print(f"  GOOGLE_DRIVE_CREDENTIALS_FILE={out_path}")
    if "sheets" in apps:
        print(f"  GOOGLE_SHEETS_CREDENTIALS_FILE={out_path}")

    print("\nOr share ONE file across every Google app:")
    print(f"  GOOGLE_CREDENTIALS_FILE={out_path}")

    print("\nThen restart the backend and press Connect in the dashboard's")
    print("Connected Apps modal. Keep this file OUT of git (see .gitignore:")
    print("credentials/ and *token*.json are already ignored).\n")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
