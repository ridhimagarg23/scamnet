"""
memory_manager.py
=================
JSON-file persistence for finished scam investigations.

Every /analyze turn appends the full ``InvestigationResult`` of the
current case to ``database/threat_memory.json`` (git-ignored). This
builds a small, human-readable threat-intel archive that can be
searched later by threat type.

Why a JSON file instead of a database?
* Zero external infrastructure - works on free-tier hosts (Render /
  Railway) that only offer an ephemeral or read-only filesystem.
* Easy to inspect, grep and export for analysts.

NOTE: on ephemeral platforms the file resets on redeploy - treat it
as per-run memory, not a durable long-term store.
"""

import json
from pathlib import Path


class MemoryManager:

    def __init__(self):
        """
        Point at the archive file (creating it when missing).

        The file lives at ``<repo-root>/database/threat_memory.json``.
        Path is resolved from this module's location (``tools/`` is
        one directory below the repository root) so the manager keeps
        working regardless of the current working directory.
        """

        # Resolve relative to the repository root (tools is one level deep)
        project_root = Path(__file__).resolve().parent.parent
        self.memory_file = project_root / "database" / "threat_memory.json"

        # Create the database directory automatically if it doesn't exist
        self.memory_file.parent.mkdir(parents=True, exist_ok=True)

        if not self.memory_file.exists():

            # Seed with an empty JSON array.
            self.memory_file.write_text(
                "[]",
                encoding="utf-8"
            )

    def save(
        self,
        investigation: dict
    ):
        """
        Append one investigation record to the memory archive.

        Parameters
        ----------
        investigation : dict
            A serialised InvestigationResult (``model_dump()`` output).
        """

        memory = self.load()

        memory.append(
            investigation
        )

        # Pretty-print so analysts can diff the archive in git.
        self.memory_file.write_text(

            json.dumps(
                memory,
                indent=4
            ),

            encoding="utf-8"

        )

    def load(self) -> list:
        """Read the full archive (empty list when nothing stored yet)."""

        return json.loads(

            self.memory_file.read_text(
                encoding="utf-8"
            )

        )

    def search(
        self,
        threat_type: str
    ) -> list:
        """
        Return every archived record matching a threat type.

        Parameters
        ----------
        threat_type : str
            Exact threat family label, e.g. ``"Banking Phishing"``.
        """

        memory = self.load()

        return [

            item

            for item in memory

            if item.get(
                "threat_type"
            ) == threat_type

        ]

    def clear(self):
        """Wipe the archive (used by tests / admin operations)."""

        self.memory_file.write_text(

            "[]",

            encoding="utf-8"

        )
