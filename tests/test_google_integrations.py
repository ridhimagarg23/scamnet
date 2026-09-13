"""
test_google_integrations.py
===========================
Offline unit tests for the Google integrations (Google Drive, Google
Sheets, Gmail) and the evidence archiver.

No network access and no real Google account is needed. Every test
injects a stub HTTP client, so the *complete* flow runs:

    credentials JSON -> OAuth token refresh -> authorized API call
    -> connect()/check_health() -> Drive upload / Sheets upsert / Gmail send

What is pinned here
-------------------
* ``connect()`` only reports ``connected=True`` after a real authorized
  API call succeeded (the honest-status contract of integrations/base.py).
* Google failures (401/403/404, network error, non-JSON body) surface as
  sanitised ``GoogleAPIError`` messages - never a token, never the
  credentials-file path.
* A credentials file that is missing / not JSON / the wrong type keeps
  the integration ``not_configured`` (HTTP 409 on the API), never
  "connected".
* Drive uploads UPDATE the same file on later turns; Sheets upserts ONE
  row per case id; the archiver skips disconnected apps and swallows
  failures.

Run with:

    OPENROUTER_API_KEY=test-key python -m unittest discover -s tests
"""

import json
import tempfile
import unittest
from types import SimpleNamespace

from fastapi import HTTPException

from integrations import get_integration
from integrations.base import IntegrationState
from integrations.gmail import GmailIntegration
from integrations.google_api import GoogleAPIError
from integrations.google_drive import GoogleDriveIntegration
from integrations.google_sheets import (
    EVIDENCE_COLUMNS,
    GoogleSheetsIntegration,
)
from tools.evidence_archive import EvidenceArchiver


# Obviously-fake secrets used ONLY inside these tests.
FAKE_ACCESS_TOKEN = "ya29.FAKE-TOKEN-do-not-leak"
FAKE_REFRESH_TOKEN = "1//FAKE-REFRESH-do-not-leak"
FAKE_CLIENT_SECRET = "GOCSPX-FAKE-client-secret-do-not-leak"


# --------------------------------------------------
# Stubs
# --------------------------------------------------

class StubResponse:
    """Mimics the parts of httpx.Response the Google clients use."""

    def __init__(self, payload, status_code=200, json_error=False):
        self._payload = payload
        self.status_code = status_code
        self._json_error = json_error
        self.headers = {"content-type": "application/json"}
        self.content = b"" if json_error else json.dumps(payload).encode()

    def json(self):
        if self._json_error:
            raise ValueError("response body is not JSON")
        return self._payload


class StubHTTPClient:
    """
    Mimics ``httpx.Client.request(...)``.

    Queued responses are returned in order; an Exception instance in the
    queue is raised instead. Every call is recorded for assertions.
    """

    def __init__(self, *responses):
        self._responses = list(responses)
        self.calls = []

    def request(
        self,
        method,
        url,
        params=None,
        json=None,
        content=None,
        headers=None,
        timeout=None,
    ):
        self.calls.append({
            "method": method,
            "url": url,
            "params": params,
            "json": json,
            "content": content,
            "headers": headers,
            "timeout": timeout,
        })
        if not self._responses:
            raise AssertionError(
                f"StubHTTPClient ran out of queued responses for {method} {url}"
            )
        response = self._responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def token_response():
    """Successful OAuth token refresh (used by every authorized test)."""

    return StubResponse({
        "access_token": FAKE_ACCESS_TOKEN,
        "expires_in": 3600,
        "token_type": "Bearer",
    })


def make_settings(credentials_file=None, **overrides):
    """Stub settings object with every Google key present but empty."""

    base = dict(
        GOOGLE_CREDENTIALS_FILE=None,
        GOOGLE_DRIVE_CREDENTIALS_FILE=credentials_file,
        GOOGLE_DRIVE_FOLDER_ID=None,
        GOOGLE_SHEETS_CREDENTIALS_FILE=credentials_file,
        GOOGLE_SHEETS_SPREADSHEET_ID=None,
        GOOGLE_SHEETS_WORKSHEET=None,
        GOOGLE_GMAIL_CREDENTIALS_FILE=credentials_file,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def write_credentials_file(credential_type="authorized_user", **overrides):
    """
    Write a temporary credentials JSON and return its path.

    Defaults to an ``authorized_user`` file (no RSA key needed) whose
    refresh_token makes google-auth hit our stub token endpoint.
    """

    info = {
        "type": credential_type,
        "client_id": "fake-client-id.apps.googleusercontent.com",
        "client_secret": FAKE_CLIENT_SECRET,
        "refresh_token": FAKE_REFRESH_TOKEN,
    }
    info.update(overrides)

    handle = tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False
    )
    json.dump(info, handle)
    handle.close()

    return handle.name


def drive_about_response(email="scamnet@project.iam.gserviceaccount.com"):
    return StubResponse({
        "user": {"emailAddress": email, "displayName": "SCAMNET"}
    })


# --------------------------------------------------
# Google Drive
# --------------------------------------------------

class TestGoogleDrive(unittest.TestCase):

    def make_drive(self, *responses, **settings_overrides):
        settings = make_settings(
            write_credentials_file(), **settings_overrides
        )
        stub = StubHTTPClient(*responses)
        return GoogleDriveIntegration(settings, http_client=stub), stub

    def test_connect_performs_real_authorized_call(self):
        """
        connect() refreshes the token and calls about.get; only then is
        the integration connected.
        """

        integration, stub = self.make_drive(
            token_response(), drive_about_response()
        )

        self.assertFalse(integration.is_connected())

        integration.connect()

        self.assertTrue(integration.is_connected())
        self.assertEqual(
            integration.get_status().state, IntegrationState.CONNECTED
        )
        self.assertTrue(integration.get_status().available)

        # Token refresh + about.get were both real HTTP calls.
        urls = [call["url"] for call in stub.calls]
        self.assertTrue(any("oauth2.googleapis.com" in url for url in urls))
        self.assertTrue(any(url.endswith("/drive/v3/about") for url in urls))

        # The bearer token is attached, and the response leaks no secret.
        api_call = stub.calls[-1]
        self.assertEqual(
            api_call["headers"]["Authorization"], f"Bearer {FAKE_ACCESS_TOKEN}"
        )
        payload = integration.get_status().model_dump_json()
        self.assertNotIn(FAKE_ACCESS_TOKEN, payload)
        self.assertNotIn(FAKE_REFRESH_TOKEN, payload)

    def test_connect_verifies_configured_folder(self):
        """A configured folder is verified at connect time."""

        integration, stub = self.make_drive(
            token_response(),
            drive_about_response(),
            StubResponse({"id": "folder-1", "name": "Cases", "mimeType": "x"}),
            GOOGLE_DRIVE_FOLDER_ID="folder-1",
        )

        integration.connect()

        info = integration.get_connection_info()
        self.assertEqual(info["folder_id"], "folder-1")
        self.assertEqual(info["folder_name"], "Cases")
        self.assertTrue(any(
            call["url"].endswith("/drive/v3/files/folder-1")
            for call in stub.calls
        ))

    def test_connect_rejects_trashed_folder(self):
        """A trashed destination folder fails the connect honestly."""

        integration, _ = self.make_drive(
            token_response(),
            drive_about_response(),
            StubResponse({"id": "folder-1", "name": "Cases", "trashed": True}),
            GOOGLE_DRIVE_FOLDER_ID="folder-1",
        )

        with self.assertRaises(GoogleAPIError) as ctx:
            integration.connect()

        self.assertIn("trash", str(ctx.exception))
        self.assertFalse(integration.is_connected())

    def test_connect_rejected_credentials_is_sanitised(self):
        """A 401 from Google becomes a sanitised error + ERROR status."""

        integration, _ = self.make_drive(
            token_response(),
            StubResponse(
                {"error": {"code": 401, "message": "Invalid Credentials"}},
                status_code=401,
            ),
        )

        with self.assertRaises(GoogleAPIError) as ctx:
            integration.connect()

        message = str(ctx.exception)
        self.assertIn("Invalid Credentials", message)
        self.assertNotIn(FAKE_ACCESS_TOKEN, message)
        self.assertFalse(integration.is_connected())

        status = integration.get_status()
        self.assertEqual(status.state, IntegrationState.ERROR)
        self.assertFalse(status.connected)
        self.assertNotIn(FAKE_ACCESS_TOKEN, status.model_dump_json())

    def test_network_error_is_reported_as_connection_error(self):
        """A transport failure never leaks the raw httpx error text."""

        integration, _ = self.make_drive(
            token_response(), ConnectionError("boom"),
        )

        with self.assertRaises(GoogleAPIError) as ctx:
            integration.connect()

        self.assertIn("ConnectionError", str(ctx.exception))
        self.assertFalse(integration.is_connected())

    def test_upload_report_creates_then_updates_the_same_file(self):
        """Multi-turn cases update one Drive artefact (no duplicates)."""

        integration, stub = self.make_drive(
            token_response(),
            drive_about_response(),
            StubResponse({
                "id": "file-1",
                "name": "TraceAI Investigation Report.md",
                "webViewLink": "https://drive.google.com/file/d/file-1/view",
            }),
            StubResponse({
                "id": "file-1",
                "name": "TraceAI Investigation Report.md",
                "webViewLink": "https://drive.google.com/file/d/file-1/view",
            }),
        )

        integration.connect()

        created = integration.upload_report(
            "TraceAI Investigation Report", "# Turn 1"
        )
        self.assertEqual(created["id"], "file-1")
        self.assertEqual(created["link"], "https://drive.google.com/file/d/file-1/view")

        updated = integration.upload_report(
            "TraceAI Investigation Report", "# Turn 2", file_id="file-1"
        )
        self.assertEqual(updated["id"], "file-1")

        create_call = stub.calls[-2]
        update_call = stub.calls[-1]

        self.assertEqual(create_call["method"], "POST")
        self.assertEqual(create_call["params"]["uploadType"], "multipart")
        # multipart body embeds the markdown and the file metadata
        self.assertIn(b"# Turn 1", create_call["content"])
        self.assertIn(b"TraceAI Investigation Report.md", create_call["content"])

        self.assertEqual(update_call["method"], "PATCH")
        self.assertIn("file-1", update_call["url"])
        self.assertEqual(update_call["content"], b"# Turn 2")

    def test_upload_report_validates_input(self):
        integration, _ = self.make_drive(token_response(), drive_about_response())
        integration.connect()

        with self.assertRaises(ValueError):
            integration.upload_report("", "# body")
        with self.assertRaises(ValueError):
            integration.upload_report("Title", "")

    def test_health_check_uses_ttl_cache_and_flips_on_failure(self):
        """Health is cached; a failing re-check honestly disconnects."""

        integration, stub = self.make_drive(
            token_response(),
            drive_about_response(),
            # Second about.get fails (expired/revoked credentials).
            StubResponse(
                {"error": {"code": 403, "message": "Forbidden"}},
                status_code=403,
            ),
        )

        integration.connect()
        self.assertEqual(len(stub.calls), 2)

        # Cached: no new HTTP call.
        self.assertTrue(integration.is_connected())
        self.assertEqual(len(stub.calls), 2)

        # Expire the cache manually, then the failing call flips state.
        integration._last_health_at = 0.0
        self.assertFalse(integration.is_connected())
        self.assertFalse(integration.is_connected())

    def test_file_name_derivation(self):
        """Report titles become safe, .md-terminated file names."""

        self.assertEqual(
            GoogleDriveIntegration._file_name("TraceAI Report"),
            "TraceAI Report.md",
        )
        # Special characters are replaced so Drive accepts the name.
        self.assertEqual(
            GoogleDriveIntegration._file_name("Report: /2026/ #1"),
            "Report_2026_1.md",
        )
        self.assertEqual(
            GoogleDriveIntegration._file_name("Already.md"),
            "Already.md",
        )
        self.assertEqual(
            GoogleDriveIntegration._file_name("  "),
            "TraceAI Investigation Report.md",
        )

    def test_disconnect_clears_session_state(self):
        integration, _ = self.make_drive(
            token_response(), drive_about_response()
        )
        integration.connect()
        self.assertTrue(integration.is_connected())

        integration.disconnect()

        self.assertFalse(integration.is_connected())
        self.assertEqual(integration.get_connection_info(), {})


# --------------------------------------------------
# Google Sheets
# --------------------------------------------------

class TestGoogleSheets(unittest.TestCase):

    def make_sheets(self, *responses, **settings_overrides):
        settings = make_settings(
            write_credentials_file(), **settings_overrides
        )
        stub = StubHTTPClient(*responses)
        return GoogleSheetsIntegration(settings, http_client=stub), stub

    SPREADSHEET_METADATA = StubResponse({
        "spreadsheetId": "spreadsheet-1",
        "properties": {"title": "SCAMNET Evidence"},
        "sheets": [{"properties": {"title": "Evidence"}}],
    })

    def test_connect_verifies_configured_spreadsheet(self):
        integration, stub = self.make_sheets(
            token_response(),
            self.SPREADSHEET_METADATA,
            GOOGLE_SHEETS_SPREADSHEET_ID="spreadsheet-1",
        )

        integration.connect()

        self.assertTrue(integration.is_connected())
        info = integration.get_connection_info()
        self.assertEqual(info["spreadsheet_id"], "spreadsheet-1")
        self.assertEqual(info["worksheet"], "Evidence")
        self.assertIn("docs.google.com/spreadsheets", info["spreadsheet_url"])

        # No addSheet call: the tab already existed.
        self.assertFalse(any(
            call["url"].endswith(":batchUpdate") for call in stub.calls
        ))

    def test_connect_creates_spreadsheet_and_worksheet_when_unset(self):
        integration, stub = self.make_sheets(
            token_response(),
            StubResponse({
                "spreadsheetId": "created-1",
                "properties": {"title": "SCAMNET Investigation Evidence"},
                "sheets": [{"properties": {"title": "Sheet1"}}],
            }),
            StubResponse({"replies": [{}]}),
        )

        integration.connect()

        self.assertTrue(integration.is_connected())
        self.assertEqual(integration.spreadsheet_id, "created-1")

        create_call = stub.calls[1]
        self.assertEqual(create_call["method"], "POST")
        self.assertIn(
            "SCAMNET Investigation Evidence",
            json.dumps(create_call["json"]),
        )

        add_sheet_call = stub.calls[2]
        self.assertTrue(add_sheet_call["url"].endswith(":batchUpdate"))
        self.assertEqual(
            add_sheet_call["json"]["requests"][0]["addSheet"]["properties"]["title"],
            "Evidence",
        )

    def test_connect_reports_missing_spreadsheet_access(self):
        """A 404 (service account not invited) is honest, not silent."""

        integration, _ = self.make_sheets(
            token_response(),
            StubResponse(
                {"error": {"code": 404, "message": "Requested entity was not found."}},
                status_code=404,
            ),
            GOOGLE_SHEETS_SPREADSHEET_ID="private-sheet",
        )

        with self.assertRaises(GoogleAPIError) as ctx:
            integration.connect()

        self.assertIn("404", str(ctx.exception))
        self.assertIn("not found", str(ctx.exception))
        self.assertEqual(
            integration.get_status().state, IntegrationState.ERROR
        )

    def test_append_rows_writes_header_once(self):
        integration, stub = self.make_sheets(
            token_response(),
            self.SPREADSHEET_METADATA,
            StubResponse({}),  # A1:A1 empty -> header must be added
            StubResponse({"updates": {"updatedRows": 2, "updatedRange": "Evidence!A1:M3"}}),
            GOOGLE_SHEETS_SPREADSHEET_ID="spreadsheet-1",
        )
        integration.connect()

        result = integration.append_rows([["a"] * len(EVIDENCE_COLUMNS)])

        self.assertEqual(result["updated_rows"], 2)
        append_call = stub.calls[-1]
        values = append_call["json"]["values"]
        self.assertEqual(values[0], list(EVIDENCE_COLUMNS))
        self.assertEqual(len(values), 2)
        self.assertEqual(append_call["params"]["valueInputOption"], "RAW")

    def test_append_rows_skips_header_when_present(self):
        integration, stub = self.make_sheets(
            token_response(),
            self.SPREADSHEET_METADATA,
            StubResponse({"values": [["case_id"]]}),
            StubResponse({"updates": {"updatedRows": 1}}),
            GOOGLE_SHEETS_SPREADSHEET_ID="spreadsheet-1",
        )
        integration.connect()

        integration.append_rows([["row"] * len(EVIDENCE_COLUMNS)])

        values = stub.calls[-1]["json"]["values"]
        self.assertEqual(len(values), 1)
        self.assertEqual(values[0][0], "row")

    def test_upsert_case_creates_then_updates_one_row(self):
        """Repeated turns rewrite the case row instead of duplicating it."""

        integration, stub = self.make_sheets(
            token_response(),
            self.SPREADSHEET_METADATA,
            # upsert #1: column A has only the header
            StubResponse({"values": []}),
            # append -> header check + append
            StubResponse({}),
            StubResponse({"updates": {"updatedRows": 2}}),
            # recompute rows after append
            StubResponse({"values": [["case-1"]]}),
            # upsert #2: column A already holds case-1 -> update in place
            StubResponse({"values": [["case-1"]]}),
            StubResponse({"updatedRows": 1, "updatedRange": "Evidence!A5:M5"}),
            GOOGLE_SHEETS_SPREADSHEET_ID="spreadsheet-1",
        )
        integration.connect()

        first = integration.upsert_case("case-1", ["case-1"] + [""] * 12)
        self.assertEqual(first["action"], "created")
        self.assertEqual(first["row"], 2)

        second = integration.upsert_case("case-1", ["case-1"] + [""] * 12)
        self.assertEqual(second["action"], "updated")
        self.assertEqual(second["row"], 2)

        update_call = stub.calls[-1]
        self.assertEqual(update_call["method"], "PUT")
        self.assertIn("Evidence%21A2%3AM2", update_call["url"])

    def test_build_case_row_flattens_investigation(self):
        integration = GoogleSheetsIntegration(make_settings())

        row = integration.build_case_row(
            "case-9",
            "2026-01-01T00:00:00+00:00",
            {
                "threat_type": "Banking Phishing",
                "risk_score": 91,
                "risk_level": "HIGH",
                "is_scam": True,
                "confidence": 95,
                "phone_numbers": ["+919999999999"],
                "emails": ["a@b.com"],
                "urls": ["http://evil.co"],
                "upi_ids": ["pay@okaxis"],
                "bank_names": ["SBI"],
                "amounts": ["Rs.5000"],
            },
        )

        self.assertEqual(len(row), len(EVIDENCE_COLUMNS))
        self.assertEqual(row[0], "case-9")
        self.assertEqual(row[2], "Banking Phishing")
        self.assertEqual(row[7], "+919999999999")
        self.assertEqual(row[9], "http://evil.co")


# --------------------------------------------------
# Gmail
# --------------------------------------------------

class TestGmail(unittest.TestCase):

    PROFILE = StubResponse({
        "emailAddress": "analyst@example.com",
        "messagesTotal": 42,
        "threadsTotal": 17,
    })

    def make_gmail(self, *responses, **settings_overrides):
        settings = make_settings(
            write_credentials_file(), **settings_overrides
        )
        stub = StubHTTPClient(*responses)
        return GmailIntegration(settings, http_client=stub), stub

    def test_connect_reads_profile(self):
        integration, _ = self.make_gmail(token_response(), self.PROFILE)

        integration.connect()

        self.assertTrue(integration.is_connected())
        self.assertEqual(
            integration.get_connection_info()["email"], "analyst@example.com"
        )
        self.assertEqual(
            integration.get_status().state, IntegrationState.CONNECTED
        )

    def test_send_email_builds_a_raw_mime_message(self):
        integration, stub = self.make_gmail(
            token_response(),
            self.PROFILE,
            StubResponse({"id": "msg-1", "threadId": "thread-1"}),
        )
        integration.connect()

        result = integration.send_email(
            "stakeholder@example.com", "TraceAI Report", "Body text"
        )

        self.assertEqual(result["id"], "msg-1")
        send_call = stub.calls[-1]
        self.assertTrue(send_call["url"].endswith("/messages/send"))

        import base64

        raw = base64.urlsafe_b64decode(send_call["json"]["raw"])
        self.assertIn(b"stakeholder@example.com", raw)
        self.assertIn(b"TraceAI Report", raw)
        self.assertIn(b"Body text", raw)

    def test_send_email_validates_input_before_calling_google(self):
        integration, stub = self.make_gmail(token_response(), self.PROFILE)
        integration.connect()
        calls_after_connect = len(stub.calls)

        for to, subject, body in (
            ("not-an-email", "s", "b"),
            ("a@b.com", "  ", "b"),
            ("a@b.com", "s", " "),
        ):
            with self.assertRaises(ValueError):
                integration.send_email(to, subject, body)

        self.assertEqual(len(stub.calls), calls_after_connect)

    def test_list_messages_validates_bounds(self):
        integration, _ = self.make_gmail(
            token_response(),
            self.PROFILE,
            StubResponse({"messages": [{"id": "m1", "threadId": "t1"}]}),
        )
        integration.connect()

        messages = integration.list_messages("is:unread", max_results=5)
        self.assertEqual(messages, [{"id": "m1", "thread_id": "t1"}])

        with self.assertRaises(ValueError):
            integration.list_messages(max_results=0)
        with self.assertRaises(ValueError):
            integration.list_messages(max_results=101)
        with self.assertRaises(ValueError):
            integration.list_messages(max_results=True)


# --------------------------------------------------
# Credentials handling
# --------------------------------------------------

class TestGoogleCredentials(unittest.TestCase):

    def test_missing_file_is_not_configured_and_hides_the_path(self):
        integration = GoogleDriveIntegration(
            make_settings("/nonexistent/service-account.json")
        )

        status = integration.get_status()

        self.assertFalse(status.configured)
        self.assertEqual(status.state, IntegrationState.NOT_CONFIGURED)
        self.assertIn("not found on the server", status.detail)
        self.assertNotIn("/nonexistent", status.detail)

    def test_wrong_type_file_is_not_configured(self):
        path = write_credentials_file(
            credential_type="installed_app",
            installed={"client_id": "x"},
        )
        integration = GmailIntegration(
            make_settings(path, GOOGLE_GMAIL_CREDENTIALS_FILE=path)
        )

        status = integration.get_status()

        self.assertFalse(status.configured)
        self.assertIn("service account or authorized-user", status.detail)
        self.assertNotIn(path, status.detail)

    def test_service_account_without_private_key_is_not_configured(self):
        path = write_credentials_file(
            credential_type="service_account",
            client_email="bot@project.iam.gserviceaccount.com",
        )
        integration = GoogleDriveIntegration(
            make_settings(
                path, GOOGLE_DRIVE_CREDENTIALS_FILE=path
            )
        )

        self.assertFalse(integration.is_configured())
        self.assertIn(
            "private_key", integration.get_status().detail
        )

    def test_shared_google_credentials_file_is_honoured(self):
        """One shared credentials file can serve Drive/Sheets/Gmail."""

        path = write_credentials_file()

        for cls in (GoogleDriveIntegration, GoogleSheetsIntegration, GmailIntegration):
            integration = cls(
                make_settings(None, GOOGLE_CREDENTIALS_FILE=path)
            )
            self.assertTrue(integration.is_configured(), cls.__name__)
            # missing_settings() must not name the provider-specific var.
            self.assertEqual(integration.missing_settings(), [])

    def test_secret_redaction_scrubs_tokens_and_paths(self):
        path = write_credentials_file()
        integration = GoogleDriveIntegration(
            make_settings(path, GOOGLE_DRIVE_CREDENTIALS_FILE=path)
        )

        redacted = integration._redact(
            f"token {FAKE_ACCESS_TOKEN} refresh {FAKE_REFRESH_TOKEN} "
            f"secret {FAKE_CLIENT_SECRET} file {path}"
        )

        self.assertNotIn(FAKE_ACCESS_TOKEN, redacted)
        self.assertNotIn(FAKE_REFRESH_TOKEN, redacted)
        self.assertNotIn(FAKE_CLIENT_SECRET, redacted)
        self.assertNotIn(path, redacted)
        self.assertIn("[REDACTED]", redacted)


# --------------------------------------------------
# API endpoints (called directly, no HTTP server)
# --------------------------------------------------

class TestGoogleEndpoints(unittest.TestCase):
    """
    The endpoint functions must map the honest outcomes to honest HTTP
    codes: 409 without usable credentials, 200 only after a real
    authorized call, and never a 200 while unconfigured.
    """

    def test_connect_unconfigured_gmail_is_409(self):
        from backend.api import integrations_connect

        integration = get_integration("gmail")
        original_settings = integration.settings

        try:
            integration.settings = make_settings()
            with self.assertRaises(HTTPException) as ctx:
                integrations_connect("gmail")

            self.assertEqual(ctx.exception.status_code, 409)
            self.assertEqual(ctx.exception.detail["status"], "not_configured")
        finally:
            integration.settings = original_settings

    def test_connect_with_stubbed_transport_reaches_connected(self):
        from backend.api import integrations_connect

        integration = get_integration("google_drive")
        original_settings = integration.settings
        original_client = integration._http_client

        try:
            integration.settings = make_settings(write_credentials_file())
            integration._http_client = StubHTTPClient(
                token_response(), drive_about_response()
            )

            payload = integrations_connect("google_drive")

            self.assertTrue(payload["connected"])
            self.assertEqual(payload["state"], IntegrationState.CONNECTED.value)
            self.assertEqual(
                payload["connection_info"]["account"],
                "scamnet@project.iam.gserviceaccount.com",
            )
        finally:
            integration.settings = original_settings
            integration._http_client = original_client
            integration.disconnect()

    def test_status_endpoint_includes_gmail(self):
        from backend.api import integrations_status

        payload = integrations_status()
        self.assertIn("gmail", payload)
        self.assertFalse(payload["gmail"]["connected"])


# --------------------------------------------------
# Evidence archiver
# --------------------------------------------------

class StubIntegration:
    """Minimal connected/unconnected integration for archiver tests."""

    def __init__(self, connected=True, fail=False):
        self.connected = connected
        self.fail = fail
        self.uploads = []
        self.upserts = []

    def is_connected(self):
        return self.connected

    def upload_report(self, title, markdown, file_id=None):
        if self.fail:
            raise RuntimeError("drive exploded")
        self.uploads.append(
            {"title": title, "markdown": markdown, "file_id": file_id}
        )
        return {"id": "file-1", "name": "report.md", "link": "https://x/file-1"}

    def build_case_row(self, case_id, updated_at, investigation):
        return [case_id, updated_at]

    def upsert_case(self, case_id, row):
        if self.fail:
            raise RuntimeError("sheets exploded")
        self.upserts.append({"case_id": case_id, "row": row})
        return {"action": "created", "row": 2, "spreadsheet_id": "sheet-1"}


class TestEvidenceArchive(unittest.TestCase):

    INVESTIGATION = {"threat_type": "Banking Phishing", "risk_score": 91}
    REPORT = {"title": "TraceAI Report", "markdown": "# report"}

    def test_skips_disconnected_apps(self):
        archiver = EvidenceArchiver(
            drive=StubIntegration(connected=False),
            sheets=StubIntegration(connected=False),
        )

        result = archiver.export("case-1", self.INVESTIGATION, self.REPORT)

        self.assertEqual(result["google_drive"]["status"], "skipped")
        self.assertEqual(result["google_drive"]["reason"], "not_connected")
        self.assertEqual(result["google_sheets"]["status"], "skipped")
        self.assertEqual(result["google_sheets"]["reason"], "not_connected")

    def test_uploads_report_and_upserts_case_when_connected(self):
        drive = StubIntegration()
        sheets = StubIntegration()
        archiver = EvidenceArchiver(drive=drive, sheets=sheets)

        result = archiver.export("case-1", self.INVESTIGATION, self.REPORT)

        self.assertEqual(result["google_drive"]["status"], "uploaded")
        self.assertEqual(result["google_drive"]["file_id"], "file-1")
        self.assertEqual(result["google_sheets"]["status"], "created")
        self.assertEqual(len(drive.uploads), 1)
        self.assertEqual(len(sheets.upserts), 1)
        self.assertEqual(sheets.upserts[0]["case_id"], "case-1")

    def test_reuses_the_existing_drive_file_id(self):
        drive = StubIntegration()
        archiver = EvidenceArchiver(drive=drive, sheets=StubIntegration())

        result = archiver.export(
            "case-1", self.INVESTIGATION, self.REPORT, drive_file_id="file-1"
        )

        self.assertEqual(result["google_drive"]["status"], "updated")
        self.assertEqual(drive.uploads[0]["file_id"], "file-1")

    def test_failures_are_swallowed_and_reported(self):
        archiver = EvidenceArchiver(
            drive=StubIntegration(fail=True),
            sheets=StubIntegration(fail=True),
        )

        result = archiver.export("case-1", self.INVESTIGATION, self.REPORT)

        self.assertEqual(result["google_drive"]["status"], "failed")
        self.assertEqual(result["google_sheets"]["status"], "failed")

    def test_missing_report_skips_the_drive_upload(self):
        archiver = EvidenceArchiver(
            drive=StubIntegration(), sheets=StubIntegration()
        )

        result = archiver.export("case-1", self.INVESTIGATION, None)

        self.assertEqual(result["google_drive"]["reason"], "no_report")


if __name__ == "__main__":
    unittest.main()
