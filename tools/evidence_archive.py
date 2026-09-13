"""
evidence_archive.py
===================
Best-effort export of a finished investigation turn into SCAMNET's
connected Google apps:

* **Google Drive**  - the markdown report is uploaded once and then
  UPDATED on later turns, so a multi-turn case keeps exactly one
  artefact (no duplicate files).
* **Google Sheets** - the accumulated case facts are written as ONE row
  per case (upserted by ``case_id``), so the evidence sheet stays a
  clean, de-duplicated table.

Design rules
------------
* **Never break an investigation.** Archiving is a side effect: every
  failure is logged and swallowed, and the returned dict reports
  ``skipped`` / ``failed`` honestly.
* **Never fake a connection.** Archiving only happens when the
  integration reports a genuine, health-verified session
  (``is_connected()``); an unconfigured app is simply ``skipped``.
* **No secrets.** Only report text and extracted IOCs are sent; tokens
  live inside the integration clients.

Usage
-----
>>> archive = EvidenceArchiver()
>>> archive.export(
...     case_id="session_ab12",
...     investigation=investigation_result,
...     report=report_result,
...     drive_file_id=None,          # reused on later turns
... )
{'google_drive': {'status': 'uploaded', 'file_id': '...'}, ...}
"""

import datetime
import logging
from typing import Any, Dict, Optional

logger = logging.getLogger("SCAMNET-EvidenceArchive")


class EvidenceArchiver:
    """
    Pushes one investigation turn to Drive and Sheets when connected.

    Parameters
    ----------
    drive : BaseIntegration | None
        Drive client override (tests inject a stub). Defaults to the
        registered ``google_drive`` integration.
    sheets : BaseIntegration | None
        Sheets client override (tests inject a stub). Defaults to the
        registered ``google_sheets`` integration.
    """

    def __init__(self, drive=None, sheets=None):

        self._drive = drive
        self._sheets = sheets

    # ----------------------------------------------
    # Collaborator resolution (lazy + injectable)
    # ----------------------------------------------

    @property
    def drive(self):
        """The Drive integration (registry lookup on first use)."""

        if self._drive is None:
            from integrations import get_integration

            self._drive = get_integration("google_drive")

        return self._drive

    @property
    def sheets(self):
        """The Sheets integration (registry lookup on first use)."""

        if self._sheets is None:
            from integrations import get_integration

            self._sheets = get_integration("google_sheets")

        return self._sheets

    # ----------------------------------------------
    # Public API
    # ----------------------------------------------

    def export(
        self,
        case_id: str,
        investigation,
        report,
        drive_file_id: Optional[str] = None,
        updated_at: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Export one turn; returns a per-app outcome map.

        Parameters
        ----------
        case_id : str
            Session / chat identifier used as the Sheets row key and in
            the uploaded file name.
        investigation : InvestigationResult | dict
            Accumulated case facts (IOCs + verdict + risk).
        report : ReportResult | dict | None
            Latest markdown report; when absent the Drive upload is
            skipped (there is nothing to archive yet).
        drive_file_id : str | None
            Drive file id from a previous turn - when given the file is
            updated instead of re-created.
        updated_at : str | None
            Timestamp override (tests); defaults to UTC now.

        Returns
        -------
        dict
            ``{"google_drive": {...}, "google_sheets": {...}}`` where
            each entry is ``{"status": "uploaded" | "updated" | "created"
            | "skipped" | "failed", ...}``.
        """

        timestamp = updated_at or datetime.datetime.now(
            datetime.timezone.utc
        ).isoformat(timespec="seconds")

        return {
            "google_drive": self._export_drive(
                case_id, report, drive_file_id
            ),
            "google_sheets": self._export_sheets(
                case_id, investigation, timestamp
            ),
        }

    # ----------------------------------------------
    # Per-app exporters (each one isolated)
    # ----------------------------------------------

    def _export_drive(
        self,
        case_id: str,
        report,
        drive_file_id: Optional[str],
    ) -> Dict[str, Any]:
        """Upload/update the markdown report in Drive (best effort)."""

        integration = self.drive
        title = _field(report, "title")
        markdown = _field(report, "markdown")

        if integration is None:
            return {"status": "skipped", "reason": "not_registered"}

        if not markdown:
            return {"status": "skipped", "reason": "no_report"}

        if not integration.is_connected():
            return {"status": "skipped", "reason": "not_connected"}

        try:
            result = integration.upload_report(
                title or f"TraceAI Investigation Report ({case_id})",
                markdown,
                file_id=drive_file_id,
            )

        except Exception as exc:  # noqa: BLE001 - best effort by design
            logger.warning(
                "Drive export failed for case %s: %s", case_id, exc
            )
            return {"status": "failed", "reason": str(exc)}

        return {
            "status": "updated" if drive_file_id else "uploaded",
            "file_id": result.get("id"),
            "name": result.get("name"),
            "link": result.get("link"),
        }

    def _export_sheets(
        self,
        case_id: str,
        investigation,
        timestamp: str,
    ) -> Dict[str, Any]:
        """Upsert the case row in Sheets (best effort)."""

        integration = self.sheets

        if integration is None:
            return {"status": "skipped", "reason": "not_registered"}

        if investigation is None:
            return {"status": "skipped", "reason": "no_investigation"}

        if not integration.is_connected():
            return {"status": "skipped", "reason": "not_connected"}

        try:
            row = integration.build_case_row(
                case_id, timestamp, investigation
            )
            result = integration.upsert_case(case_id, row)

        except Exception as exc:  # noqa: BLE001 - best effort by design
            logger.warning(
                "Sheets export failed for case %s: %s", case_id, exc
            )
            return {"status": "failed", "reason": str(exc)}

        return {
            "status": result.get("action", "written"),
            "row": result.get("row"),
            "spreadsheet_id": result.get("spreadsheet_id"),
        }


def _field(source, name: str) -> Any:
    """Read ``name`` from a pydantic model or a plain dict."""

    if source is None:
        return None

    if isinstance(source, dict):
        return source.get(name)

    return getattr(source, name, None)
