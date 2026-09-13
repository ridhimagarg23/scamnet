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
* **Gmail**         - the finished report is e-mailed to the configured
  stakeholder address(es). By default this happens ONCE per case (the
  first turn that produced a report); set
  ``GOOGLE_GMAIL_SEND_EVERY_TURN=1`` to re-send on every turn.

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
...     report_already_emailed=False,
... )
{'google_drive': {'status': 'uploaded', 'file_id': '...'},
 'google_sheets': {'status': 'created', ...},
 'gmail': {'status': 'sent', 'recipients': [...]}}
"""

import datetime
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger("SCAMNET-EvidenceArchive")


class EvidenceArchiver:
    """
    Pushes one investigation turn to Drive, Sheets and Gmail when
    connected.

    Parameters
    ----------
    drive : BaseIntegration | None
        Drive client override (tests inject a stub). Defaults to the
        registered ``google_drive`` integration.
    sheets : BaseIntegration | None
        Sheets client override (tests inject a stub). Defaults to the
        registered ``google_sheets`` integration.
    gmail : BaseIntegration | None
        Gmail client override (tests inject a stub). Defaults to the
        registered ``gmail`` integration.
    settings : config.Settings | None
        Settings override (tests inject a stub). Defaults to the shared
        ``config.settings`` singleton - read for the Gmail recipient
        list, subject prefix and send-every-turn flag.
    """

    def __init__(self, drive=None, sheets=None, gmail=None, settings=None):

        self._drive = drive
        self._sheets = sheets
        self._gmail = gmail
        self._settings = settings

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

    @property
    def gmail(self):
        """The Gmail integration (registry lookup on first use)."""

        if self._gmail is None:
            from integrations import get_integration

            self._gmail = get_integration("gmail")

        return self._gmail

    @property
    def settings(self):
        """The shared settings singleton (or the injected stub)."""

        if self._settings is None:
            from config import settings as app_settings

            self._settings = app_settings

        return self._settings

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
        report_already_emailed: bool = False,
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
            Latest markdown report; when absent the Drive upload and
            the Gmail delivery are skipped (nothing to archive yet).
        drive_file_id : str | None
            Drive file id from a previous turn - when given the file is
            updated instead of re-created.
        updated_at : str | None
            Timestamp override (tests); defaults to UTC now.
        report_already_emailed : bool
            Whether a previous turn already e-mailed this case's report.
            Honoured only when ``GOOGLE_GMAIL_SEND_EVERY_TURN`` is off
            (the default), so a multi-turn case does not spam the inbox.

        Returns
        -------
        dict
            ``{"google_drive": {...}, "google_sheets": {...},
            "gmail": {...}}`` where each entry is
            ``{"status": "uploaded" | "updated" | "created" | "sent" |
            "skipped" | "failed", ...}``.
        """

        timestamp = updated_at or datetime.datetime.now(
            datetime.timezone.utc
        ).isoformat(timespec="seconds")

        drive_result = self._export_drive(
            case_id, report, drive_file_id
        )

        return {
            "google_drive": drive_result,
            "google_sheets": self._export_sheets(
                case_id, investigation, timestamp
            ),
            "gmail": self._export_gmail(
                case_id,
                investigation,
                report,
                drive_result,
                report_already_emailed=report_already_emailed,
            ),
        }

    def send_report(
        self,
        case_id: str,
        investigation,
        report,
        recipients: Optional[List[str]] = None,
        drive_link: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        E-mail a finished report ON DEMAND (outside the /analyze turn).

        Same honest contract as the automatic workflow, but the caller
        chooses the recipients (defaults to the configured list) and the
        once-per-case guard does NOT apply - an explicit send is always
        attempted. Used by POST /api/integrations/gmail/send-report.

        Returns ``{"status": "sent" | "skipped" | "failed", ...}``.
        """

        integration = self.gmail

        if integration is None:
            return {"status": "skipped", "reason": "not_registered"}

        if not _field(report, "markdown"):
            return {"status": "skipped", "reason": "no_report"}

        if not integration.is_connected():
            return {"status": "skipped", "reason": "not_connected"}

        addresses = recipients or self._gmail_recipients()
        addresses = [
            str(a).strip() for a in addresses
            if str(a).strip() and "@" in str(a)
        ]

        if not addresses:
            return {"status": "skipped", "reason": "no_recipients"}

        subject = self._gmail_subject(_field(report, "title"), case_id)
        body = self._gmail_body(
            case_id,
            investigation,
            report,
            {"link": drive_link} if drive_link else {},
        )

        sent_to: List[str] = []
        errors: List[str] = []

        for address in addresses:
            try:
                integration.send_email(address, subject, body)
                sent_to.append(address)
            except Exception as exc:  # noqa: BLE001 - best effort
                logger.warning(
                    "On-demand Gmail report to %s failed for case %s: %s",
                    address, case_id, exc,
                )
                errors.append(str(exc))

        if not sent_to:
            return {
                "status": "failed",
                "reason": errors[0] if errors else "delivery_failed",
                "recipients": addresses,
            }

        result: Dict[str, Any] = {
            "status": "sent",
            "recipients": sent_to,
            "subject": subject,
        }
        if errors:
            result["partial_errors"] = errors
        return result

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

    def _export_gmail(
        self,
        case_id: str,
        investigation,
        report,
        drive_result: Dict[str, Any],
        report_already_emailed: bool = False,
    ) -> Dict[str, Any]:
        """
        E-mail the finished report to the configured recipients.

        Best effort, exactly like the Drive/Sheets exporters: a missing
        connection, an empty recipient list or a rejected send is
        reported honestly (``skipped`` / ``failed``) and never breaks
        the investigation.
        """

        integration = self.gmail

        if integration is None:
            return {"status": "skipped", "reason": "not_registered"}

        markdown = _field(report, "markdown")
        title = _field(report, "title")

        if not markdown:
            return {"status": "skipped", "reason": "no_report"}

        if not integration.is_connected():
            return {"status": "skipped", "reason": "not_connected"}

        recipients = self._gmail_recipients()

        if not recipients:
            return {"status": "skipped", "reason": "no_recipients"}

        # Once-per-case default: a multi-turn investigation regenerates
        # the report every turn, but re-e-mailing it each time would
        # spam the stakeholder. GOOGLE_GMAIL_SEND_EVERY_TURN=1 opts in.
        send_every_turn = bool(
            getattr(self.settings, "GOOGLE_GMAIL_SEND_EVERY_TURN", False)
        )
        if report_already_emailed and not send_every_turn:
            return {"status": "skipped", "reason": "already_sent"}

        subject = self._gmail_subject(title, case_id)
        body = self._gmail_body(
            case_id, investigation, report, drive_result
        )

        sent_to: List[str] = []
        errors: List[str] = []

        for address in recipients:
            try:
                integration.send_email(address, subject, body)
                sent_to.append(address)
            except Exception as exc:  # noqa: BLE001 - best effort
                logger.warning(
                    "Gmail report delivery to %s failed for case %s: %s",
                    address, case_id, exc,
                )
                errors.append(str(exc))

        if not sent_to:
            return {
                "status": "failed",
                "reason": errors[0] if errors else "delivery_failed",
                "recipients": recipients,
            }

        result: Dict[str, Any] = {
            "status": "sent",
            "recipients": sent_to,
            "subject": subject,
        }
        if errors:
            result["partial_errors"] = errors

        return result

    # ----------------------------------------------
    # Gmail content helpers
    # ----------------------------------------------

    def _gmail_recipients(self) -> List[str]:
        """Configured report recipients (never a secret)."""

        value = getattr(
            self.settings, "GOOGLE_GMAIL_REPORT_RECIPIENTS", None
        )

        if isinstance(value, str):
            return [
                item.strip()
                for item in value.replace(";", ",").split(",")
                if item.strip() and "@" in item
            ]

        if isinstance(value, (list, tuple)):
            return [str(item).strip() for item in value if str(item).strip()]

        return []

    def _gmail_subject(self, title: Optional[str], case_id: str) -> str:
        """Build the report e-mail subject line."""

        prefix = getattr(
            self.settings,
            "GOOGLE_GMAIL_REPORT_SUBJECT_PREFIX",
            "[TraceAI] Scam Investigation Report",
        ) or "[TraceAI] Scam Investigation Report"

        if title and str(title).strip():
            return f"{prefix}: {str(title).strip()}"

        return f"{prefix} ({case_id})"

    def _gmail_body(
        self,
        case_id: str,
        investigation,
        report,
        drive_result: Dict[str, Any],
    ) -> str:
        """
        Compose the plain-text report e-mail.

        Leads with a short, scannable summary (threat type, risk, IOCs)
        and then the full markdown report. When Drive archived the same
        report, its link is included so the stakeholder can open the
        canonical artefact.
        """

        lines: List[str] = []

        lines.append("TraceAI - Undercover Scam Investigation Report")
        lines.append("=" * 52)
        lines.append("")
        lines.append(f"Case ID: {case_id}")

        threat_type = _field(investigation, "threat_type")
        risk_score = _field(investigation, "risk_score")
        risk_level = _field(investigation, "risk_level")
        is_scam = _field(investigation, "is_scam")

        if threat_type:
            lines.append(f"Threat type: {threat_type}")
        if risk_score is not None:
            lines.append(f"Risk score: {risk_score}/100 ({risk_level or 'n/a'})")
        if is_scam is not None:
            lines.append(f"Verdict: {'SCAM' if is_scam else 'not a scam'}")

        # IOC summary - the evidence that backs the verdict.
        ioc_lines = self._ioc_summary(investigation)
        if ioc_lines:
            lines.append("")
            lines.append("Indicators of Compromise:")
            lines.extend(f"  - {line}" for line in ioc_lines)

        drive_link = (drive_result or {}).get("link")
        if drive_link:
            lines.append("")
            lines.append(f"Archived report (Google Drive): {drive_link}")

        lines.append("")
        lines.append("-" * 52)
        lines.append("Full report")
        lines.append("-" * 52)
        lines.append("")
        lines.append(str(_field(report, "markdown") or "").strip())
        lines.append("")
        lines.append(
            "This report was generated automatically by TraceAI. "
            "Never share OTPs, passwords or money with unverified parties."
        )

        return "\n".join(lines)

    @staticmethod
    def _ioc_summary(investigation) -> List[str]:
        """Flatten the collected IOC families into short bullet lines."""

        if investigation is None:
            return []

        families = (
            ("phone_numbers", "Phone"),
            ("emails", "Email"),
            ("urls", "URL"),
            ("upi_ids", "UPI"),
            ("bank_names", "Bank"),
            ("amounts", "Amount"),
        )

        summary: List[str] = []

        for attribute, label in families:
            value = _field(investigation, attribute)

            if isinstance(value, (list, tuple, set)):
                items = [str(item) for item in value if str(item).strip()]
            elif value not in (None, ""):
                items = [str(value)]
            else:
                items = []

            if items:
                summary.append(f"{label}: {', '.join(items)}")

        return summary


def _field(source, name: str) -> Any:
    """Read ``name`` from a pydantic model or a plain dict."""

    if source is None:
        return None

    if isinstance(source, dict):
        return source.get(name)

    return getattr(source, name, None)
