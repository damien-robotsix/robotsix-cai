"""Tests for cai_lib.actions.plan — handle_plan() behaviour."""
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cai_lib.fsm import Confidence, IssueState


class TestHandlePlanUnexpectedState(unittest.TestCase):
    """handle_plan() must abort immediately for any state other than REFINED or PLANNING."""

    @patch("cai_lib.actions.plan._run_plan_select_pipeline")
    @patch("cai_lib.actions.plan.log_run")
    @patch("cai_lib.actions.plan.get_issue_state", return_value=IssueState.RAISED)
    def test_raised_state_returns_1_without_pipeline(
        self, mock_state, mock_log_run, mock_pipeline
    ):
        from cai_lib.actions.plan import handle_plan

        issue = {"number": 42, "title": "test issue", "labels": [], "body": ""}
        result = handle_plan(issue)

        self.assertEqual(result, 1)
        mock_pipeline.assert_not_called()
        mock_log_run.assert_called_once()
        # Confirm the log_run was for unexpected_state
        call_kwargs = mock_log_run.call_args
        self.assertIn("unexpected_state", str(call_kwargs))


class TestRunSelectAgent(unittest.TestCase):
    """_run_select_agent() diagnostics and parse robustness."""

    def _issue(self):
        return {"number": 777, "title": "t", "body": "b", "labels": []}

    def _completed(self, stdout: str = "", stderr: str = "", returncode: int = 0):
        return subprocess.CompletedProcess(
            args=["claude"], returncode=returncode, stdout=stdout, stderr=stderr,
        )

    @patch("cai_lib.actions.plan._run_claude_p")
    def test_parses_valid_json(self, mock_run):
        from cai_lib.actions.plan import _run_select_agent
        mock_run.return_value = self._completed(stdout=(
            '{"plan":"do X","confidence":"HIGH",'
            '"confidence_reason":"both plans converge"}'
        ))

        out = _run_select_agent(self._issue(), ["p1", "p2"], Path("/tmp/x"))

        self.assertIsNotNone(out)
        plan, conf, reason = out
        self.assertIn("do X", plan)
        self.assertEqual(conf, Confidence.HIGH)
        self.assertEqual(reason, "both plans converge")

    @patch("cai_lib.actions.plan._run_claude_p")
    def test_strips_markdown_code_fence(self, mock_run):
        """Model sometimes wraps --json-schema output in ```json``` — we should cope."""
        from cai_lib.actions.plan import _run_select_agent
        mock_run.return_value = self._completed(stdout=(
            '```json\n'
            '{"plan":"go","confidence":"MEDIUM",'
            '"confidence_reason":"scope unclear"}\n'
            '```'
        ))

        out = _run_select_agent(self._issue(), ["p1", "p2"], Path("/tmp/x"))

        self.assertIsNotNone(out)
        _, conf, reason = out
        self.assertEqual(conf, Confidence.MEDIUM)
        self.assertEqual(reason, "scope unclear")

    @patch("cai_lib.actions.plan._run_claude_p")
    def test_logs_stderr_on_nonzero_exit(self, mock_run):
        """When cai-select exits non-zero, stderr must surface in the log."""
        from cai_lib.actions import plan
        mock_run.return_value = self._completed(
            stdout="",
            stderr="boom: API overloaded",
            returncode=1,
        )

        with patch("builtins.print") as mock_print:
            out = plan._run_select_agent(self._issue(), ["p1", "p2"], Path("/tmp/x"))

        self.assertIsNone(out)
        # Stderr preview must appear in at least one print call.
        printed = " ".join(str(c) for c in mock_print.call_args_list)
        self.assertIn("boom: API overloaded", printed)
        self.assertIn("exit 1", printed)

    @patch("cai_lib.actions.plan._run_claude_p")
    def test_invalid_json_stdout_preview_in_log(self, mock_run):
        from cai_lib.actions import plan
        mock_run.return_value = self._completed(stdout="not json at all <xml/>")

        with patch("builtins.print") as mock_print:
            out = plan._run_select_agent(self._issue(), ["p1", "p2"], Path("/tmp/x"))

        self.assertIsNone(out)
        printed = " ".join(str(c) for c in mock_print.call_args_list)
        self.assertIn("not valid JSON", printed)
        self.assertIn("not json at all", printed)


class TestRunPlanAgent(unittest.TestCase):
    """_run_plan_agent() retries once on non-zero exit and surfaces stderr."""

    @patch("cai_lib.actions.plan._build_issue_block", return_value="")
    @patch("cai_lib.actions.plan._work_directory_block", return_value="")
    @patch("cai_lib.actions.plan._run_claude_p")
    def test_retries_once_on_nonzero_then_fails(self, mock_run, _mwb, _mib):
        from cai_lib.actions import plan
        failed = subprocess.CompletedProcess(
            args=["claude"], returncode=2,
            stdout="", stderr="cai-plan: rate limited",
        )
        mock_run.side_effect = [failed, failed]

        with patch("builtins.print") as mock_print:
            text, ok = plan._run_plan_agent(
                {"number": 42, "title": "t", "body": "b"},
                1, Path("/tmp/x"),
            )

        self.assertFalse(ok)
        self.assertIn("Plan 1 failed: exit 2", text)
        self.assertEqual(mock_run.call_count, 2)
        printed = " ".join(str(c) for c in mock_print.call_args_list)
        self.assertIn("cai-plan: rate limited", printed)
        self.assertIn("attempt 1/2", printed)
        self.assertIn("attempt 2/2", printed)

    @patch("cai_lib.actions.plan._build_issue_block", return_value="")
    @patch("cai_lib.actions.plan._work_directory_block", return_value="")
    @patch("cai_lib.actions.plan._run_claude_p")
    def test_retry_recovers_on_second_attempt(self, mock_run, _mwb, _mib):
        from cai_lib.actions import plan
        failed = subprocess.CompletedProcess(
            args=["claude"], returncode=1, stdout="", stderr="transient",
        )
        ok_proc = subprocess.CompletedProcess(
            args=["claude"], returncode=0,
            stdout="## Plan\n\nSummary: do X", stderr="",
        )
        mock_run.side_effect = [failed, ok_proc]

        text, ok = plan._run_plan_agent(
            {"number": 42, "title": "t", "body": "b"},
            2, Path("/tmp/x"),
        )

        self.assertTrue(ok)
        self.assertIn("do X", text)
        self.assertEqual(mock_run.call_count, 2)


class TestExtractRefinedPlan(unittest.TestCase):
    """_extract_refined_plan() extracts the ## Refined Issue block."""

    def test_returns_block_stripped_at_separator(self):
        from cai_lib.actions.plan import _extract_refined_plan
        body = (
            "## Refined Issue\n\n"
            "### Description\nfix the thing\n\n"
            "### Plan\n1. do it\n\n"
            "---\n\n"
            "> **Original issue text:**\n> please fix\n"
        )
        result = _extract_refined_plan(body)
        self.assertIsNotNone(result)
        self.assertIn("## Refined Issue", result)
        self.assertIn("fix the thing", result)
        self.assertNotIn("Original issue text", result)

    def test_returns_none_when_marker_missing(self):
        from cai_lib.actions.plan import _extract_refined_plan
        body = "## Some other section\n\nstuff\n"
        self.assertIsNone(_extract_refined_plan(body))

    def test_strips_preexisting_stored_plan_block(self):
        from cai_lib.actions.plan import _extract_refined_plan
        body = (
            "<!-- cai-plan-start -->\n"
            "## Selected Implementation Plan\n\nold plan\n"
            "<!-- cai-plan-end -->\n\n"
            "## Refined Issue\n\nthe real plan\n"
        )
        result = _extract_refined_plan(body)
        self.assertIsNotNone(result)
        self.assertIn("## Refined Issue", result)
        self.assertIn("the real plan", result)
        self.assertNotIn("old plan", result)


class TestPlanSelectPipelineFallback(unittest.TestCase):
    """When both planners fail all retries, fall back to refined plan."""

    @patch("cai_lib.actions.plan._run_select_agent")
    @patch("cai_lib.actions.plan._run_plan_agent")
    def test_both_failed_uses_refined_plan_with_low_confidence(
        self, mock_plan, mock_select,
    ):
        from cai_lib.actions import plan
        mock_plan.side_effect = [
            ("(Plan 1 failed: exit 1 after 2 attempts)", False),
            ("(Plan 2 failed: exit 1 after 2 attempts)", False),
        ]
        issue = {
            "number": 99, "title": "t",
            "body": (
                "## Refined Issue\n\n"
                "### Description\nfix the thing\n\n"
                "### Plan\n1. do it\n\n"
                "---\n\n"
                "> **Original issue text:**\n> please fix\n"
            ),
        }

        result = plan._run_plan_select_pipeline(issue, Path("/tmp/x"))

        self.assertIsNotNone(result)
        plan_text, conf, reason = result
        self.assertIn("## Refined Issue", plan_text)
        self.assertNotIn("Original issue text", plan_text)  # trimmed at ---
        self.assertEqual(conf, Confidence.LOW)
        self.assertIn("Both cai-plan invocations failed", reason)
        mock_select.assert_not_called()

    @patch("cai_lib.actions.plan._run_select_agent")
    @patch("cai_lib.actions.plan._run_plan_agent")
    def test_partial_success_still_runs_select(self, mock_plan, mock_select):
        from cai_lib.actions import plan
        mock_plan.side_effect = [
            ("(Plan 1 failed: exit 1 after 2 attempts)", False),
            ("## Plan\nSummary: do X", True),
        ]
        mock_select.return_value = (
            "selected", Confidence.MEDIUM, "ok",
        )
        issue = {"number": 99, "title": "t",
                 "body": "## Refined Issue\n\nstuff\n"}

        result = plan._run_plan_select_pipeline(issue, Path("/tmp/x"))

        self.assertIsNotNone(result)
        mock_select.assert_called_once()


if __name__ == "__main__":
    unittest.main()
