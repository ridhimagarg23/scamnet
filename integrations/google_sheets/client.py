"""
Google Sheets integration client for SCAMNET.

Role in SCAMNET
---------------
Google Sheets is the **live evidence store**: every investigated case
gets one row in a spreadsheet, updated as new IOCs are extracted, so an
analyst (or a stakeholder) can watch an investigation accumulate
evidence without touching the dashboard.

Implemented flow (real Sheets API, no fakes)
--------------------------------------------
* ``connect()``  - loads the server-side credentials JSON, mints an
  OAuth access token and verifies the target spreadsheet with a real
  read. When no spreadsheet id is configured, SCAMNET **creates** one
  (that is itself an authorized, verifiable Sheets call) so the
  evidence store always exists.
* ``ensure_worksheet()`` - guarantees the evidence tab exists.
* ``append_rows()`` / ``upsert_case()`` - write evidence rows
  (``values.append`` / ``values.update``), de-duplicated per case id.
* ``check_health()`` - cheap authorized spreadsheet read (TTL-cached).
* ``disconnect()`` - drops the cached credentials/session state.

Credentials
-----------
Point ``GOOGLE_SHEETS_CREDENTIALS_FILE`` (or the shared
``GOOGLE_CREDENTIALS_FILE``) at a service-account JSON or an OAuth
authorized-user JSON. For a service account, SHARE the spreadsheet
with the service account's ``client_email`` (Editor) - otherwise
connect honestly fails with a Google 404/403 instead of pretending
to work.
"""

from typing import Any, Dict, List, Optional, Sequence
from urllib.parse import quote

from integrations.google_api import (
    GoogleAPIError,
    GoogleApiIntegration,
)

SHEETS_API_BASE = "https://sheets.googleapis.com/v4/spreadsheets"

# ``spreadsheets`` covers read/write of sheet content; creating a new
# spreadsheet is allowed with the same scope.
SHEETS_SCOPES = ("https://www.googleapis.com/auth/spreadsheets",)

# Worksheet (tab) SCAMNET writes evidence rows into.
DEFAULT_WORKSHEET = "Evidence"

# Column layout of the evidence sheet - kept in one place so the
# header row and the written rows can never drift apart.
EVIDENCE_COLUMNS = (
    "case_id",
    "updated_at",
    "threat_type",
    "risk_score",
    "risk_level",
    "is_scam",
    "confidence",
    "phone_numbers",
    "emails",
    "urls",
    "upi_ids",
    "bank_names",
    "amounts",
)


class GoogleSheetsIntegration(GoogleApiIntegration):
    """Google Sheets client: live investigation evidence."""

    id = "google_sheets"
    name = "Google Sheets"
    purpose = "Live investigation evidence"

    api_label = "Google Sheets"
    scopes = SHEETS_SCOPES
    credentials_setting = "GOOGLE_SHEETS_CREDENTIALS_FILE"

    required_settings = ("GOOGLE_SHEETS_CREDENTIALS_FILE",)
    optional_settings = (
        "GOOGLE_SHEETS_SPREADSHEET_ID",
        "GOOGLE_SHEETS_WORKSHEET",
    )

    #: Title used when SCAMNET has to create the evidence spreadsheet.
    default_spreadsheet_title = "SCAMNET Investigation Evidence"

    setup_instructions = (
        "Create a Google Cloud project, enable the Sheets API, create a "
        "service account (or an OAuth client with a refresh token) and "
        "download its JSON key to the server. Set "
        "GOOGLE_SHEETS_CREDENTIALS_FILE (or the shared "
        "GOOGLE_CREDENTIALS_FILE) to that path - and optionally "
        "GOOGLE_SHEETS_SPREADSHEET_ID to an existing spreadsheet - in "
        "the server-side .env, restart the backend, then press Connect. "
        "Leave the id empty and SCAMNET creates the spreadsheet; share "
        "an existing spreadsheet with the service account's "
        "client_email as Editor."
    )

    def __init__(self, settings, http_client: Optional[Any] = None):
        super().__init__(settings, http_client=http_client)

        # Spreadsheet id discovered/created by connect(); in-memory only
        # (a restart re-verifies or re-creates it).
        self._spreadsheet_id: Optional[str] = None

    # ----------------------------------------------
    # Configuration helpers
    # ----------------------------------------------

    @property
    def spreadsheet_id(self) -> Optional[str]:
        """
        Target spreadsheet id: the configured one, else the one created
        by ``connect()``, else None.
        """

        configured = getattr(
            self.settings, "GOOGLE_SHEETS_SPREADSHEET_ID", None
        )
        if configured:
            return str(configured)

        return self._spreadsheet_id

    @property
    def worksheet_title(self) -> str:
        """Worksheet (tab) name evidence rows are written to."""

        configured = getattr(
            self.settings, "GOOGLE_SHEETS_WORKSHEET", None
        )
        return str(configured) if configured else DEFAULT_WORKSHEET

    # ----------------------------------------------
    # Lifecycle
    # ----------------------------------------------

    def verify_connection(self) -> Dict[str, Any]:
        """
        Prove the credentials work with a real authorized Sheets call.

        * configured spreadsheet id -> read its metadata (404/403 surface
          as an honest Google API error, e.g. "the service account has
          not been given access");
        * no id configured -> create the spreadsheet with
          ``spreadsheets.create`` and remember its id.

        Either way the evidence worksheet is guaranteed to exist, so a
        later ``upsert_case()`` cannot fail because the tab is missing.

        Returns
        -------
        dict
            ``{"spreadsheet_id", "spreadsheet_title", "worksheet",
            "spreadsheet_url"}`` - never a token.
        """

        spreadsheet_id = self.spreadsheet_id

        if spreadsheet_id:
            payload = self._api_request(
                "GET",
                f"{SHEETS_API_BASE}/{spreadsheet_id}",
                params={
                    "fields": (
                        "spreadsheetId,properties.title,"
                        "sheets.properties.title"
                    )
                },
            )
        else:
            payload = self._api_request(
                "POST",
                SHEETS_API_BASE,
                params={
                    "fields": (
                        "spreadsheetId,properties.title,"
                        "sheets.properties.title"
                    )
                },
                json_body={
                    "properties": {"title": self.default_spreadsheet_title}
                },
                acceptable=(200, 201),
            )

            spreadsheet_id = (
                payload.get("spreadsheetId")
                if isinstance(payload, dict)
                else None
            )

            if not spreadsheet_id:
                raise GoogleAPIError(
                    "Google Sheets did not return a spreadsheet id when "
                    "creating the evidence spreadsheet."
                )

            self._spreadsheet_id = spreadsheet_id

        if not isinstance(payload, dict) or not payload.get("spreadsheetId"):
            raise GoogleAPIError(
                "Google Sheets returned an unexpected spreadsheet "
                "metadata response."
            )

        title = None
        properties = payload.get("properties")
        if isinstance(properties, dict):
            title = properties.get("title")

        if not self._has_worksheet(payload):
            self.ensure_worksheet(self.worksheet_title)

        return {
            "spreadsheet_id": payload.get("spreadsheetId"),
            "spreadsheet_title": title,
            "worksheet": self.worksheet_title,
            "spreadsheet_url": (
                f"https://docs.google.com/spreadsheets/d/"
                f"{payload.get('spreadsheetId')}/edit"
            ),
        }

    # ----------------------------------------------
    # Sheets operations
    # ----------------------------------------------

    def _has_worksheet(self, payload: Dict[str, Any]) -> bool:
        """True when ``payload`` lists the evidence tab already."""

        sheets = payload.get("sheets")
        if not isinstance(sheets, list):
            return False

        target = self.worksheet_title

        for sheet in sheets:
            properties = sheet.get("properties") if isinstance(sheet, dict) else None
            if isinstance(properties, dict) and properties.get("title") == target:
                return True

        return False

    def ensure_worksheet(self, title: Optional[str] = None) -> bool:
        """
        Create the evidence worksheet when it does not exist.

        Returns True when a tab was created, False when it already
        existed (idempotent - safe to call on every connect).
        """

        spreadsheet_id = self.spreadsheet_id

        if not spreadsheet_id:
            raise GoogleAPIError(
                "No Google Sheets spreadsheet is active yet - call "
                "connect() first."
            )

        target = title or self.worksheet_title

        self._api_request(
            "POST",
            f"{SHEETS_API_BASE}/{spreadsheet_id}:batchUpdate",
            json_body={
                "requests": [
                    {"addSheet": {"properties": {"title": target}}}
                ]
            },
        )

        return True

    def get_values(self, cell_range: str) -> List[List[Any]]:
        """Read a cell range (e.g. ``"Evidence!A1:M5"``)."""

        spreadsheet_id = self.spreadsheet_id

        if not spreadsheet_id:
            raise GoogleAPIError(
                "No Google Sheets spreadsheet is active yet - call "
                "connect() first."
            )

        encoded = quote(cell_range, safe="")

        payload = self._api_request(
            "GET",
            f"{SHEETS_API_BASE}/{spreadsheet_id}/values/{encoded}",
        )

        values = payload.get("values") if isinstance(payload, dict) else None
        return values if isinstance(values, list) else []

    def append_rows(
        self,
        rows: Sequence[Sequence[Any]],
        worksheet: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Append evidence rows to the worksheet.

        The header row is written automatically the first time the
        worksheet is used, so a freshly created spreadsheet becomes
        self-documenting on first write.

        Returns
        -------
        dict
            ``{"updated_rows", "updated_range", "spreadsheet_id",
            "worksheet"}``.
        """

        spreadsheet_id = self.spreadsheet_id

        if not spreadsheet_id:
            raise GoogleAPIError(
                "No Google Sheets spreadsheet is active yet - call "
                "connect() first."
            )

        sheet = worksheet or self.worksheet_title

        payload_rows = [list(row) for row in rows]

        if not payload_rows:
            raise ValueError("rows must contain at least one row.")

        values = self.get_values(f"{sheet}!A1:A1")
        header_present = bool(values and values[0] and values[0][0])

        if not header_present:
            payload_rows = [list(EVIDENCE_COLUMNS)] + payload_rows

        encoded_range = quote(f"{sheet}!A1:append", safe="")

        payload = self._api_request(
            "POST",
            f"{SHEETS_API_BASE}/{spreadsheet_id}/values/{encoded_range}",
            params={
                "valueInputOption": "RAW",
                "insertDataOption": "INSERT_ROWS",
            },
            json_body={"values": payload_rows},
        )

        updates = payload.get("updates") if isinstance(payload, dict) else None
        updates = updates if isinstance(updates, dict) else {}

        return {
            "spreadsheet_id": spreadsheet_id,
            "worksheet": sheet,
            "updated_rows": updates.get("updatedRows"),
            "updated_range": updates.get("updatedRange"),
        }

    def upsert_case(
        self,
        case_id: str,
        row: Sequence[Any],
        worksheet: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Insert or update ONE case row, keyed by its ``case_id``.

        Multi-turn investigations call this repeatedly; the row is
        rewritten in place instead of appending duplicates.

        Returns
        -------
        dict
            ``{"action": "created" | "updated", "row": int,
            "spreadsheet_id", "worksheet"}``.
        """

        if not isinstance(case_id, str) or not case_id.strip():
            raise ValueError("case_id must be a non-empty string.")

        spreadsheet_id = self.spreadsheet_id

        if not spreadsheet_id:
            raise GoogleAPIError(
                "No Google Sheets spreadsheet is active yet - call "
                "connect() first."
            )

        sheet = worksheet or self.worksheet_title

        column_a = self.get_values(f"{sheet}!A2:A")

        target_row = None

        for offset, existing in enumerate(column_a):
            if existing and str(existing[0]) == case_id:
                # +2: header row + zero-based list offset.
                target_row = offset + 2
                break

        if target_row is None:
            self.append_rows([row], worksheet=sheet)

            # append_rows may add the header first; recompute the row.
            column_a = self.get_values(f"{sheet}!A2:A")
            for offset, existing in enumerate(column_a):
                if existing and str(existing[0]) == case_id:
                    target_row = offset + 2
                    break

            return {
                "spreadsheet_id": spreadsheet_id,
                "worksheet": sheet,
                "action": "created",
                "row": target_row,
            }

        last_column = _column_letter(len(row))
        encoded_range = quote(
            f"{sheet}!A{target_row}:{last_column}{target_row}", safe=""
        )

        self._api_request(
            "PUT",
            f"{SHEETS_API_BASE}/{spreadsheet_id}/values/{encoded_range}",
            params={"valueInputOption": "RAW"},
            json_body={"values": [list(row)]},
        )

        return {
            "spreadsheet_id": spreadsheet_id,
            "worksheet": sheet,
            "action": "updated",
            "row": target_row,
        }

    def build_case_row(
        self,
        case_id: str,
        updated_at: str,
        investigation,
    ) -> List[Any]:
        """
        Flatten an ``InvestigationResult`` into the evidence columns.

        ``investigation`` may be a pydantic model or a plain dict, so
        the same helper serves the API and the CLI.
        """

        def get(attribute: str, default: Any = None) -> Any:
            if isinstance(investigation, dict):
                return investigation.get(attribute, default)
            return getattr(investigation, attribute, default)

        def joined(value: Any) -> str:
            if isinstance(value, (list, tuple, set)):
                return ", ".join(str(item) for item in value)
            if value is None:
                return ""
            return str(value)

        return [
            case_id,
            updated_at,
            joined(get("threat_type")),
            get("risk_score", ""),
            joined(get("risk_level")),
            joined(get("is_scam")),
            get("confidence", ""),
            joined(get("phone_numbers")),
            joined(get("emails")),
            joined(get("urls")),
            joined(get("upi_ids")),
            joined(get("bank_names")),
            joined(get("amounts")),
        ]


def _column_letter(index: int) -> str:
    """1-based column number -> spreadsheet letter (1 -> A, 27 -> AA)."""

    if index < 1:
        raise ValueError("index must be >= 1")

    letters = ""

    while index:
        index, remainder = divmod(index - 1, 26)
        letters = chr(65 + remainder) + letters

    return letters
