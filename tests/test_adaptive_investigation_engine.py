"""
test_adaptive_investigation_engine.py
=====================================
Offline unit tests for the deterministic Adaptive Investigation
Engine (persona profile selection + objective-ladder progression).

No mocks and no network: the engine is pure rule-based logic.

Run with:

    OPENROUTER_API_KEY=test-key python -m unittest discover -s tests
"""

import unittest
from tools.adaptive_investigation_engine import AdaptiveInvestigationEngine, InvestigationState


class TestAdaptiveInvestigationEngine(unittest.TestCase):

    def test_engine_flow(self):
        """
        initialize() must produce a stateful engine whose update()
        advances the turn counter and refreshes objective/strategy.
        """
        engine = AdaptiveInvestigationEngine()

        # Test initialization
        state = engine.initialize("Banking Phishing")
        self.assertIsInstance(state, InvestigationState)
        self.assertEqual(state.turn_number, 1)
        self.assertIsNotNone(state.current_objective)
        self.assertIsNotNone(state.current_strategy)
        self.assertIsNotNone(state.profile)

        # Test state transition
        updated_state = engine.update(objective_completed=True)
        self.assertEqual(updated_state.turn_number, 2)
        self.assertIsNotNone(updated_state.current_objective)
        self.assertIsNotNone(updated_state.current_strategy)
