"""Tests for :mod:`cai_lib.cost_summary`.

Covers the close-time roll-up path (#1198):
  - ``_load_issue_cost_rows`` filters by ``target_number`` +
    ``target_kind`` and reads through ``_load_cost_log``.
  - ``_build_final_cost_summary`` emits a well-formed marker + body
    with per-agent / per-stage / cache / parent-model sections.
  - Graceful degradation when optional keys
    (``phase`` / ``cache_hit_rate`` / ``fix_attempt_count`` /
    ``parent_model``) are absent on the rows.
  - Empty-rows input returns ``("", "")`` and
    ``post_final_cost_summary`` then does NOT call
    ``_post_issue_comment``.
  - Best-effort contract: raising ``_post_issue_comment`` does not
    propagate out of ``post_final_cost_summary``.

All tests use ``unittest`` to match the project convention — pytest is
not installed.
"""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cai_lib.config import CAI_COST_COMMENT_RE
from cai_lib.cost_summary import (
    _build_final_cost_summary,
    _load_issue_cost_rows,
    _stage_key,
    post_final_cost_summary,
)


def _row(
    *,
    target_kind: str | None = "issue",
    target_number: int | None = 42,
    cost: float = 0.10,
    agent: str = "cai-plan",
    category: str = "plan.plan",
    phase: str | None = None,
    model: str = "claude-opus-4-7",
    input_tokens: int = 100,
    output_tokens: int = 50,
    cache_read: int = 400,
    cache_create: int = 20,
    turns: int = 3,
    duration_ms: int = 1234,
    cache_hit_rate: float | None = None,
) -> dict:
    row: dict = {
        "ts": "2026-04-23T12:00:00Z",
        "category": category,
        "agent": agent,
        "cost_usd": cost,
        "duration_ms": duration_ms,
        "num_turns": turns,
        "is_error": False,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_input_tokens": cache_read,
        "cache_creation_input_tokens": cache_create,
        "parent_model": model,
    }
    if target_kind is not None:
        row["target_kind"] = target_kind
    if target_number is not None:
        row["target_number"] = target_number
    if phase is not None:
        row["phase"] = phase
    if cache_hit_rate is not None:
        row["cache_hit_rate"] = cache_hit_rate
    return row


class TestLoadIssueCostRows(unittest.TestCase):
    def test_filters_by_target_kind_and_number(self):
        all_rows = [
            _row(target_kind="issue", target_number=42),
            _row(target_kind="issue", target_number=99, agent="cai-refine"),
            _row(target_kind="pr", target_number=100, agent="cai-review-pr"),
            _row(target_kind="pr", target_number=5, agent="cai-merge"),
            _row(target_kind=None, target_number=None, agent="cai-misc"),
        ]
        with patch("cai_lib.audit.cost._load_cost_log",
                   return_value=all_rows), \
             patch("cai_lib.transcript_sync.pull_cost", return_value=0):
            got = _load_issue_cost_rows(42, 100)
        agents = sorted(r["agent"] for r in got)
        self.assertEqual(agents, ["cai-plan", "cai-review-pr"])

    def test_ignores_rows_with_non_int_target_number(self):
        all_rows = [
            _row(target_kind="issue", target_number=42),
            {"target_kind": "issue", "target_number": "42",
             "agent": "cai-bad"},
        ]
        with patch("cai_lib.audit.cost._load_cost_log",
                   return_value=all_rows), \
             patch("cai_lib.transcript_sync.pull_cost", return_value=0):
            got = _load_issue_cost_rows(42, None)
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["agent"], "cai-plan")

    def test_pr_number_optional(self):
        all_rows = [
            _row(target_kind="issue", target_number=42),
            _row(target_kind="pr", target_number=1, agent="cai-review-pr"),
        ]
        with patch("cai_lib.audit.cost._load_cost_log",
                   return_value=all_rows), \
             patch("cai_lib.transcript_sync.pull_cost", return_value=0):
            got = _load_issue_cost_rows(42, None)
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["agent"], "cai-plan")


class TestStageKey(unittest.TestCase):
    def test_prefers_phase(self):
        self.assertEqual(
            _stage_key({"phase": "plan", "fsm_state": "PLANNING",
                        "category": "plan.plan"}),
            "plan",
        )

    def test_falls_back_to_fsm_state(self):
        self.assertEqual(
            _stage_key({"fsm_state": "PLANNING", "category": "plan.plan"}),
            "PLANNING",
        )

    def test_falls_back_to_category(self):
        self.assertEqual(
            _stage_key({"category": "plan.plan"}),
            "plan.plan",
        )

    def test_unknown_when_all_missing(self):
        self.assertEqual(_stage_key({}), "(unknown)")


class TestBuildFinalCostSummary(unittest.TestCase):
    def test_empty_rows_returns_empty_tuple(self):
        marker, body = _build_final_cost_summary(42, 100, [], 0)
        self.assertEqual(marker, "")
        self.assertEqual(body, "")

    def test_marker_shape_and_regex_match(self):
        rows = [
            _row(agent="cai-plan", cost=0.50, turns=3, duration_ms=1000),
            _row(agent="cai-implement", cost=1.25, turns=10,
                 duration_ms=2000),
        ]
        marker, body = _build_final_cost_summary(42, 100, rows, 2)
        self.assertTrue(marker.startswith("<!-- cai-cost-final "))
        self.assertTrue(marker.endswith(" -->"))
        self.assertIn("issue=42", marker)
        self.assertIn("pr=100", marker)
        self.assertIn("total_usd=1.7500", marker)
        self.assertIn("total_turns=13", marker)
        self.assertIn("total_duration_ms=3000", marker)
        self.assertIn("rows=2", marker)
        self.assertIn("fix_attempt_count=2", marker)
        self.assertIsNotNone(CAI_COST_COMMENT_RE.search(marker))

    def test_headline_and_sections_present(self):
        rows = [
            _row(agent="cai-plan", cost=0.50, category="plan.plan",
                 phase="plan", model="claude-opus-4-7"),
            _row(agent="cai-implement", cost=1.25,
                 category="implement", phase="implement",
                 model="claude-sonnet-4-6"),
        ]
        _marker, body = _build_final_cost_summary(42, 100, rows, 1)
        self.assertIn("## cai final cost summary", body)
        self.assertIn("**Issue:** #42", body)
        self.assertIn("**PR:** #100", body)
        self.assertIn("**Invocations:** 2", body)
        self.assertIn("**fix_attempt_count:** 1", body)
        self.assertIn("$1.7500", body)
        self.assertIn("### Per-agent breakdown", body)
        self.assertIn("`cai-plan`", body)
        self.assertIn("`cai-implement`", body)
        self.assertIn("### Per-stage breakdown", body)
        self.assertIn("`plan`", body)
        self.assertIn("`implement`", body)
        self.assertIn("### Parent model mix", body)
        self.assertIn("`claude-opus-4-7`", body)
        self.assertIn("`claude-sonnet-4-6`", body)

    def test_degrades_when_phase_missing(self):
        """No ``phase`` → per-stage grouping falls back to category."""
        rows = [
            _row(agent="cai-plan", category="plan.plan"),
            _row(agent="cai-implement", category="implement"),
        ]
        _marker, body = _build_final_cost_summary(42, 100, rows, 0)
        self.assertIn("`plan.plan`", body)
        self.assertIn("`implement`", body)

    def test_degrades_when_parent_model_missing(self):
        row = _row(agent="cai-plan")
        row.pop("parent_model", None)
        _marker, body = _build_final_cost_summary(42, 100, [row], 0)
        self.assertIn("`(unknown)`", body)

    def test_cache_hit_rate_derived_from_tokens_when_absent(self):
        rows = [
            _row(agent="cai-plan", input_tokens=100, cache_read=900),
        ]
        _marker, body = _build_final_cost_summary(42, 100, rows, 0)
        # 900 / (900 + 100) = 90.0%
        self.assertIn("Cache hit rate:** 90.0%", body)

    def test_cache_hit_rate_uses_explicit_field_when_present(self):
        rows = [
            _row(agent="cai-plan", cache_hit_rate=0.5),
            _row(agent="cai-implement", cache_hit_rate=0.9),
        ]
        _marker, body = _build_final_cost_summary(42, 100, rows, 0)
        # mean(0.5, 0.9) = 0.7 → 70.0%
        self.assertIn("Cache hit rate:** 70.0%", body)

    def test_cache_hit_rate_zero_denominator_safe(self):
        rows = [
            _row(agent="cai-plan", input_tokens=0, cache_read=0),
        ]
        _marker, body = _build_final_cost_summary(42, 100, rows, 0)
        self.assertIn("Cache hit rate:** 0.0%", body)


class TestPostFinalCostSummary(unittest.TestCase):
    def test_skips_post_when_no_rows(self):
        with patch("cai_lib.cost_summary._load_issue_cost_rows",
                   return_value=[]), \
             patch("cai_lib.github._post_issue_comment") as mock_post:
            with patch("builtins.print"):
                post_final_cost_summary(42, 100)
        self.assertEqual(mock_post.call_count, 0)

    def test_posts_on_issue_when_rows_present(self):
        rows = [_row(agent="cai-plan", cost=0.25)]
        with patch("cai_lib.cost_summary._load_issue_cost_rows",
                   return_value=rows), \
             patch("cai_lib.cost_summary._load_fix_attempt_count",
                   return_value=2), \
             patch("cai_lib.github._post_issue_comment",
                   return_value=True) as mock_post:
            post_final_cost_summary(42, 100)
        self.assertEqual(mock_post.call_count, 1)
        (num, body), kwargs = mock_post.call_args
        self.assertEqual(num, 42)
        self.assertIn("<!-- cai-cost-final ", body)
        self.assertIn("## cai final cost summary", body)
        self.assertEqual(kwargs.get("log_prefix"), "cai cost final")

    def test_swallows_post_exception(self):
        rows = [_row(agent="cai-plan", cost=0.25)]
        with patch("cai_lib.cost_summary._load_issue_cost_rows",
                   return_value=rows), \
             patch("cai_lib.cost_summary._load_fix_attempt_count",
                   return_value=0), \
             patch("cai_lib.github._post_issue_comment",
                   side_effect=RuntimeError("boom")):
            with patch("builtins.print"):
                # Must not raise.
                post_final_cost_summary(42, 100)

    def test_swallows_loader_exception(self):
        with patch("cai_lib.cost_summary._load_issue_cost_rows",
                   side_effect=RuntimeError("boom")), \
             patch("cai_lib.github._post_issue_comment") as mock_post:
            with patch("builtins.print"):
                post_final_cost_summary(42, 100)
        self.assertEqual(mock_post.call_count, 0)


if __name__ == "__main__":
    unittest.main()
