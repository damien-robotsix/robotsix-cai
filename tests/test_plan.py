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
    """_run_plan_agent() surfaces stderr on subprocess failure."""

    @patch("cai_lib.actions.plan._build_issue_block", return_value="")
    @patch("cai_lib.actions.plan._work_directory_block", return_value="")
    @patch("cai_lib.actions.plan._run_claude_p")
    def test_logs_stderr_on_nonzero_exit(self, mock_run, _mwb, _mib):
        from cai_lib.actions import plan
        mock_run.return_value = subprocess.CompletedProcess(
            args=["claude"], returncode=2,
            stdout="", stderr="cai-plan: rate limited",
        )

        with patch("builtins.print") as mock_print:
            out = plan._run_plan_agent(
                {"number": 42, "title": "t", "body": "b"},
                1, Path("/tmp/x"),
            )

        self.assertIn("Plan 1 failed: exit 2", out)
        printed = " ".join(str(c) for c in mock_print.call_args_list)
        self.assertIn("cai-plan: rate limited", printed)


class TestHandlePlanGateAnchorMitigation(unittest.TestCase):
    """#918 — handle_plan_gate routes anchor-mitigated plans via the
    MEDIUM-threshold sibling transition instead of the HIGH default."""

    _ANCHOR_NOTE = (
        "> **Anchor-based edits:** The fix agent must Read each "
        "target file first and locate edits by anchor text (unique "
        "surrounding lines), not by line number.\n\n"
        "## Plan\n### Summary\n..."
    )

    def _issue(self, *, confidence, plan_text):
        return {
            "number": 918,
            "title": "t",
            "body": "",
            "labels": [{"name": "auto-improve:planned"}],
            "_cai_plan_confidence": confidence,
            "_cai_plan_confidence_reason": "line-number drift only",
            "_cai_plan_text": plan_text,
        }

    @patch("cai_lib.actions.plan.apply_transition_with_confidence")
    @patch("cai_lib.actions.plan.log_run")
    def test_medium_with_marker_uses_mitigated_transition(
        self, _mock_log, mock_apply
    ):
        from cai_lib.actions.plan import handle_plan_gate
        mock_apply.return_value = (True, False)

        rc = handle_plan_gate(self._issue(
            confidence=Confidence.MEDIUM,
            plan_text=self._ANCHOR_NOTE,
        ))

        self.assertEqual(rc, 0)
        args = mock_apply.call_args[0]
        # Positional: (issue_number, transition_name, confidence).
        self.assertEqual(args[0], 918)
        self.assertEqual(args[1], "planned_to_plan_approved_mitigated")
        # Reported confidence is passed through unchanged — gating is a
        # property of the selected transition, not a confidence upgrade.
        self.assertEqual(args[2], Confidence.MEDIUM)

    @patch("cai_lib.actions.plan.apply_transition_with_confidence")
    @patch("cai_lib.actions.plan.log_run")
    def test_medium_without_marker_uses_default_transition(
        self, _mock_log, mock_apply
    ):
        from cai_lib.actions.plan import handle_plan_gate
        # Default HIGH transition diverts MEDIUM → (True, True).
        mock_apply.return_value = (True, True)

        rc = handle_plan_gate(self._issue(
            confidence=Confidence.MEDIUM,
            plan_text="plan body with no anchor marker",
        ))

        self.assertEqual(rc, 0)
        args = mock_apply.call_args[0]
        self.assertEqual(args[1], "planned_to_plan_approved")
        self.assertEqual(args[2], Confidence.MEDIUM)

    @patch("cai_lib.actions.plan.apply_transition_with_confidence")
    @patch("cai_lib.actions.plan.log_run")
    def test_low_with_marker_still_diverts(
        self, _mock_log, mock_apply
    ):
        from cai_lib.actions.plan import handle_plan_gate
        mock_apply.return_value = (True, True)

        rc = handle_plan_gate(self._issue(
            confidence=Confidence.LOW,
            plan_text=self._ANCHOR_NOTE,
        ))

        self.assertEqual(rc, 0)
        args = mock_apply.call_args[0]
        # Marker present → mitigated transition is picked; LOW < MEDIUM
        # so the gate still diverts (required=MEDIUM, reported=LOW).
        self.assertEqual(args[1], "planned_to_plan_approved_mitigated")
        self.assertEqual(args[2], Confidence.LOW)

    @patch("cai_lib.actions.plan.apply_transition_with_confidence")
    @patch("cai_lib.actions.plan.log_run")
    def test_high_with_marker_uses_mitigated_transition(
        self, _mock_log, mock_apply
    ):
        from cai_lib.actions.plan import handle_plan_gate
        mock_apply.return_value = (True, False)

        rc = handle_plan_gate(self._issue(
            confidence=Confidence.HIGH,
            plan_text=self._ANCHOR_NOTE,
        ))

        self.assertEqual(rc, 0)
        args = mock_apply.call_args[0]
        # Marker present → mitigated transition regardless of reported
        # confidence. HIGH >= MEDIUM so the gate passes.
        self.assertEqual(args[1], "planned_to_plan_approved_mitigated")
        self.assertEqual(args[2], Confidence.HIGH)

    @patch("cai_lib.actions.plan.apply_transition_with_confidence")
    @patch("cai_lib.actions.plan.log_run")
    def test_high_without_marker_uses_default_transition(
        self, _mock_log, mock_apply
    ):
        from cai_lib.actions.plan import handle_plan_gate
        mock_apply.return_value = (True, False)

        rc = handle_plan_gate(self._issue(
            confidence=Confidence.HIGH,
            plan_text="plan body with no anchor marker",
        ))

        self.assertEqual(rc, 0)
        args = mock_apply.call_args[0]
        self.assertEqual(args[1], "planned_to_plan_approved")
        self.assertEqual(args[2], Confidence.HIGH)


class TestPlanHasAnchorMitigationHelper(unittest.TestCase):
    """#918 — module-private anchor-mitigation regex helper."""

    def test_canonical_note_matches(self):
        from cai_lib.actions.plan import _plan_has_anchor_mitigation
        plan = (
            "> **Anchor-based edits:** Read first and locate edits by "
            "anchor text (unique surrounding lines), not by line number.\n"
        )
        self.assertTrue(_plan_has_anchor_mitigation(plan))

    def test_case_insensitive(self):
        from cai_lib.actions.plan import _plan_has_anchor_mitigation
        self.assertTrue(_plan_has_anchor_mitigation(
            "LOCATE EDITS BY ANCHOR TEXT ... NOT BY LINE NUMBER"
        ))

    def test_crosses_newlines(self):
        from cai_lib.actions.plan import _plan_has_anchor_mitigation
        plan = (
            "Locate edits by anchor text in each file,\n"
            "and do not rely on absolute line numbers "
            "- not by line number.\n"
        )
        self.assertTrue(_plan_has_anchor_mitigation(plan))

    def test_missing_marker_returns_false(self):
        from cai_lib.actions.plan import _plan_has_anchor_mitigation
        self.assertFalse(_plan_has_anchor_mitigation(""))
        self.assertFalse(_plan_has_anchor_mitigation(None))
        self.assertFalse(_plan_has_anchor_mitigation(
            "plan body with no marker at all"
        ))

    def test_partial_marker_returns_false(self):
        from cai_lib.actions.plan import _plan_has_anchor_mitigation
        # Only one half of the phrase — must not trigger the override.
        self.assertFalse(_plan_has_anchor_mitigation(
            "locate edits by anchor text only"
        ))
        self.assertFalse(_plan_has_anchor_mitigation(
            "do not use line numbers"
        ))


if __name__ == "__main__":
    unittest.main()
