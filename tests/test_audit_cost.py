"""Tests for cai_lib/audit/cost.py — _primary_model and _load_cost_log."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cai_lib.audit.cost as _cost_module  # noqa: E402
from cai_lib.audit.cost import _load_cost_log, _primary_model  # noqa: E402


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


class TestLoadCostLogAggregation(unittest.TestCase):
    """Tests for _load_cost_log multi-host aggregation behaviour."""

    _RECENT_ROW_A = json.dumps({
        "ts": "2099-01-01T00:00:00Z",
        "category": "cai-implement",
        "cost_usd": 0.01,
        "agent": "cai-implement",
    })
    _RECENT_ROW_B = json.dumps({
        "ts": "2099-01-02T00:00:00Z",
        "category": "cai-audit",
        "cost_usd": 0.02,
        "agent": "cai-audit",
    })

    def test_falls_back_to_local_when_aggregate_missing(self):
        """Without aggregate dir, reads COST_LOG_PATH."""
        with tempfile.TemporaryDirectory() as tmp:
            local = Path(tmp) / "cai-cost.jsonl"
            local.write_text(self._RECENT_ROW_A + "\n")
            missing_agg = Path(tmp) / "nonexistent-aggregate"
            with (
                mock.patch.object(_cost_module, "COST_LOG_PATH", local),
                mock.patch.object(_cost_module, "COST_LOG_AGGREGATE_DIR", missing_agg),
            ):
                rows = _load_cost_log(days=3650)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["category"], "cai-implement")

    def test_falls_back_to_local_when_aggregate_empty(self):
        """With empty aggregate dir, falls back to COST_LOG_PATH."""
        with tempfile.TemporaryDirectory() as tmp:
            local = Path(tmp) / "cai-cost.jsonl"
            local.write_text(self._RECENT_ROW_A + "\n")
            agg = Path(tmp) / "aggregate"
            agg.mkdir()  # exists but empty
            with (
                mock.patch.object(_cost_module, "COST_LOG_PATH", local),
                mock.patch.object(_cost_module, "COST_LOG_AGGREGATE_DIR", agg),
            ):
                rows = _load_cost_log(days=3650)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["category"], "cai-implement")

    def test_uses_aggregate_when_populated(self):
        """With populated aggregate dir, reads from all machine subdirs."""
        with tempfile.TemporaryDirectory() as tmp:
            agg = Path(tmp) / "aggregate"
            machine_a = agg / "machine-a"
            machine_b = agg / "machine-b"
            machine_a.mkdir(parents=True)
            machine_b.mkdir(parents=True)
            (machine_a / "cai-cost.jsonl").write_text(self._RECENT_ROW_A + "\n")
            (machine_b / "cai-cost.jsonl").write_text(self._RECENT_ROW_B + "\n")
            local = Path(tmp) / "cai-cost.jsonl"  # not created — should not be used
            with (
                mock.patch.object(_cost_module, "COST_LOG_PATH", local),
                mock.patch.object(_cost_module, "COST_LOG_AGGREGATE_DIR", agg),
            ):
                rows = _load_cost_log(days=3650)
            categories = {r["category"] for r in rows}
            self.assertEqual(len(rows), 2)
            self.assertIn("cai-implement", categories)
            self.assertIn("cai-audit", categories)

    def test_aggregate_excludes_old_rows(self):
        """Only rows within the `days` window are returned from aggregate."""
        with tempfile.TemporaryDirectory() as tmp:
            agg = Path(tmp) / "aggregate"
            machine_a = agg / "machine-a"
            machine_a.mkdir(parents=True)
            old_row = json.dumps({
                "ts": "2000-01-01T00:00:00Z",
                "category": "old",
                "cost_usd": 0.99,
            })
            (machine_a / "cai-cost.jsonl").write_text(
                self._RECENT_ROW_A + "\n" + old_row + "\n"
            )
            with (
                mock.patch.object(_cost_module, "COST_LOG_AGGREGATE_DIR", agg),
            ):
                rows = _load_cost_log(days=7)
            # The 2099 row should be included; the 2000 row excluded.
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["category"], "cai-implement")

    def test_returns_empty_when_no_local_and_no_aggregate(self):
        """Returns [] when neither COST_LOG_PATH nor aggregate dir exists."""
        with tempfile.TemporaryDirectory() as tmp:
            missing_local = Path(tmp) / "does-not-exist.jsonl"
            missing_agg = Path(tmp) / "does-not-exist-agg"
            with (
                mock.patch.object(_cost_module, "COST_LOG_PATH", missing_local),
                mock.patch.object(_cost_module, "COST_LOG_AGGREGATE_DIR", missing_agg),
            ):
                rows = _load_cost_log(days=7)
            self.assertEqual(rows, [])


if __name__ == "__main__":
    unittest.main()
