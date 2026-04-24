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

from claude_agent_sdk.types import (
    AssistantMessage,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
)

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


def _mk_assistant(model: str, *, parent_tool_use_id: str | None = None,
                  text: str = "hi") -> AssistantMessage:
    return AssistantMessage(
        content=[TextBlock(text=text)],
        model=model,
        parent_tool_use_id=parent_tool_use_id,
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

    def test_matches_cai_cost_final_marker(self):
        """Issue #1198: the widened regex also catches the close-time
        roll-up marker so ``_strip_cost_comments`` filters it out of
        agent-input streams the same way it filters per-run markers."""
        body = (
            "<!-- cai-cost-final issue=1 pr=2 total_usd=1.2300 "
            "total_turns=42 total_duration_ms=123456 rows=7 "
            "fix_attempt_count=3 -->\n"
            "## cai final cost summary\n\n..."
        )
        self.assertIsNotNone(CAI_COST_COMMENT_RE.search(body))


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
    @patch("cai_lib.subagent.legacy.log_cost")
    def test_posts_issue_comment_when_target_issue(self, _mock_log):
        from cai_lib.subagent import _run_claude_p, core, legacy

        msg = _mk_result()
        with patch.object(core, "query", _mock_query(msg)), \
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

    @patch("cai_lib.subagent.legacy.log_cost")
    def test_posts_pr_comment_when_target_pr(self, _mock_log):
        from cai_lib.subagent import _run_claude_p, core, legacy

        msg = _mk_result()
        with patch.object(core, "query", _mock_query(msg)), \
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

    @patch("cai_lib.subagent.legacy.log_cost")
    def test_no_comment_when_kwargs_omitted(self, _mock_log):
        from cai_lib.subagent import _run_claude_p, core, legacy

        msg = _mk_result()
        with patch.object(core, "query", _mock_query(msg)), \
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

    @patch("cai_lib.subagent.legacy.log_cost")
    def test_no_comment_when_only_one_kwarg_set(self, _mock_log):
        from cai_lib.subagent import _run_claude_p, core, legacy

        msg = _mk_result()
        with patch.object(core, "query", _mock_query(msg)), \
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

    @patch("cai_lib.subagent.legacy.log_cost")
    def test_posting_failure_does_not_change_returncode(self, _mock_log):
        from cai_lib.subagent import _run_claude_p, core, legacy

        msg = _mk_result()
        with patch.object(core, "query", _mock_query(msg)), \
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

    @patch("cai_lib.subagent.legacy.log_cost")
    def test_posts_comment_on_agent_error_path(self, _mock_log):
        """is_error=True still posts a cost comment — cost is incurred
        either way and the attribution is useful for diagnosing failed
        runs."""
        from cai_lib.subagent import _run_claude_p, core, legacy

        msg = _mk_result(
            subtype="error_max_turns", is_error=True,
            result="exhausted turns",
        )
        with patch.object(core, "query", _mock_query(msg)), \
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


class TestCostCommentParentModel(unittest.TestCase):
    """Parent model picked from the first parent-level ``AssistantMessage``,
    not ``next(iter(model_usage))`` — which otherwise mislabels opus-
    configured agents with whichever haiku subagent/helper fired first."""

    @patch("cai_lib.subagent.legacy.log_cost")
    def test_parent_model_wins_over_first_model_usage_key(self, _mock_log):
        from cai_lib.subagent import _run_claude_p, core, legacy

        # model_usage dict orders haiku first (a subagent / memory helper
        # that ran before the parent's first assistant turn). The parent
        # AssistantMessage carries opus.
        msg_result = _mk_result(
            model_usage={
                "claude-haiku-4-5-20251001": {},
                "claude-opus-4-7": {},
            }
        )
        msg_sub = _mk_assistant(
            "claude-haiku-4-5-20251001", parent_tool_use_id="tool_abc",
        )
        msg_parent = _mk_assistant(
            "claude-opus-4-7", parent_tool_use_id=None,
        )
        with patch.object(core, "query",
                          _mock_query(msg_sub, msg_parent, msg_result)), \
             patch("cai_lib.github._post_issue_comment",
                   return_value=True) as mock_issue:
            _run_claude_p(
                ["claude", "-p", "--agent", "cai-refine"],
                category="refine",
                agent="cai-refine",
                target_kind="issue",
                target_number=1,
            )
        (_num, body), _kwargs = mock_issue.call_args
        self.assertIn("model=claude-opus-4-7", body)
        self.assertIn("subagent_models=claude-haiku-4-5-20251001", body)
        self.assertIn("on `claude-opus-4-7", body)
        # and definitely NOT the haiku-first mislabel
        self.assertNotIn("model=claude-haiku-4-5-20251001", body)

    @patch("cai_lib.subagent.legacy.log_cost")
    def test_falls_back_to_model_usage_when_no_parent_message(
        self, _mock_log,
    ):
        """When the run has no parent-level AssistantMessage (unusual —
        happens on very-early crash paths), the old ``next(iter(...))``
        heuristic still applies so the comment is never blank."""
        from cai_lib.subagent import _run_claude_p, core, legacy

        msg_result = _mk_result(model_usage={"claude-sonnet-4-6": {}})
        with patch.object(core, "query", _mock_query(msg_result)), \
             patch("cai_lib.github._post_issue_comment",
                   return_value=True) as mock_issue:
            _run_claude_p(
                ["claude", "-p", "--agent", "cai-merge"],
                category="merge",
                agent="cai-merge",
                target_kind="issue",
                target_number=2,
            )
        (_num, body), _kwargs = mock_issue.call_args
        self.assertIn("model=claude-sonnet-4-6", body)
        # no subagents when only one model was used
        self.assertNotIn("subagent_models=", body)

    @patch("cai_lib.subagent.legacy.log_cost")
    def test_single_model_run_has_no_subagent_field(self, _mock_log):
        from cai_lib.subagent import _run_claude_p, core, legacy

        msg_result = _mk_result(model_usage={"claude-opus-4-7": {}})
        msg_parent = _mk_assistant("claude-opus-4-7")
        with patch.object(core, "query",
                          _mock_query(msg_parent, msg_result)), \
             patch("cai_lib.github._post_issue_comment",
                   return_value=True) as mock_issue:
            _run_claude_p(
                ["claude", "-p", "--agent", "cai-plan"],
                category="plan.plan",
                agent="cai-plan",
                target_kind="issue",
                target_number=3,
            )
        (_num, body), _kwargs = mock_issue.call_args
        self.assertIn("model=claude-opus-4-7", body)
        self.assertNotIn("subagent_models=", body)
        self.assertNotIn("subagent model(s)", body)


class TestCostCommentPerModelDetail(unittest.TestCase):
    """Per-model cost/token breakdown in the comment body."""

    @patch("cai_lib.subagent.legacy.log_cost")
    def test_per_model_lines_rendered(self, _mock_log):
        from cai_lib.subagent import _run_claude_p, core, legacy

        msg_result = _mk_result(
            model_usage={
                "claude-opus-4-7": {
                    "inputTokens": 32,
                    "outputTokens": 8288,
                    "cacheReadInputTokens": 1029092,
                    "cacheCreationInputTokens": 48773,
                    "costUSD": 1.02673725,
                },
                "claude-haiku-4-5-20251001": {
                    "inputTokens": 818,
                    "outputTokens": 16,
                    "cacheReadInputTokens": 0,
                    "cacheCreationInputTokens": 0,
                    "costUSD": 0.000898,
                },
            },
        )
        msg_parent = _mk_assistant("claude-opus-4-7")
        with patch.object(core, "query",
                          _mock_query(msg_parent, msg_result)), \
             patch("cai_lib.github._post_issue_comment",
                   return_value=True) as mock_issue:
            _run_claude_p(
                ["claude", "-p", "--agent", "cai-refine"],
                category="refine",
                agent="cai-refine",
                target_kind="issue",
                target_number=1188,
            )
        (_num, body), _kwargs = mock_issue.call_args
        self.assertIn("`claude-opus-4-7` (parent): $1.0267", body)
        self.assertIn("`claude-haiku-4-5-20251001` (subagent): $0.0009",
                      body)
        self.assertIn("in=32 ", body)
        self.assertIn("out=8288 ", body)
        self.assertIn("cache_read=1029092", body)
        # parent comes before subagent in the body
        self.assertLess(body.index("(parent)"), body.index("(subagent)"))

    @patch("cai_lib.subagent.legacy.log_cost")
    def test_per_category_cost_split_rendered(self, _mock_log):
        """Each per-model line carries an inline $X.XXXX split for
        each of in / out / cache_read / cache_create, derived from
        the fixed Claude 4.x pricing ratios."""
        from cai_lib.subagent import _run_claude_p, core, legacy

        # Canonical #1191-plan figures: in=34, out=34394,
        # cache_read=3_267_345, cache_create=127_540, total=$3.2908.
        # Expected split via ratios 1:5:0.1:1.25:
        #   weighted = 34 + 5*34394 + 0.1*3267345 + 1.25*127540
        #            = 658163.5
        #   scale    = 3.2908 / 658163.5 ≈ 5.0e-6
        #   in_cost       ≈ $0.0002
        #   out_cost      ≈ $0.8599
        #   cache_read    ≈ $1.6337
        #   cache_write   ≈ $0.7971
        msg_result = _mk_result(
            model_usage={
                "claude-opus-4-7": {
                    "inputTokens": 34,
                    "outputTokens": 34394,
                    "cacheReadInputTokens": 3267345,
                    "cacheCreationInputTokens": 127540,
                    "costUSD": 3.2908,
                },
            },
        )
        msg_parent = _mk_assistant("claude-opus-4-7")
        with patch.object(core, "query",
                          _mock_query(msg_parent, msg_result)), \
             patch("cai_lib.github._post_issue_comment",
                   return_value=True) as mock_issue:
            _run_claude_p(
                ["claude", "-p", "--agent", "cai-plan"],
                category="plan.plan",
                agent="cai-plan",
                target_kind="issue",
                target_number=1191,
            )
        (_num, body), _kwargs = mock_issue.call_args
        # Tokens kept verbatim, dollar splits from the 1:5:0.1:1.25
        # ratios. The four splits must sum to the total $3.2908.
        self.assertIn("in=34 ($0.0002)", body)
        self.assertIn("out=34394 ($0.8598)", body)
        self.assertIn("cache_read=3267345 ($1.6337)", body)
        self.assertIn("cache_create=127540 ($0.7971)", body)

    def test_split_cost_by_category_zero_tokens(self):
        """Zero tokens yields zero split, no DivisionByZero."""
        from cai_lib.cost_comment import _split_cost_by_category

        self.assertEqual(
            _split_cost_by_category(0.0, 0, 0, 0, 0),
            {"input": 0.0, "output": 0.0, "cache_read": 0.0, "cache_write": 0.0},
        )
        self.assertEqual(
            _split_cost_by_category(1.0, 0, 0, 0, 0),
            {"input": 0.0, "output": 0.0, "cache_read": 0.0, "cache_write": 0.0},
        )


class TestCostCommentSubagentInvocations(unittest.TestCase):
    """Task-tool invocation tracking — which subagents and how many times."""

    @staticmethod
    def _assistant_with_task(subagent_type: str | None,
                             tool_use_id: str = "tool_1"):
        content: list = [TextBlock(text="spawning sub")]
        input_dict: dict = {}
        if subagent_type is not None:
            input_dict["subagent_type"] = subagent_type
        content.append(ToolUseBlock(
            id=tool_use_id, name="Task", input=input_dict,
        ))
        return AssistantMessage(
            content=content,
            model="claude-opus-4-7",
            parent_tool_use_id=None,
        )

    @patch("cai_lib.subagent.legacy.log_cost")
    def test_subagent_counts_rendered(self, mock_log):
        from cai_lib.subagent import _run_claude_p, core, legacy

        parent1 = self._assistant_with_task("cai-dup-check", "t1")
        parent2 = self._assistant_with_task("cai-dup-check", "t2")
        parent3 = self._assistant_with_task("Explore", "t3")
        msg_result = _mk_result(model_usage={"claude-opus-4-7": {}})
        with patch.object(core, "query",
                          _mock_query(parent1, parent2, parent3,
                                      msg_result)), \
             patch("cai_lib.github._post_issue_comment",
                   return_value=True) as mock_issue:
            _run_claude_p(
                ["claude", "-p", "--agent", "cai-refine"],
                category="refine",
                agent="cai-refine",
                target_kind="issue",
                target_number=1,
            )
        (_num, body), _kwargs = mock_issue.call_args
        # Summary line shows invoked subagents with counts
        self.assertIn("`Explore` ×1", body)
        self.assertIn("`cai-dup-check` ×2", body)
        self.assertIn("subagents invoked:", body)
        # Marker carries the machine-parsable form
        self.assertIn(
            "subagents_invoked=Explore:1,cai-dup-check:2", body,
        )
        # The cost row logged to disk carries the same mapping
        row = mock_log.call_args[0][0]
        self.assertEqual(
            row.get("subagents"), {"cai-dup-check": 2, "Explore": 1},
        )

    @patch("cai_lib.subagent.legacy.log_cost")
    def test_missing_subagent_type_buckets_as_general_purpose(
        self, _mock_log,
    ):
        from cai_lib.subagent import _run_claude_p, core, legacy

        parent = self._assistant_with_task(None, "t1")
        msg_result = _mk_result(model_usage={"claude-opus-4-7": {}})
        with patch.object(core, "query",
                          _mock_query(parent, msg_result)), \
             patch("cai_lib.github._post_issue_comment",
                   return_value=True) as mock_issue:
            _run_claude_p(
                ["claude", "-p", "--agent", "cai-refine"],
                category="refine",
                agent="cai-refine",
                target_kind="issue",
                target_number=1,
            )
        (_num, body), _kwargs = mock_issue.call_args
        self.assertIn("`general-purpose` ×1", body)

    @patch("cai_lib.subagent.legacy.log_cost")
    def test_no_subagent_line_when_no_task_invocations(self, _mock_log):
        from cai_lib.subagent import _run_claude_p, core, legacy

        msg_parent = _mk_assistant("claude-opus-4-7")
        msg_result = _mk_result(model_usage={"claude-opus-4-7": {}})
        with patch.object(core, "query",
                          _mock_query(msg_parent, msg_result)), \
             patch("cai_lib.github._post_issue_comment",
                   return_value=True) as mock_issue:
            _run_claude_p(
                ["claude", "-p", "--agent", "cai-plan"],
                category="plan.plan",
                agent="cai-plan",
                target_kind="issue",
                target_number=2,
            )
        (_num, body), _kwargs = mock_issue.call_args
        self.assertNotIn("subagents invoked:", body)
        self.assertNotIn("subagents_invoked=", body)


if __name__ == "__main__":
    unittest.main()
