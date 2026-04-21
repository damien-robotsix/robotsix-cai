"""Tests for cai_lib/audit/cost.py — _primary_model function."""
import unittest

from cai_lib.audit.cost import _primary_model


class TestPrimaryModel(unittest.TestCase):
    def test_empty_row(self):
        self.assertEqual(_primary_model({}), "")

    def test_no_models_key(self):
        self.assertEqual(_primary_model({"cost": 1.0}), "")

    def test_models_not_dict(self):
        self.assertEqual(_primary_model({"models": []}), "")

    def test_single_model(self):
        row = {"models": {"claude-sonnet-4-6": {"outputTokens": 500, "inputTokens": 100}}}
        self.assertEqual(_primary_model(row), "claude-sonnet-4-6")

    def test_picks_highest_output_tokens(self):
        """Haiku has small outputTokens (SDK overhead); Sonnet has large (agent work)."""
        row = {
            "models": {
                "claude-haiku-4-5-20251001": {"inputTokens": 39463, "outputTokens": 20},
                "claude-sonnet-4-6": {"inputTokens": 60, "outputTokens": 33600},
            }
        }
        self.assertEqual(_primary_model(row), "claude-sonnet-4-6")

    def test_haiku_wins_when_it_has_more_tokens(self):
        """If Haiku genuinely produced more output tokens, return it."""
        row = {
            "models": {
                "claude-haiku-4-5-20251001": {"inputTokens": 1000, "outputTokens": 5000},
                "claude-sonnet-4-6": {"inputTokens": 60, "outputTokens": 100},
            }
        }
        self.assertEqual(_primary_model(row), "claude-haiku-4-5-20251001")

    def test_missing_output_tokens_defaults_to_zero(self):
        """Models missing outputTokens key default to 0."""
        row = {
            "models": {
                "model-a": {"inputTokens": 100},
                "model-b": {"outputTokens": 10},
            }
        }
        self.assertEqual(_primary_model(row), "model-b")


if __name__ == "__main__":
    unittest.main()
