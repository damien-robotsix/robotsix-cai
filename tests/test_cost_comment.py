"""Tests for the per-target cost-attribution comment pipeline.

Covers:
  1. ``CAI_COST_COMMENT_RE`` matches the marker emitted by
     ``_post_cost_comment`` and does NOT match ordinary comment bodies.
  2. ``_strip_cost_comments`` filters out every cost-marker comment
     and preserves non-marker comments in order.
  3. ``_run_claude_p`` invokes ``_post_cost_comment`` on the success
     path when both ``target_kind`` and ``target_number`` are set and
     leaves no comment otherwise.
  4. A failure inside the posting helper is swallowed and does NOT
     change ``CompletedProcess.returncode`` (contract: cost comment
     is informational, never gating).

All tests use ``unittest`` to match the project convention — pytest is
not installed.
"""
import os
import sys
import subprocess
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from claude_agent_sdk.types import ResultMessage

from cai_lib.config import CAI_COST_COMMENT_RE
from cai_lib.github import _strip_cost_comments


def _mk_result(**fields) -> ResultMessage:
    return ResultMessage(
        subtype=fields.pop("subtype", "success"),
        duration_ms=fields.pop("duration_ms", 1234),
        duration_api_ms=fields.pop("duration_api_ms", 999),
        is_error=fields.pop("is_error", False),
        num_turns=fields.pop("num_turns", 3),
        session_id=fields.pop("session_id", "s1"),
        total_cost_usd=fields.pop("total_cost_usd", 0.1234),
        usage=fields.pop("usage", {"input_tokens": 100, "output_tokens": 50}),
        result=fields.pop("result", "ok"),
        structured_output=fields.pop("structured_output", None),
        model_usage=fields.pop("model_usage", {"claude-sonnet-4": {}}),
    )


def _mock_query(*messages):
    async def _gen(*, prompt, options=None, transport=None):
        for m in messages:
            yield m
    return _gen


class TestCostMarkerRegex(unittest.TestCase):
    def test_matches_marker_comment(self):
        body = (
            "<!-- cai-cost agent=cai-refine category=refine "
            "model=claude-sonnet-4 cost_usd=0.1234 turns=3 "
            "duration_ms=1234 input_tokens=100 output_tokens=50 "
            "is_error=False ts=2026-04-22T00:00:00Z -->\n"
            "**Agent cost:** `cai-refine` on `claude-sonnet-4` — ..."
        )
        self.assertIsNotNone(CAI_COST_COMMENT_RE.search(body))

    def test_does_not_match_ordinary_comment(self):
        body = "Please address the comment on line 42."
        self.assertIsNone(CAI_COST_COMMENT_RE.search(body))

    def test_does_not_match_lock_comment(self):
        body = (
            "<!-- cai-lock owner=abc123 acquired=2026-04-22T00:00:00Z -->"
        )
        self.assertIsNone(CAI_COST_COMMENT_RE.search(body))


class TestStripCostComments(unittest.TestCase):
    def test_filters_out_cost_markers_only(self):
        cost = {
            "body": "<!-- cai-cost agent=x category=y -->\nAgent cost: ..."
        }
        normal = {"body": "a real reviewer comment"}
        other = {"body": "<!-- cai-lock owner=abc -->"}
        result = _strip_cost_comments([cost, normal, other])
        self.assertEqual(result, [normal, other])

    def test_preserves_order(self):
        a = {"body": "first"}
        b = {"body": "<!-- cai-cost agent=x -->"}
        c = {"body": "third"}
        self.assertEqual(_strip_cost_comments([a, b, c]), [a, c])

    def test_handles_empty_and_none_body(self):
        self.assertEqual(_strip_cost_comments([]), [])
        self.assertEqual(
            _strip_cost_comments([{"body": None}, {"body": ""}]),
            [{"body": None}, {"body": ""}],
        )

    def test_input_not_mutated(self):
        comments = [
            {"body": "<!-- cai-cost agent=x -->"},
            {"body": "stay"},
        ]
        original_len = len(comments)
        _ = _strip_cost_comments(comments)
        self.assertEqual(len(comments), original_len)


class TestRunClaudePPostsCostComment(unittest.TestCase):
    @patch("cai_lib.subprocess_utils.log_cost")
    def test_posts_issue_comment_when_target_issue(self, _mock_log):
        from cai_lib import subprocess_utils
        from cai_lib.subprocess_utils import _run_claude_p

        msg = _mk_result()
        with patch.object(subprocess_utils, "query", _mock_query(msg)), \
             patch("cai_lib.github._post_issue_comment",
                   return_value=True) as mock_issue, \
             patch("cai_lib.github._post_pr_comment",
                   return_value=True) as mock_pr:
            proc = _run_claude_p(
                ["claude", "-p", "--agent", "cai-refine"],
                category="refine",
                agent="cai-refine",
                target_kind="issue",
                target_number=42,
            )
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(mock_issue.call_count, 1)
        self.assertEqual(mock_pr.call_count, 0)
        (num, body), _kwargs = mock_issue.call_args
        self.assertEqual(num, 42)
        self.assertIn("<!-- cai-cost", body)
        self.assertIn("agent=cai-refine", body)
        self.assertIn("category=refine", body)
        self.assertIn("Agent cost:", body)
        self.assertIsNotNone(CAI_COST_COMMENT_RE.search(body))

    @patch("cai_lib.subprocess_utils.log_cost")
    def test_posts_pr_comment_when_target_pr(self, _mock_log):
        from cai_lib import subprocess_utils
        from cai_lib.subprocess_utils import _run_claude_p

        msg = _mk_result()
        with patch.object(subprocess_utils, "query", _mock_query(msg)), \
             patch("cai_lib.github._post_issue_comment",
                   return_value=True) as mock_issue, \
             patch("cai_lib.github._post_pr_comment",
                   return_value=True) as mock_pr:
            proc = _run_claude_p(
                ["claude", "-p", "--agent", "cai-merge"],
                category="merge",
                agent="cai-merge",
                target_kind="pr",
                target_number=77,
            )
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(mock_pr.call_count, 1)
        self.assertEqual(mock_issue.call_count, 0)

    @patch("cai_lib.subprocess_utils.log_cost")
    def test_no_comment_when_kwargs_omitted(self, _mock_log):
        from cai_lib import subprocess_utils
        from cai_lib.subprocess_utils import _run_claude_p

        msg = _mk_result()
        with patch.object(subprocess_utils, "query", _mock_query(msg)), \
             patch("cai_lib.github._post_issue_comment") as mock_issue, \
             patch("cai_lib.github._post_pr_comment") as mock_pr:
            proc = _run_claude_p(
                ["claude", "-p", "--agent", "cai-plan"],
                category="plan.plan",
                agent="cai-plan",
            )
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(mock_issue.call_count, 0)
        self.assertEqual(mock_pr.call_count, 0)

    @patch("cai_lib.subprocess_utils.log_cost")
    def test_no_comment_when_only_one_kwarg_set(self, _mock_log):
        from cai_lib import subprocess_utils
        from cai_lib.subprocess_utils import _run_claude_p

        msg = _mk_result()
        with patch.object(subprocess_utils, "query", _mock_query(msg)), \
             patch("cai_lib.github._post_issue_comment") as mock_issue, \
             patch("cai_lib.github._post_pr_comment") as mock_pr:
            proc = _run_claude_p(
                ["claude", "-p", "--agent", "cai-plan"],
                category="plan.plan",
                agent="cai-plan",
                target_kind="issue",
            )
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(mock_issue.call_count, 0)
        self.assertEqual(mock_pr.call_count, 0)

    @patch("cai_lib.subprocess_utils.log_cost")
    def test_posting_failure_does_not_change_returncode(self, _mock_log):
        from cai_lib import subprocess_utils
        from cai_lib.subprocess_utils import _run_claude_p

        msg = _mk_result()
        with patch.object(subprocess_utils, "query", _mock_query(msg)), \
             patch("cai_lib.github._post_issue_comment",
                   side_effect=subprocess.CalledProcessError(
                       1, ["gh"], stderr="boom")):
            with patch("builtins.print"):
                proc = _run_claude_p(
                    ["claude", "-p", "--agent", "cai-refine"],
                    category="refine",
                    agent="cai-refine",
                    target_kind="issue",
                    target_number=42,
                )
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, "ok")

    @patch("cai_lib.subprocess_utils.log_cost")
    def test_posts_comment_on_agent_error_path(self, _mock_log):
        """is_error=True still posts a cost comment — cost is incurred
        either way and the attribution is useful for diagnosing failed
        runs."""
        from cai_lib import subprocess_utils
        from cai_lib.subprocess_utils import _run_claude_p

        msg = _mk_result(
            subtype="error_max_turns", is_error=True,
            result="exhausted turns",
        )
        with patch.object(subprocess_utils, "query", _mock_query(msg)), \
             patch("cai_lib.github._post_issue_comment",
                   return_value=True) as mock_issue:
            with patch("builtins.print"):
                proc = _run_claude_p(
                    ["claude", "-p", "--agent", "cai-implement"],
                    category="implement",
                    agent="cai-implement",
                    target_kind="issue",
                    target_number=5,
                )
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(mock_issue.call_count, 1)
        (_num, body), _kwargs = mock_issue.call_args
        self.assertIn("is_error=True", body)


if __name__ == "__main__":
    unittest.main()
