"""
integrations
============
SCAMNET external-app integration layer.

One shared client instance per provider is registered here at import
time, bound to the server-side ``config.settings`` singleton (the same
configuration pattern the rest of the project uses). The API
(``backend/api.py``) and any future orchestrator talk to integrations
ONLY through this registry - never by instantiating clients ad hoc.

Registered providers
--------------------
* ``telegram``      - communication & intelligence gathering
* ``google_sheets`` - live investigation evidence
* ``google_drive``  - investigation reports

Honest-status guarantee
-----------------------
Every status produced by this layer reflects the REAL server-side
configuration and authentication state (see base.py). No integration
reports ``connected=True`` unless a genuine handshake succeeded, and
no secret value ever appears in a status payload.
"""

from typing import Dict, Optional

from config import settings

from .base import (
    BaseIntegration,
    IntegrationNotConfiguredError,
    IntegrationNotImplementedError,
    IntegrationState,
    IntegrationStatus,
)
from .telegram import TelegramIntegration
from .google_sheets import GoogleSheetsIntegration
from .google_drive import GoogleDriveIntegration


# --------------------------------------------------
# Registry (single shared instance per provider)
# --------------------------------------------------

REGISTRY: Dict[str, BaseIntegration] = {}


def _register(integration: BaseIntegration) -> BaseIntegration:
    """Add one client instance to the registry (id must be unique)."""

    if integration.id in REGISTRY:
        raise ValueError(
            f"Duplicate integration id: '{integration.id}'"
        )
    REGISTRY[integration.id] = integration
    return integration


_register(TelegramIntegration(settings))
_register(GoogleSheetsIntegration(settings))
_register(GoogleDriveIntegration(settings))


# --------------------------------------------------
# Registry access helpers (used by backend/api.py)
# --------------------------------------------------

def get_integration(integration_id: str) -> Optional[BaseIntegration]:
    """Return the registered client for ``integration_id`` or None."""

    return REGISTRY.get(integration_id)


def get_all_integration_statuses() -> Dict[str, dict]:
    """
    Honest status snapshot of every registered integration, keyed by
    id - the exact payload served by ``GET /api/integrations``.

    Values are JSON-safe dicts (``model_dump(mode="json")``) and are
    guaranteed secret-free by ``IntegrationStatus`` (see base.py).
    """

    return {
        integration_id: integration.get_status().model_dump(mode="json")
        for integration_id, integration in REGISTRY.items()
    }


__all__ = [
    "REGISTRY",
    "BaseIntegration",
    "IntegrationState",
    "IntegrationStatus",
    "IntegrationNotConfiguredError",
    "IntegrationNotImplementedError",
    "TelegramIntegration",
    "GoogleSheetsIntegration",
    "GoogleDriveIntegration",
    "get_integration",
    "get_all_integration_statuses",
]
