"""
Google Drive integration client for SCAMNET.

Role in SCAMNET
---------------
Google Drive is the **report archive**: every investigation report can be
pushed to a Drive folder as a markdown file, and the dashboard surfaces
the created file id / shareable link so an analyst can open the evidence
directly.

Implemented flow (real Drive API, no fakes)
-------------------------------------------
* ``connect()``  - loads the server-side credentials JSON, mints an
  OAuth access token and calls ``about.get`` (optionally also verifying
  the configured destination folder) before marking the integration
  connected. Nothing is marked connected unless Google answered.
* ``check_health()`` - cheap authorized ``about.get`` ping (TTL-cached).
* ``upload_report()`` - creates the report file (``uploadType=multipart``)
  or updates the same file on later turns (``uploadType=media``), so a
  multi-turn case keeps exactly one Drive artefact.
* ``disconnect()`` - drops the cached credentials/session state.

Credentials
-----------
Point ``GOOGLE_DRIVE_CREDENTIALS_FILE`` (or the shared
``GOOGLE_CREDENTIALS_FILE``) at a service-account JSON or an OAuth
authorized-user JSON. For a service account, share the destination
folder with the service account's ``client_email`` and set
``GOOGLE_DRIVE_FOLDER_ID`` to that folder's id.
"""

import re
from typing import Any, Dict, Optional

from integrations.google_api import (
    DEFAULT_HTTP_TIMEOUT,
    UPLOAD_HTTP_TIMEOUT,
    GoogleAPIError,
    GoogleApiIntegration,
    markdown_multipart_body,
)

# Report files are uploaded through the Drive upload endpoint.
DRIVE_API_BASE = "https://www.googleapis.com/drive/v3"
DRIVE_UPLOAD_BASE = "https://www.googleapis.com/upload/drive/v3/files"

# ``drive`` (rather than ``drive.file``) because a service account must
# be able to see a folder that was shared with it from another account.
DRIVE_SCOPES = ("https://www.googleapis.com/auth/drive",)


class GoogleDriveIntegration(GoogleApiIntegration):
    """Google Drive client: report archive with real authentication."""

    id = "google_drive"
    name = "Google Drive"
    purpose = "Investigation reports"

    api_label = "Google Drive"
    scopes = DRIVE_SCOPES
    credentials_setting = "GOOGLE_DRIVE_CREDENTIALS_FILE"

    required_settings = ("GOOGLE_DRIVE_CREDENTIALS_FILE",)
    optional_settings = ("GOOGLE_DRIVE_FOLDER_ID",)

    setup_instructions = (
        "Create a Google Cloud project, enable the Drive API, create a "
        "service account (or an OAuth client with a refresh token) and "
        "download its JSON key to the server. Set "
        "GOOGLE_DRIVE_CREDENTIALS_FILE (or the shared "
        "GOOGLE_CREDENTIALS_FILE) to that path - and optionally "
        "GOOGLE_DRIVE_FOLDER_ID to the destination folder id - in the "
        "server-side .env, restart the backend, then press Connect. For "
        "a service account, share the destination folder with the "
        "service account's client_email."
    )

    # ----------------------------------------------
    # Configuration helpers
    # ----------------------------------------------

    @property
    def folder_id(self) -> Optional[str]:
        """Configured destination folder id (or None for My Drive root)."""

        value = getattr(self.settings, "GOOGLE_DRIVE_FOLDER_ID", None)
        return str(value) if value else None

    # ----------------------------------------------
    # Lifecycle
    # ----------------------------------------------

    def verify_connection(self) -> Dict[str, Any]:
        """
        Prove the credentials work with a real authorized Drive call.

        Calls ``about.get`` (the account identity) and, when
        ``GOOGLE_DRIVE_FOLDER_ID`` is set, ``files.get`` on that folder
        so a misconfigured folder is caught at connect time instead of
        at report-upload time.

        Returns
        -------
        dict
            ``{"account": str | None, "display_name": str | None,
            "folder_id": str | None, "folder_name": str | None}`` -
            never a token.
        """

        about = self._api_request(
            "GET",
            f"{DRIVE_API_BASE}/about",
            params={
                "fields": "user(emailAddress,displayName),storageQuota(limit)"
            },
        )

        user = about.get("user") if isinstance(about, dict) else None
        user = user if isinstance(user, dict) else {}

        info: Dict[str, Any] = {
            "account": user.get("emailAddress"),
            "display_name": user.get("displayName"),
        }

        folder_id = self.folder_id

        if folder_id:
            folder = self._api_request(
                "GET",
                f"{DRIVE_API_BASE}/files/{folder_id}",
                params={"fields": "id,name,mimeType,trashed"},
            )

            if not isinstance(folder, dict) or not folder.get("id"):
                raise GoogleAPIError(
                    "Google Drive did not return the configured folder "
                    "(check GOOGLE_DRIVE_FOLDER_ID)."
                )

            if folder.get("trashed"):
                raise GoogleAPIError(
                    "The configured Google Drive folder is in the trash "
                    "(check GOOGLE_DRIVE_FOLDER_ID)."
                )

            info["folder_id"] = folder.get("id")
            info["folder_name"] = folder.get("name")

        return info

    # ----------------------------------------------
    # Drive operations
    # ----------------------------------------------

    def upload_report(
        self,
        title: str,
        markdown: str,
        file_id: Optional[str] = None,
        folder_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Create (or update) the markdown report file in Drive.

        Parameters
        ----------
        title : str
            Report title - also used to derive the file name.
        markdown : str
            Complete report body.
        file_id : str | None
            Existing Drive file id. When given the file is UPDATED
            instead of duplicated (multi-turn cases keep one artefact).
        folder_id : str | None
            Destination folder override; defaults to
            ``GOOGLE_DRIVE_FOLDER_ID``.

        Returns
        -------
        dict
            ``{"id", "name", "link"}`` - the link is the Drive
            ``webViewLink`` when Google returned one.

        Raises
        ------
        GoogleAPIError
            The credentials were rejected or Drive refused the upload
            (quota, permissions, ...).
        """

        if not isinstance(title, str) or not title.strip():
            raise ValueError("title must be a non-empty string.")
        if not isinstance(markdown, str) or not markdown.strip():
            raise ValueError("markdown must be a non-empty string.")

        content = markdown.encode("utf-8")
        filename = self._file_name(title)

        if file_id:
            result = self._api_request(
                "PATCH",
                f"{DRIVE_UPLOAD_BASE}/{file_id}",
                params={"uploadType": "media", "fields": "id,name,webViewLink"},
                content=content,
                headers={
                    "Content-Type": "text/markdown; charset=UTF-8",
                },
                acceptable=(200,),
                timeout=UPLOAD_HTTP_TIMEOUT,
            )
        else:
            metadata: Dict[str, Any] = {
                "name": filename,
                "mimeType": "text/markdown",
            }

            destination = folder_id or self.folder_id
            if destination:
                metadata["parents"] = [destination]

            body, content_type = markdown_multipart_body(
                metadata, filename, content
            )

            result = self._api_request(
                "POST",
                DRIVE_UPLOAD_BASE,
                params={
                    "uploadType": "multipart",
                    "fields": "id,name,webViewLink",
                },
                content=body,
                headers={"Content-Type": content_type},
                acceptable=(200, 201),
                timeout=UPLOAD_HTTP_TIMEOUT,
            )

        if not isinstance(result, dict) or not result.get("id"):
            raise GoogleAPIError(
                "Google Drive returned an unexpected upload response "
                "(missing file id)."
            )

        return {
            "id": result.get("id"),
            "name": result.get("name") or filename,
            "link": result.get("webViewLink"),
        }

    def get_file(self, file_id: str) -> Dict[str, Any]:
        """Fetch metadata for one Drive file (used by tests/health checks)."""

        if not isinstance(file_id, str) or not file_id.strip():
            raise ValueError("file_id must be a non-empty string.")

        result = self._api_request(
            "GET",
            f"{DRIVE_API_BASE}/files/{file_id}",
            params={"fields": "id,name,mimeType,trashed,webViewLink"},
            timeout=DEFAULT_HTTP_TIMEOUT,
        )

        if not isinstance(result, dict) or not result.get("id"):
            raise GoogleAPIError(
                "Google Drive returned an unexpected file metadata response."
            )

        return result

    def delete_file(self, file_id: str) -> bool:
        """Permanently delete one Drive file (cleanup/testing helper)."""

        if not isinstance(file_id, str) or not file_id.strip():
            raise ValueError("file_id must be a non-empty string.")

        self._api_request(
            "DELETE",
            f"{DRIVE_API_BASE}/files/{file_id}",
            acceptable=(200, 204),
        )

        return True

    # ----------------------------------------------
    # Helpers
    # ----------------------------------------------

    @staticmethod
    def _file_name(title: str) -> str:
        """Derive a safe Drive file name from the report title."""

        safe = "".join(
            character if character.isalnum() or character in " .-_" else "_"
            for character in title.strip()
        )
        # Collapse the underscore/space runs that special characters
        # leave behind ("Report: /2026/" -> "Report_2026").
        safe = re.sub(r"[\s_]{2,}", "_", safe).strip(" _")[:120]
        safe = safe or "TraceAI Investigation Report"

        if not safe.lower().endswith(".md"):
            safe += ".md"

        return safe
