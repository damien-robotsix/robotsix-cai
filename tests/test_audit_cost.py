"""Tests for cai_lib/audit/cost.py — _load_cost_log."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cai_lib.audit.cost as _cost_module  # noqa: E402
from cai_lib.audit.cost import _load_cost_log  # noqa: E402


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
        "category": "cai-refine",
        "cost_usd": 0.02,
        "agent": "cai-refine",
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
            self.assertIn("cai-refine", categories)

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


class TestBuildCostSummaryFsmState(unittest.TestCase):
    """Issue #1203: ``_build_cost_summary`` §4 phase breakdown aggregates
    rows by the optional ``fsm_state`` field and handles rows missing the
    field by bucketing them under ``(none)``."""

    _RECENT_TS_A = "2099-01-01T00:00:00Z"
    _RECENT_TS_B = "2099-01-02T00:00:00Z"
    _RECENT_TS_C = "2099-01-03T00:00:00Z"

    def _write_rows(self, path: Path, rows: list[dict]) -> None:
        path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")

    def test_summary_groups_by_fsm_state_with_none_bucket(self):
        from cai_lib.audit.cost import _build_cost_summary

        with tempfile.TemporaryDirectory() as tmp:
            local = Path(tmp) / "cai-cost.jsonl"
            self._write_rows(local, [
                {"ts": self._RECENT_TS_A, "category": "refine",
                 "cost_usd": 0.10, "fsm_state": "REFINING"},
                {"ts": self._RECENT_TS_B, "category": "plan.plan",
                 "cost_usd": 0.20, "fsm_state": "PLANNING"},
                {"ts": self._RECENT_TS_C, "category": "rescue",
                 "cost_usd": 0.05},  # no fsm_state — non-FSM call site
            ])
            missing_agg = Path(tmp) / "nope"
            with (
                mock.patch.object(_cost_module, "COST_LOG_PATH", local),
                mock.patch.object(_cost_module, "COST_LOG_AGGREGATE_DIR", missing_agg),
            ):
                summary = _build_cost_summary(days=3650, top_n=3)

        # §4 Phase breakdown replaces the old "### By FSM state" section.
        self.assertIn("§4 Phase breakdown", summary)
        self.assertIn("| fsm_state |", summary)
        # The three distinct buckets must all appear in the §4 table.
        self.assertIn("| REFINING | 1 |", summary)
        self.assertIn("| PLANNING | 1 |", summary)
        self.assertIn("| (none) | 1 |", summary)

    def test_summary_empty_when_no_rows(self):
        """Back-compat: no rows → empty string (no sections emitted)."""
        from cai_lib.audit.cost import _build_cost_summary

        with tempfile.TemporaryDirectory() as tmp:
            missing_local = Path(tmp) / "missing.jsonl"
            missing_agg = Path(tmp) / "missing-agg"
            with (
                mock.patch.object(_cost_module, "COST_LOG_PATH", missing_local),
                mock.patch.object(_cost_module, "COST_LOG_AGGREGATE_DIR", missing_agg),
            ):
                summary = _build_cost_summary(days=7, top_n=3)

        self.assertEqual(summary, "")


class TestBuildCostSummaryAllSections(unittest.TestCase):
    """Verify all 7 §-sections of the rewritten _build_cost_summary.

    Uses cluster_n=2 so §2 and §6 trigger with small fixtures.
    """

    _TS = [
        "2099-01-01T00:00:00Z",
        "2099-01-02T00:00:00Z",
        "2099-01-03T00:00:00Z",
        "2099-01-04T00:00:00Z",
        "2099-01-05T00:00:00Z",
    ]

    def _write_rows(self, path: Path, rows: list[dict]) -> None:
        path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")

    def _base_row(self, i: int, **extra) -> dict:
        r = {
            "ts": self._TS[i % len(self._TS)],
            "category": "implement",
            "agent": "cai-implement",
            "cost_usd": 0.10 * (i + 1),
            "host": "host-a",
        }
        r.update(extra)
        return r

    def test_section1_headline(self):
        """§1 headline shows grand total and invocation count."""
        from cai_lib.audit.cost import _build_cost_summary

        with tempfile.TemporaryDirectory() as tmp:
            local = Path(tmp) / "cai-cost.jsonl"
            self._write_rows(local, [self._base_row(0), self._base_row(1)])
            missing_agg = Path(tmp) / "nope"
            with (
                mock.patch.object(_cost_module, "COST_LOG_PATH", local),
                mock.patch.object(_cost_module, "COST_LOG_AGGREGATE_DIR", missing_agg),
            ):
                summary = _build_cost_summary(days=3650, top_n=5, cluster_n=2)

        self.assertIn("## Cost summary (last 3650d,", summary)
        self.assertIn("2 invocations", summary)

    def test_section2_delta_by_agent(self):
        """§2 shows agent delta when agent has >= 2*cluster_n rows."""
        from cai_lib.audit.cost import _build_cost_summary

        # Need >= 4 rows (cluster_n=2 → skip < 4)
        rows = [self._base_row(i, ts=self._TS[i % len(self._TS)]) for i in range(4)]
        with tempfile.TemporaryDirectory() as tmp:
            local = Path(tmp) / "cai-cost.jsonl"
            self._write_rows(local, rows)
            missing_agg = Path(tmp) / "nope"
            with (
                mock.patch.object(_cost_module, "COST_LOG_PATH", local),
                mock.patch.object(_cost_module, "COST_LOG_AGGREGATE_DIR", missing_agg),
            ):
                summary = _build_cost_summary(days=3650, top_n=5, cluster_n=2)

        self.assertIn("§2 Recent vs prior", summary)
        self.assertIn("cai-implement", summary)

    def test_section2_skipped_when_not_enough_rows(self):
        """§2 is omitted when no agent has >= 2*cluster_n rows."""
        from cai_lib.audit.cost import _build_cost_summary

        with tempfile.TemporaryDirectory() as tmp:
            local = Path(tmp) / "cai-cost.jsonl"
            self._write_rows(local, [self._base_row(0)])  # only 1 row
            missing_agg = Path(tmp) / "nope"
            with (
                mock.patch.object(_cost_module, "COST_LOG_PATH", local),
                mock.patch.object(_cost_module, "COST_LOG_AGGREGATE_DIR", missing_agg),
            ):
                summary = _build_cost_summary(days=3650, top_n=5, cluster_n=2)

        self.assertNotIn("§2", summary)

    def test_section3_expensive_targets(self):
        """§3 shows top targets grouped by target_number."""
        from cai_lib.audit.cost import _build_cost_summary

        rows = [
            self._base_row(0, target_number=42, cost_usd=0.50),
            self._base_row(1, target_number=99, cost_usd=0.10),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            local = Path(tmp) / "cai-cost.jsonl"
            self._write_rows(local, rows)
            missing_agg = Path(tmp) / "nope"
            with (
                mock.patch.object(_cost_module, "COST_LOG_PATH", local),
                mock.patch.object(_cost_module, "COST_LOG_AGGREGATE_DIR", missing_agg),
            ):
                summary = _build_cost_summary(days=3650, top_n=5, cluster_n=2)

        self.assertIn("§3 Top-", summary)
        self.assertIn("| #42 |", summary)
        self.assertIn("| #99 |", summary)

    def test_section3_outcome_join(self):
        """§3 joins outcome log to show outcome and fix_attempt_count."""
        from cai_lib.audit.cost import _build_cost_summary

        cost_rows = [self._base_row(0, target_number=42, cost_usd=1.00)]
        outcome_row = json.dumps({
            "ts": "2099-01-01T00:00:00Z",
            "issue_number": 42,
            "outcome": "solved",
            "fix_attempt_count": 3,
        })
        with tempfile.TemporaryDirectory() as tmp:
            local = Path(tmp) / "cai-cost.jsonl"
            outcome = Path(tmp) / "cai-outcome.jsonl"
            self._write_rows(local, cost_rows)
            outcome.write_text(outcome_row + "\n")
            missing_agg = Path(tmp) / "nope"
            with (
                mock.patch.object(_cost_module, "COST_LOG_PATH", local),
                mock.patch.object(_cost_module, "COST_LOG_AGGREGATE_DIR", missing_agg),
                mock.patch.object(_cost_module, "OUTCOME_LOG_PATH", outcome),
            ):
                summary = _build_cost_summary(days=3650, top_n=5, cluster_n=2)

        self.assertIn("| #42 |", summary)
        self.assertIn("solved", summary)
        self.assertIn("3", summary)

    def test_section3_skipped_when_no_target_numbers(self):
        """§3 is omitted when no rows have target_number."""
        from cai_lib.audit.cost import _build_cost_summary

        with tempfile.TemporaryDirectory() as tmp:
            local = Path(tmp) / "cai-cost.jsonl"
            self._write_rows(local, [self._base_row(0)])  # no target_number
            missing_agg = Path(tmp) / "nope"
            with (
                mock.patch.object(_cost_module, "COST_LOG_PATH", local),
                mock.patch.object(_cost_module, "COST_LOG_AGGREGATE_DIR", missing_agg),
            ):
                summary = _build_cost_summary(days=3650, top_n=5, cluster_n=2)

        self.assertNotIn("§3", summary)

    def test_section4_phase_breakdown_always_present(self):
        """§4 is always shown, even with a single row."""
        from cai_lib.audit.cost import _build_cost_summary

        with tempfile.TemporaryDirectory() as tmp:
            local = Path(tmp) / "cai-cost.jsonl"
            self._write_rows(local, [self._base_row(0, fsm_state="IN_PROGRESS")])
            missing_agg = Path(tmp) / "nope"
            with (
                mock.patch.object(_cost_module, "COST_LOG_PATH", local),
                mock.patch.object(_cost_module, "COST_LOG_AGGREGATE_DIR", missing_agg),
            ):
                summary = _build_cost_summary(days=3650, top_n=5, cluster_n=2)

        self.assertIn("§4 Phase breakdown", summary)
        self.assertIn("| IN_PROGRESS |", summary)

    def test_section4_retry_split(self):
        """§4 correctly splits first-attempt vs retry via outcome join."""
        from cai_lib.audit.cost import _build_cost_summary

        cost_rows = [
            self._base_row(0, target_number=10, fsm_state="IN_PROGRESS", cost_usd=0.20),
        ]
        # fix_attempt_count=3 → retry
        outcome_row = json.dumps({
            "ts": "2099-01-01T00:00:00Z",
            "issue_number": 10,
            "outcome": "solved",
            "fix_attempt_count": 3,
        })
        with tempfile.TemporaryDirectory() as tmp:
            local = Path(tmp) / "cai-cost.jsonl"
            outcome = Path(tmp) / "cai-outcome.jsonl"
            self._write_rows(local, cost_rows)
            outcome.write_text(outcome_row + "\n")
            missing_agg = Path(tmp) / "nope"
            with (
                mock.patch.object(_cost_module, "COST_LOG_PATH", local),
                mock.patch.object(_cost_module, "COST_LOG_AGGREGATE_DIR", missing_agg),
                mock.patch.object(_cost_module, "OUTCOME_LOG_PATH", outcome),
            ):
                summary = _build_cost_summary(days=3650, top_n=5, cluster_n=2)

        # The IN_PROGRESS row should be in the retry column (0 first, 1 retry)
        self.assertIn("| IN_PROGRESS | 0 |", summary)

    def test_section5_module_field(self):
        """§5 groups by module field when present."""
        from cai_lib.audit.cost import _build_cost_summary

        rows = [
            self._base_row(0, module="fsm", cost_usd=0.30),
            self._base_row(1, module="fsm", cost_usd=0.10),
            self._base_row(2, module="audit", cost_usd=0.50),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            local = Path(tmp) / "cai-cost.jsonl"
            self._write_rows(local, rows)
            missing_agg = Path(tmp) / "nope"
            with (
                mock.patch.object(_cost_module, "COST_LOG_PATH", local),
                mock.patch.object(_cost_module, "COST_LOG_AGGREGATE_DIR", missing_agg),
            ):
                summary = _build_cost_summary(days=3650, top_n=5, cluster_n=2)

        self.assertIn("§5 Per-module", summary)
        self.assertIn("| fsm |", summary)
        self.assertIn("| audit |", summary)
        # audit > fsm by total cost, so audit should appear first
        self.assertLess(summary.index("| audit |"), summary.index("| fsm |"))

    def test_section5_skipped_when_no_module_data(self):
        """§5 is omitted when no rows have module or scope_files."""
        from cai_lib.audit.cost import _build_cost_summary

        with tempfile.TemporaryDirectory() as tmp:
            local = Path(tmp) / "cai-cost.jsonl"
            self._write_rows(local, [self._base_row(0)])  # no module
            missing_agg = Path(tmp) / "nope"
            with (
                mock.patch.object(_cost_module, "COST_LOG_PATH", local),
                mock.patch.object(_cost_module, "COST_LOG_AGGREGATE_DIR", missing_agg),
            ):
                summary = _build_cost_summary(days=3650, top_n=5, cluster_n=2)

        self.assertNotIn("§5", summary)

    def test_section6_cache_regression(self):
        """§6 flags a ≥10pp cache-hit-rate drop for the same fingerprint."""
        from cai_lib.audit.cost import _build_cost_summary

        # cluster_n=2: need >= 4 rows per (agent, fingerprint)
        # prior 2: high cache hit; recent 2: low cache hit
        rows = [
            self._base_row(0, prompt_fingerprint="abc123", cache_hit_rate=0.80),
            self._base_row(1, prompt_fingerprint="abc123", cache_hit_rate=0.80),
            self._base_row(2, prompt_fingerprint="abc123", cache_hit_rate=0.50),
            self._base_row(3, prompt_fingerprint="abc123", cache_hit_rate=0.50),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            local = Path(tmp) / "cai-cost.jsonl"
            self._write_rows(local, rows)
            missing_agg = Path(tmp) / "nope"
            with (
                mock.patch.object(_cost_module, "COST_LOG_PATH", local),
                mock.patch.object(_cost_module, "COST_LOG_AGGREGATE_DIR", missing_agg),
            ):
                summary = _build_cost_summary(days=3650, top_n=5, cluster_n=2)

        self.assertIn("§6 Cache-health regressions", summary)
        self.assertIn("abc123", summary)
        self.assertIn("⚠️", summary)

    def test_section6_skipped_when_no_regression(self):
        """§6 is omitted when cache hit rate is stable (< 10pp drop)."""
        from cai_lib.audit.cost import _build_cost_summary

        rows = [
            self._base_row(0, prompt_fingerprint="fp1", cache_hit_rate=0.80),
            self._base_row(1, prompt_fingerprint="fp1", cache_hit_rate=0.80),
            self._base_row(2, prompt_fingerprint="fp1", cache_hit_rate=0.79),
            self._base_row(3, prompt_fingerprint="fp1", cache_hit_rate=0.79),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            local = Path(tmp) / "cai-cost.jsonl"
            self._write_rows(local, rows)
            missing_agg = Path(tmp) / "nope"
            with (
                mock.patch.object(_cost_module, "COST_LOG_PATH", local),
                mock.patch.object(_cost_module, "COST_LOG_AGGREGATE_DIR", missing_agg),
            ):
                summary = _build_cost_summary(days=3650, top_n=5, cluster_n=2)

        self.assertNotIn("§6", summary)

    def test_section7_host_anomalies(self):
        """§7 flags hosts with mean $/call ≥ 2× median.

        Use 3 hosts so the odd-index median is the middle value and the
        expensive host clearly exceeds 2× it.
        """
        from cai_lib.audit.cost import _build_cost_summary

        # host-a and host-b have mean 0.10; host-c has mean 1.00.
        # sorted_means=[0.10, 0.10, 1.00], median=0.10; host-c: 1.00 >= 2*0.10 ✓
        rows = [
            self._base_row(0, host="host-a", cost_usd=0.10),
            self._base_row(1, host="host-b", cost_usd=0.10),
            self._base_row(2, host="host-c", cost_usd=1.00),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            local = Path(tmp) / "cai-cost.jsonl"
            self._write_rows(local, rows)
            missing_agg = Path(tmp) / "nope"
            with (
                mock.patch.object(_cost_module, "COST_LOG_PATH", local),
                mock.patch.object(_cost_module, "COST_LOG_AGGREGATE_DIR", missing_agg),
            ):
                summary = _build_cost_summary(days=3650, top_n=5, cluster_n=2)

        self.assertIn("§7 Host anomalies", summary)
        self.assertIn("| host-a |", summary)
        self.assertIn("| host-c |", summary)
        # host-c has mean 1.00 vs median 0.10; 1.00 >= 2*0.10 so flagged
        idx_c = summary.index("| host-c |")
        self.assertIn("⚠️", summary[idx_c:idx_c + 80])

    def test_scope_files_field_in_row(self):
        """Rows can carry scope_files and prompt_fingerprint fields."""
        from cai_lib.audit.cost import _build_cost_summary

        rows = [
            self._base_row(
                0,
                scope_files=["cai_lib/audit/cost.py", "tests/test_audit_cost.py"],
                prompt_fingerprint="deadbeef",
            )
        ]
        with tempfile.TemporaryDirectory() as tmp:
            local = Path(tmp) / "cai-cost.jsonl"
            self._write_rows(local, rows)
            missing_agg = Path(tmp) / "nope"
            with (
                mock.patch.object(_cost_module, "COST_LOG_PATH", local),
                mock.patch.object(_cost_module, "COST_LOG_AGGREGATE_DIR", missing_agg),
            ):
                # Confirm the summary is non-empty (rows present)
                summary = _build_cost_summary(days=3650, top_n=5, cluster_n=2)

        self.assertNotEqual(summary, "")


if __name__ == "__main__":
    unittest.main()
