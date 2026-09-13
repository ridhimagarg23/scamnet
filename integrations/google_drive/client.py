"""
Google Drive integration client for SCAMNET.

Role in SCAMNET
---------------
Google Drive is the report archive: create investigation report files,
upload evidence-backed markdown/PDF reports produced by the existing
``ReportAgent`` and return the created file id / shareable link so the
dashboard can surface them.

Current stage (foundation only)
-------------------------------
NO real Drive API calls are made yet. The client reports honest
configuration status (including whether the credentials file actually
exists on the server - without revealing its path); ``connect()``
raises ``IntegrationNotImplementedError`` until Google authentication
(service account or OAuth) is implemented in a later increment.

To implement the real flow later
--------------------------------
1. Add ``google-auth`` + ``google-api-python-client`` (+
   ``google-auth-oauthlib`` for user OAuth) to requirements.txt.
2. ``connect()``: load the credentials file, build authorized Drive
   credentials (scope: drive.file) and verify with ``about.get``. Set
   ``self._connected = True`` only on success and flip
   ``connect_implemented = True``.
3. ``check_health()``: cheap authorized read-only ping.
4. ``disconnect()``: drop the cached service/credentials.
5. Add ``create_report_file(title, markdown) -> {id, link}`` used at
   the end of an investigation (feed it the existing ReportAgent
   output - do not regenerate reports here).
"""

import os

from integrations.base import (
    BaseIntegration,
    IntegrationNotConfiguredError,
    IntegrationNotImplementedError,
)


class GoogleDriveIntegration(BaseIntegration):
    """Google Drive client (status-only until auth is built)."""

    id = "google_drive"
    name = "Google Drive"
    purpose = "Investigation reports"

    # Server-side path to the service-account JSON (or OAuth token
    # JSON). The file itself is git-ignored and never leaves server.
    required_settings = ("GOOGLE_DRIVE_CREDENTIALS_FILE",)
    # Optional destination folder for investigation reports.
    optional_settings = ("GOOGLE_DRIVE_FOLDER_ID",)

    # Flipped to True only when connect() performs real authentication.
    connect_implemented = False

    setup_instructions = (
        "Create a Google Cloud service account (or OAuth client) with "
        "Drive API access, download its JSON key to the server, set "
        "GOOGLE_DRIVE_CREDENTIALS_FILE (and optionally "
        "GOOGLE_DRIVE_FOLDER_ID) in the server-side .env, restart the "
        "backend, then implement the auth flow in "
        "integrations/google_drive/client.py."
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
        path = getattr(self.settings, "GOOGLE_DRIVE_CREDENTIALS_FILE", None)
        if path and not os.path.isfile(path):
            issues.append(
                "Google Drive credentials file not found on the server "
                "(path withheld)"
            )
        return issues

    # ----------------------------------------------
    # Lifecycle
    # ----------------------------------------------

    def connect(self) -> None:
        """
        Authorize against the Google Drive API.

        Not implemented yet: raises honest errors instead of faking a
        successful connection.
        """

        if not self.is_configured():
            raise IntegrationNotConfiguredError(
                "Google Drive is not configured: set "
                "GOOGLE_DRIVE_CREDENTIALS_FILE (a valid server-side "
                "credentials JSON path) in the .env."
            )

        raise IntegrationNotImplementedError(
            "Google Drive authentication is not implemented yet. "
            "Server-side setup is required before SCAMNET can connect."
        )

    def disconnect(self) -> None:
        """Drop the authorized session (idempotent no-op for now)."""

        if not self.is_connected():
            return

        raise IntegrationNotImplementedError(
            "Google Drive disconnect is not implemented yet."
        )

    def check_health(self) -> bool:
        """
        Verify the authorized session can reach Drive.

        Until a real connection can exist, this honestly returns False.
        """

        return False
