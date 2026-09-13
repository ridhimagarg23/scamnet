"""
test_memory_manager.py
======================
Offline unit tests for the JSON persistence layer.

The test redirects MemoryManager to a temp file so the repository's
real ``database/threat_memory.json`` is never touched.

Run with:

    OPENROUTER_API_KEY=test-key python -m unittest discover -s tests
"""

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from tools.memory_manager import MemoryManager


class TestMemoryManager(unittest.TestCase):

    def setUp(self):
        # Every test gets its own throwaway file.
        self.temp_dir = TemporaryDirectory()
        self.temp_file = Path(self.temp_dir.name) / "test_memory.json"

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_save_load_search_clear(self):
        """
        save -> load round-trip, threat-type search filtering and
        clear() must all behave on a fresh temp archive.
        """
        # Instantiate and override target path
        manager = MemoryManager()
        manager.memory_file = self.temp_file

        # Re-run initialization to ensure test file exists
        manager.clear()
        self.assertEqual(manager.load(), [])

        # Test save & load
        test_case = {"threat_type": "Banking Phishing", "risk_score": 92}
        manager.save(test_case)

        loaded = manager.load()
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0]["threat_type"], "Banking Phishing")
        self.assertEqual(loaded[0]["risk_score"], 92)

        # Test search (matching vs non-matching threat types)
        search_results = manager.search("Banking Phishing")
        self.assertEqual(len(search_results), 1)

        empty_search = manager.search("Job Scam")
        self.assertEqual(len(empty_search), 0)

        # Test clear
        manager.clear()
        self.assertEqual(manager.load(), [])
