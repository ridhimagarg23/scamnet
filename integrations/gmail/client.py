"""
Gmail integration client for SCAMNET.

Role in SCAMNET
---------------
Gmail is the **evidence inbox / outbound channel**: analysts forward a
scam e-mail to the monitored mailbox, SCAMNET reads it as raw material
for an investigation and can send the finished report to a stakeholder
address - all through the same honest integration contract as Telegram
(``integrations/telegram``) and the other Google apps.

Implemented flow (real Gmail API, no fakes)
-------------------------------------------
* ``connect()``  - loads the server-side credentials JSON, mints an
  OAuth access token and calls ``users.getProfile`` before marking the
  integration connected.
* ``check_health()`` - cheap authorized ``getProfile`` ping (TTL-cached).
* ``list_messages()`` / ``get_message()`` - read recent mail (evidence).
* ``send_email()`` - send a plain-text report e-mail.
* ``disconnect()`` - drops the cached credentials/session state.

Credentials
-----------
Point ``GOOGLE_GMAIL_CREDENTIALS_FILE`` (or the shared
``GOOGLE_CREDENTIALS_FILE``) at a credentials JSON. Gmail requires a
**user** (OAuth "authorized_user" with a refresh token), because service
accounts only get Gmail access through Google Workspace domain-wide
delegation - a plain service account will honestly fail to connect.
"""

import base64
import re
from email.message import EmailMessage
from typing import Any, Dict, List, Optional

from integrations.google_api import (
    GoogleAPIError,
    GoogleApiIntegration,
)

GMAIL_API_BASE = "https://gmail.googleapis.com/gmail/v1/users/me"

# Working with Gmail requires an OAuth *user* (refresh token) unless the
# Workspace tenant granted domain-wide delegation. ``gmail.send`` lets
# SCAMNET deliver reports; ``gmail.readonly`` lets it read evidence the
# analyst forwards to the mailbox.
GMAIL_SCOPES = (
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
)

# Simple, deliberately permissive address check - the SMTP server is the
# real authority; this only stops obviously broken input reaching Google.
EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

MAX_RESULTS_LIMIT = 100


class GmailIntegration(GoogleApiIntegration):
    """Gmail client: evidence inbox + report delivery."""

    id = "gmail"
    name = "Gmail"
    purpose = "Evidence inbox & report delivery"

    api_label = "Gmail"
    scopes = GMAIL_SCOPES
    credentials_setting = "GOOGLE_GMAIL_CREDENTIALS_FILE"

    required_settings = ("GOOGLE_GMAIL_CREDENTIALS_FILE",)
    optional_settings = ()

    setup_instructions = (
        "Create a Google Cloud project, enable the Gmail API and create "
        "an OAuth client, then run the OAuth consent flow for the "
        "monitored mailbox so you obtain a credentials JSON containing a "
        "refresh_token. Set GOOGLE_GMAIL_CREDENTIALS_FILE (or the shared "
        "GOOGLE_CREDENTIALS_FILE) to that path in the server-side .env, "
        "restart the backend, then press Connect. Service accounts need "
        "Google Workspace domain-wide delegation to use Gmail."
    )

    # ----------------------------------------------
    # Lifecycle
    # ----------------------------------------------

    def verify_connection(self) -> Dict[str, Any]:
        """
        Prove the credentials work with a real authorized Gmail call.

        Returns
        -------
        dict
            ``{"email", "messages_total", "threads_total"}`` - the
            mailbox identity, never a token.
        """

        profile = self._api_request("GET", f"{GMAIL_API_BASE}/profile")

        if not isinstance(profile, dict) or not profile.get("emailAddress"):
            raise GoogleAPIError(
                "Gmail returned an unexpected profile response "
                "(missing emailAddress)."
            )

        return {
            "email": profile.get("emailAddress"),
            "messages_total": profile.get("messagesTotal"),
            "threads_total": profile.get("threadsTotal"),
        }

    # ----------------------------------------------
    # Gmail operations
    # ----------------------------------------------

    def list_messages(
        self,
        query: Optional[str] = None,
        max_results: int = 10,
    ) -> List[Dict[str, Any]]:
        """
        List message ids matching an optional Gmail search query.

        Parameters
        ----------
        query : str | None
            Standard Gmail search syntax, e.g. ``"is:unread subject:refund"``.
        max_results : int
            Number of ids to return (1-100).

        Returns
        -------
        list[dict]
            ``[{"id", "thread_id"}, ...]`` - metadata only; use
            ``get_message()`` for the body.
        """

        if isinstance(max_results, bool) or not isinstance(max_results, int):
            raise ValueError("max_results must be an integer.")
        if not 1 <= max_results <= MAX_RESULTS_LIMIT:
            raise ValueError(
                f"max_results must be between 1 and {MAX_RESULTS_LIMIT}."
            )
        if query is not None and not isinstance(query, str):
            raise ValueError("query must be a string or None.")

        params: Dict[str, Any] = {"maxResults": max_results}
        if query:
            params["q"] = query

        payload = self._api_request(
            "GET", f"{GMAIL_API_BASE}/messages", params=params
        )

        messages = payload.get("messages") if isinstance(payload, dict) else None
        messages = messages if isinstance(messages, list) else []

        return [
            {
                "id": item.get("id"),
                "thread_id": item.get("threadId"),
            }
            for item in messages
            if isinstance(item, dict) and item.get("id")
        ]

    def get_message(self, message_id: str, fmt: str = "full") -> Dict[str, Any]:
        """Fetch one message (``fmt``: ``full`` | ``metadata`` | ``minimal``)."""

        if not isinstance(message_id, str) or not message_id.strip():
            raise ValueError("message_id must be a non-empty string.")

        if fmt not in ("full", "metadata", "minimal", "raw"):
            raise ValueError("fmt must be full, metadata, minimal or raw.")

        payload = self._api_request(
            "GET",
            f"{GMAIL_API_BASE}/messages/{message_id}",
            params={"format": fmt},
        )

        if not isinstance(payload, dict) or not payload.get("id"):
            raise GoogleAPIError(
                "Gmail returned an unexpected message response."
            )

        return payload

    def send_email(
        self,
        to: str,
        subject: str,
        body: str,
    ) -> Dict[str, Any]:
        """
        Send one plain-text e-mail from the connected mailbox.

        Parameters
        ----------
        to : str
            Recipient address.
        subject : str
            Non-empty subject line.
        body : str
            Plain-text body (markdown is acceptable - mail clients show
            it as text).

        Returns
        -------
        dict
            ``{"id", "thread_id", "to"}`` of the sent message.
        """

        if not isinstance(to, str) or not EMAIL_PATTERN.match(to.strip()):
            raise ValueError("to must be a valid e-mail address.")
        if not isinstance(subject, str) or not subject.strip():
            raise ValueError("subject must be a non-empty string.")
        if not isinstance(body, str) or not body.strip():
            raise ValueError("body must be a non-empty string.")

        message = EmailMessage()
        message["To"] = to.strip()
        message["Subject"] = subject.strip()
        message.set_content(body)

        raw = base64.urlsafe_b64encode(
            message.as_bytes()
        ).decode("ascii")

        payload = self._api_request(
            "POST",
            f"{GMAIL_API_BASE}/messages/send",
            json_body={"raw": raw},
            acceptable=(200,),
        )

        if not isinstance(payload, dict) or not payload.get("id"):
            raise GoogleAPIError(
                "Gmail returned an unexpected send response (missing id)."
            )

        return {
            "id": payload.get("id"),
            "thread_id": payload.get("threadId"),
            "to": to.strip(),
        }
