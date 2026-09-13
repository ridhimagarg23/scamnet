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
import logging
import os
import threading
from pathlib import Path

logger = logging.getLogger("SCAMNET-MemoryManager")

# One lock for the whole archive, shared by every instance.
# MemoryManager objects are created per request AND used from the
# Telegram worker thread, so an instance-level lock would not protect
# anything: concurrent read-modify-write cycles used to interleave and
# silently drop a record (or corrupt the JSON file).
_FILE_LOCK = threading.RLock()


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

        Thread-safe (a single module-level lock guards every instance)
        and crash-safe: the new content is written to a temporary file
        and atomically moved into place, so a reader can never observe
        a half-written archive.

        Parameters
        ----------
        investigation : dict
            A serialised InvestigationResult (``model_dump()`` output).
        """

        with _FILE_LOCK:

            memory = self.load()

            memory.append(
                investigation
            )

            # Pretty-print so analysts can diff the archive in git.
            payload = json.dumps(
                memory,
                indent=4
            )

            temporary = self.memory_file.with_suffix(".json.tmp")

            temporary.write_text(
                payload,
                encoding="utf-8"
            )

            # Atomic replace: readers see either the old or the new
            # archive, never a partial write (the API and the Telegram
            # worker archive from different threads).
            os.replace(temporary, self.memory_file)

    def load(self) -> list:
        """
        Read the full archive (empty list when nothing stored yet).

        A corrupt archive (previous crash, manual edit, disk full)
        never breaks an investigation: the damaged file is preserved
        next to the archive as ``threat_memory.corrupt-<n>.json`` and an
        empty list is returned so the caller can keep working.
        """

        with _FILE_LOCK:

            try:
                return json.loads(
                    self.memory_file.read_text(encoding="utf-8")
                )

            except FileNotFoundError:
                return []

            except (json.JSONDecodeError, UnicodeDecodeError) as exc:

                backup = self._preserve_corrupt_archive()

                logger.error(
                    "Threat memory archive is not valid JSON (%s). "
                    "Preserved as %s and starting a fresh archive.",
                    exc, backup,
                )

                return []

    def _preserve_corrupt_archive(self) -> Path:
        """Move a damaged archive aside so no data is silently lost."""

        index = 0

        while True:
            backup = self.memory_file.with_name(
                f"threat_memory.corrupt-{index}.json"
            )
            if not backup.exists():
                break
            index += 1

        try:
            os.replace(self.memory_file, backup)
        except OSError:  # pragma: no cover - defensive
            return self.memory_file

        return backup

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

        with _FILE_LOCK:

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
