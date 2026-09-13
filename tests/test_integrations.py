"""
test_integrations.py
====================
Offline unit tests for the SCAMNET integration layer (integrations/).

No network access and no credentials are needed: these tests pin the
HONEST-STATUS contract of the integration foundation -

* unconfigured integrations report connected=False / available=False;
* connect() never fakes success (raises NotConfigured / NotImplemented);
* status payloads never leak secret values or credential file paths;
* the API endpoint functions map those failures to honest HTTP codes
  (404 / 409 / 501) instead of a fake 200.

Run with:

    OPENROUTER_API_KEY=test-key python -m unittest discover -s tests
"""

import unittest
from types import SimpleNamespace

from fastapi import HTTPException

from integrations import (
    REGISTRY,
    get_all_integration_statuses,
    get_integration,
)
from integrations.base import (
    IntegrationNotConfiguredError,
    IntegrationState,
)
from integrations.telegram import TelegramIntegration
from integrations.google_sheets import GoogleSheetsIntegration
from integrations.google_drive import GoogleDriveIntegration
from integrations.gmail import GmailIntegration


ALL_IDS = ("telegram", "google_sheets", "google_drive", "gmail")

ALL_CLASSES = (
    TelegramIntegration,
    GoogleSheetsIntegration,
    GoogleDriveIntegration,
    GmailIntegration,
)

# Every provider now implements a REAL connect flow (Bot API getMe for
# Telegram; authorized Google API calls for Drive/Sheets/Gmail). The
# mocked-transport coverage for those flows lives in
# tests/test_telegram_integration.py and
# tests/test_google_integrations.py - this module pins the
# honest-status contract that must hold no matter what.


def make_settings(**overrides):
    """
    Stub settings object with every integration key present but empty
    (mirrors config.Settings defaults). Tests inject fake credentials
    through ``overrides`` without touching the real environment.
    """

    base = dict(
        TELEGRAM_BOT_TOKEN=None,
        TELEGRAM_API_BASE="https://api.telegram.org",
        GOOGLE_CREDENTIALS_FILE=None,
        GOOGLE_SHEETS_CREDENTIALS_FILE=None,
        GOOGLE_SHEETS_SPREADSHEET_ID=None,
        GOOGLE_SHEETS_WORKSHEET=None,
        GOOGLE_DRIVE_CREDENTIALS_FILE=None,
        GOOGLE_DRIVE_FOLDER_ID=None,
        GOOGLE_GMAIL_CREDENTIALS_FILE=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


class TestIntegrationRegistry(unittest.TestCase):

    def test_core_integrations_registered(self):
        """The registry exposes exactly the SCAMNET core apps."""

        self.assertEqual(set(REGISTRY.keys()), set(ALL_IDS))
        self.assertIs(get_integration("telegram"), REGISTRY["telegram"])
        self.assertIsNone(get_integration("unknown_app"))

    def test_status_payload_shape(self):
        """GET /api/integrations payload keys match the documented contract."""

        statuses = get_all_integration_statuses()
        self.assertEqual(set(statuses.keys()), set(ALL_IDS))

        for integration_id in ALL_IDS:
            payload = statuses[integration_id]
            for field in (
                "available",
                "connected",
                "configured",
                "name",
                "purpose",
                "state",
                "detail",
                "setup_instructions",
            ):
                self.assertIn(field, payload)
            self.assertIsInstance(payload["available"], bool)
            self.assertIsInstance(payload["connected"], bool)


class TestHonestStatus(unittest.TestCase):

    def test_unconfigured_never_claims_connected(self):
        """Without credentials every app is not_configured, never connected."""

        for cls in ALL_CLASSES:
            integration = cls(make_settings())
            status = integration.get_status()

            self.assertFalse(status.configured, cls.__name__)
            self.assertFalse(status.available, cls.__name__)
            self.assertFalse(status.connected, cls.__name__)
            self.assertEqual(
                status.state, IntegrationState.NOT_CONFIGURED, cls.__name__
            )
            # The missing setting NAMES (not values) are surfaced.
            for key in cls.required_settings:
                self.assertIn(key, status.detail)

    def test_connect_requires_configuration(self):
        """connect() on an unconfigured client fails honestly (409 path)."""

        for cls in ALL_CLASSES:
            integration = cls(make_settings())
            with self.assertRaises(IntegrationNotConfiguredError, msg=cls.__name__):
                integration.connect()
            self.assertFalse(integration.is_connected())

    def test_wrong_credentials_file_never_claims_configured(self):
        """
        A path that exists but is not a Google credentials JSON (this
        test file, for instance) must stay not_configured - the operator
        gets an actionable message instead of a failed API call, and the
        path is never echoed back.
        """

        for cls in (
            GoogleSheetsIntegration,
            GoogleDriveIntegration,
            GmailIntegration,
        ):
            key = cls.required_settings[0]
            integration = cls(make_settings(**{key: __file__}))

            self.assertFalse(integration.is_configured(), cls.__name__)

            with self.assertRaises(IntegrationNotConfiguredError, msg=cls.__name__):
                integration.connect()

            status = integration.get_status()
            self.assertFalse(status.available, cls.__name__)
            self.assertFalse(status.connected, cls.__name__)
            self.assertNotIn(__file__, status.detail)

    def test_google_credentials_file_must_exist(self):
        """
        A credentials path that does not exist on the server keeps the
        integration not_configured - and the path is NOT leaked into
        the API payload.
        """

        missing_path = "/nonexistent-dir/service_account.json"

        for cls in (GoogleSheetsIntegration, GoogleDriveIntegration):
            key = cls.required_settings[0]
            integration = cls(make_settings(**{key: missing_path}))

            self.assertFalse(integration.is_configured(), cls.__name__)
            status = integration.get_status()
            self.assertEqual(
                status.state, IntegrationState.NOT_CONFIGURED, cls.__name__
            )
            self.assertNotIn("/nonexistent-dir", status.detail)
            self.assertIn("not found on the server", status.detail)

    def test_status_never_leaks_secret_values(self):
        """Secret values injected into settings never reach the payload."""

        secret_token = "super-secret-bot-token-12345"
        integration = TelegramIntegration(
            make_settings(TELEGRAM_BOT_TOKEN=secret_token)
        )

        payload_json = integration.get_status().model_dump_json()
        self.assertNotIn(secret_token, payload_json)

    def test_disconnect_is_safe_noop_when_not_connected(self):
        """disconnect() without a live session must not raise or fake state."""

        for cls in ALL_CLASSES:
            integration = cls(make_settings())
            integration.disconnect()  # no-op
            self.assertFalse(integration.is_connected())


class TestIntegrationEndpoints(unittest.TestCase):
    """
    Endpoint functions from backend/api.py are called directly (no HTTP
    server needed). They must translate integration failures into the
    honest HTTP codes the dashboard relies on.
    """

    def test_get_integrations_returns_honest_statuses(self):
        from backend.api import integrations_status

        payload = integrations_status()
        self.assertEqual(set(payload.keys()), set(ALL_IDS))

        for integration_id in ALL_IDS:
            status = payload[integration_id]
            # In the test environment nothing is genuinely authenticated.
            self.assertFalse(status["connected"])
            self.assertIsInstance(status["available"], bool)

    def test_connect_unknown_integration_returns_404(self):
        from backend.api import integrations_connect

        with self.assertRaises(HTTPException) as ctx:
            integrations_connect("unknown_app")
        self.assertEqual(ctx.exception.status_code, 404)

    def test_connect_without_credentials_is_409_not_fake_success(self):
        """
        In the test environment no Google credentials exist, so POST
        connect must answer 409 not_configured - never a 200.
        (Telegram's and Google's real connect flows are covered with
        mocked transports in tests/test_telegram_integration.py and
        tests/test_google_integrations.py.)
        """

        from backend.api import integrations_connect

        integration = get_integration("google_drive")

        with self.assertRaises(HTTPException) as ctx:
            integrations_connect("google_drive")

        if integration.is_configured():
            # A developer machine with real credentials would attempt a
            # genuine connection; either it worked or it failed honestly.
            self.assertIn(ctx.exception.status_code, (502,))
        else:
            self.assertEqual(ctx.exception.status_code, 409)
            self.assertEqual(ctx.exception.detail["status"], "not_configured")

    def test_connect_with_invalid_credentials_file_is_409(self):
        """
        Simulates an operator who pointed GOOGLE_DRIVE_CREDENTIALS_FILE
        at a file that is not a Google credentials JSON: the endpoint
        must answer 409 not_configured (never a fake success) and must
        not echo the path back.
        """

        from backend.api import integrations_connect

        integration = get_integration("google_drive")
        original_settings = integration.settings

        try:
            integration.settings = make_settings(
                GOOGLE_DRIVE_CREDENTIALS_FILE=__file__
            )

            with self.assertRaises(HTTPException) as ctx:
                integrations_connect("google_drive")

            self.assertEqual(ctx.exception.status_code, 409)
            self.assertEqual(ctx.exception.detail["status"], "not_configured")
            self.assertNotIn(__file__, str(ctx.exception.detail))
        finally:
            integration.settings = original_settings

    def test_disconnect_endpoint_is_safe_and_honest(self):
        """POST disconnect clears session state without touching config."""

        from backend.api import integrations_disconnect

        status = integrations_disconnect("google_drive")

        self.assertFalse(status["connected"])
        self.assertIn(status["state"], ("not_configured", "disconnected"))


if __name__ == "__main__":
    unittest.main()
