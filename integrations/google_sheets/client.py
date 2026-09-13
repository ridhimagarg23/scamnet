"""
Google Sheets integration client for SCAMNET.

Role in SCAMNET
---------------
Google Sheets is the live evidence store: create/update investigation
records, append extracted intelligence (phone numbers, UPI IDs, URLs,
names, payment details) and keep entities + investigation status in sync
while the undercover conversation runs.

Current stage (foundation only)
-------------------------------
NO real Sheets API calls are made yet. The client reports honest
configuration status (including whether the credentials file actually
exists on the server - without revealing its path); ``connect()``
raises ``IntegrationNotImplementedError`` until Google authentication
(service account or OAuth) is implemented in a later increment.

To implement the real flow later
--------------------------------
1. Add ``google-auth`` + ``google-api-python-client`` (or ``gspread``)
   to requirements.txt.
2. ``connect()``: load the credentials file, build authorized Sheets
   service credentials (scope: spreadsheets), and verify with a cheap
   read (e.g. spreadsheet metadata). Set ``self._connected = True``
   only on success and flip ``connect_implemented = True``.
3. ``check_health()``: re-verify the spreadsheet is reachable.
4. ``disconnect()``: drop the cached service/credentials.
5. Add evidence-append / status-update operations used by the
   investigation pipeline (reuse existing IOC extraction from
   ``tools/entity_extractor.py`` - do not duplicate it).
"""

import os

from integrations.base import (
    BaseIntegration,
    IntegrationNotConfiguredError,
    IntegrationNotImplementedError,
)


class GoogleSheetsIntegration(BaseIntegration):
    """Google Sheets client (status-only until auth is built)."""

    id = "google_sheets"
    name = "Google Sheets"
    purpose = "Live investigation evidence"

    # Server-side path to the service-account JSON (or OAuth token
    # JSON). The file itself is git-ignored and never leaves server.
    required_settings = ("GOOGLE_SHEETS_CREDENTIALS_FILE",)
    # Optional target spreadsheet; when absent the real implementation
    # may create one per investigation.
    optional_settings = ("GOOGLE_SHEETS_SPREADSHEET_ID",)

    # Flipped to True only when connect() performs real authentication.
    connect_implemented = False

    setup_instructions = (
        "Create a Google Cloud service account (or OAuth client) with "
        "Sheets API access, download its JSON key to the server, set "
        "GOOGLE_SHEETS_CREDENTIALS_FILE (and optionally "
        "GOOGLE_SHEETS_SPREADSHEET_ID) in the server-side .env, restart "
        "the backend, then implement the auth flow in "
        "integrations/google_sheets/client.py."
    )

    # ----------------------------------------------
    # Configuration validity
    # ----------------------------------------------

    def configuration_issues(self):
        """
        The credentials env var must also point at a real file on the
        server. The reported message deliberately omits the path.
        """

        issues = []
        path = getattr(self.settings, "GOOGLE_SHEETS_CREDENTIALS_FILE", None)
        if path and not os.path.isfile(path):
            issues.append(
                "Google Sheets credentials file not found on the server "
                "(path withheld)"
            )
        return issues

    # ----------------------------------------------
    # Lifecycle
    # ----------------------------------------------

    def connect(self) -> None:
        """
        Authorize against the Google Sheets API.

        Not implemented yet: raises honest errors instead of faking a
        successful connection.
        """

        if not self.is_configured():
            raise IntegrationNotConfiguredError(
                "Google Sheets is not configured: set "
                "GOOGLE_SHEETS_CREDENTIALS_FILE (a valid server-side "
                "credentials JSON path) in the .env."
            )

        raise IntegrationNotImplementedError(
            "Google Sheets authentication is not implemented yet. "
            "Server-side setup is required before SCAMNET can connect."
        )

    def disconnect(self) -> None:
        """Drop the authorized session (idempotent no-op for now)."""

        if not self.is_connected():
            return

        raise IntegrationNotImplementedError(
            "Google Sheets disconnect is not implemented yet."
        )

    def check_health(self) -> bool:
        """
        Verify the authorized session can reach the spreadsheet.

        Until a real connection can exist, this honestly returns False.
        """

        return False
