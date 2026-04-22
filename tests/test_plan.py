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
        plan, conf, reason, requires_review, approvable = out
        self.assertIn("do X", plan)
        self.assertEqual(conf, Confidence.HIGH)
        self.assertEqual(reason, "both plans converge")
        self.assertFalse(requires_review)
        self.assertFalse(approvable)

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
        _, conf, reason, requires_review, approvable = out
        self.assertEqual(conf, Confidence.MEDIUM)
        self.assertEqual(reason, "scope unclear")
        self.assertFalse(requires_review)
        self.assertFalse(approvable)

    @patch("cai_lib.actions.plan._run_claude_p")
    def test_parses_requires_human_review_true(self, mock_run):
        """cai-select may set requires_human_review=true on knowing divergence (#982)."""
        from cai_lib.actions.plan import _run_select_agent
        mock_run.return_value = self._completed(stdout=(
            '{"plan":"swap X for Y","confidence":"MEDIUM",'
            '"confidence_reason":"plan knowingly diverges from refined preference",'
            '"requires_human_review":true}'
        ))

        out = _run_select_agent(self._issue(), ["p1", "p2"], Path("/tmp/x"))

        self.assertIsNotNone(out)
        _, conf, _, requires_review, approvable = out
        self.assertEqual(conf, Confidence.MEDIUM)
        self.assertTrue(requires_review)
        self.assertFalse(approvable)

    @patch("cai_lib.actions.plan._run_claude_p")
    def test_requires_human_review_defaults_to_false(self, mock_run):
        """Omitting the field yields False — backward-compatible with pre-#982 payloads."""
        from cai_lib.actions.plan import _run_select_agent
        mock_run.return_value = self._completed(stdout=(
            '{"plan":"ok","confidence":"HIGH",'
            '"confidence_reason":"no divergence"}'
        ))

        out = _run_select_agent(self._issue(), ["p1", "p2"], Path("/tmp/x"))

        self.assertIsNotNone(out)
        _, _, _, requires_review, approvable = out
        self.assertFalse(requires_review)
        self.assertFalse(approvable)

    @patch("cai_lib.actions.plan._run_claude_p")
    def test_parses_approvable_at_medium_true(self, mock_run):
        """cai-select may set approvable_at_medium=true on soft-risk MEDIUM plans (#1008)."""
        from cai_lib.actions.plan import _run_select_agent
        mock_run.return_value = self._completed(stdout=(
            '{"plan":"do X","confidence":"MEDIUM",'
            '"confidence_reason":"only soft risks: additive JSON field",'
            '"approvable_at_medium":true}'
        ))

        out = _run_select_agent(self._issue(), ["p1", "p2"], Path("/tmp/x"))

        self.assertIsNotNone(out)
        _, conf, _, requires_review, approvable = out
        self.assertEqual(conf, Confidence.MEDIUM)
        self.assertFalse(requires_review)
        self.assertTrue(approvable)

    @patch("cai_lib.actions.plan._run_claude_p")
    def test_approvable_at_medium_defaults_to_false(self, mock_run):
        """Omitting the field yields False — backward-compatible with pre-#1008 payloads."""
        from cai_lib.actions.plan import _run_select_agent
        mock_run.return_value = self._completed(stdout=(
            '{"plan":"ok","confidence":"MEDIUM",'
            '"confidence_reason":"scope unclear"}'
        ))

        out = _run_select_agent(self._issue(), ["p1", "p2"], Path("/tmp/x"))

        self.assertIsNotNone(out)
        _, _, _, _, approvable = out
        self.assertFalse(approvable)

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

    @patch("cai_lib.actions.plan.fire_trigger")
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
        self.assertEqual(mock_apply.call_args.kwargs.get("confidence"), Confidence.MEDIUM)

    @patch("cai_lib.actions.plan.fire_trigger")
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
        self.assertEqual(mock_apply.call_args.kwargs.get("confidence"), Confidence.MEDIUM)

    @patch("cai_lib.actions.plan.fire_trigger")
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
        self.assertEqual(mock_apply.call_args.kwargs.get("confidence"), Confidence.LOW)

    @patch("cai_lib.actions.plan.fire_trigger")
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
        self.assertEqual(mock_apply.call_args.kwargs.get("confidence"), Confidence.HIGH)

    @patch("cai_lib.actions.plan.fire_trigger")
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
        self.assertEqual(mock_apply.call_args.kwargs.get("confidence"), Confidence.HIGH)


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


class TestHandlePlanGateRequiresHumanReview(unittest.TestCase):
    """#982 — handle_plan_gate must divert via planned_to_human with a
    bespoke admin-approval message when cai-select set
    ``requires_human_review=true``, independent of the confidence level
    and of the anchor-mitigation marker."""

    def _issue(self, *, confidence, requires_review, plan_text="plan body"):
        return {
            "number": 982,
            "title": "t",
            "body": "",
            "labels": [{"name": "auto-improve:planned"}],
            "_cai_plan_confidence": confidence,
            "_cai_plan_confidence_reason": "plan diverges from refined preference",
            "_cai_plan_text": plan_text,
            "_cai_plan_requires_human_review": requires_review,
        }

    @patch("cai_lib.actions.plan._set_labels")
    @patch("cai_lib.actions.plan._post_issue_comment")
    @patch("cai_lib.actions.plan.fire_trigger")
    @patch("cai_lib.actions.plan.log_run")
    def test_requires_review_high_conf_still_diverts(
        self, _mock_log, mock_fire, mock_post, mock_set_labels
    ):
        from cai_lib.actions.plan import handle_plan_gate
        from cai_lib.config import LABEL_PLAN_NEEDS_REVIEW
        mock_fire.return_value = (True, False)
        mock_set_labels.return_value = True

        rc = handle_plan_gate(self._issue(
            confidence=Confidence.HIGH,
            requires_review=True,
        ))

        self.assertEqual(rc, 0)
        # fire_trigger must be called exactly once with the non-gated
        # planned_to_human transition.
        calls_by_name = [c.args[1] for c in mock_fire.call_args_list]
        self.assertIn("planned_to_human", calls_by_name)
        gated_calls = [
            c for c in mock_fire.call_args_list
            if c.kwargs.get("_confidence_gated")
        ]
        self.assertEqual(gated_calls, [])
        # divert_reason kwarg must carry the admin-approval message.
        fire_call_kwargs = mock_fire.call_args.kwargs
        divert_reason = fire_call_kwargs.get("divert_reason", "")
        self.assertIn(
            "Plan diverges from refined-issue preference",
            divert_reason,
        )
        self.assertIn("admin approval required", divert_reason)
        mock_post.assert_not_called()
        # #1128 — supplementary plan-needs-review label must be applied
        # on top of :human-needed so rescue skips the issue.
        mock_set_labels.assert_called_once()
        set_labels_kwargs = mock_set_labels.call_args.kwargs
        self.assertEqual(
            set_labels_kwargs.get("add"), [LABEL_PLAN_NEEDS_REVIEW],
        )

    @patch("cai_lib.actions.plan._post_issue_comment")
    @patch("cai_lib.actions.plan.fire_trigger")
    @patch("cai_lib.actions.plan.log_run")
    def test_requires_review_false_falls_through_to_confidence_gate(
        self, _mock_log, mock_fire, mock_post
    ):
        from cai_lib.actions.plan import handle_plan_gate
        mock_fire.return_value = (True, False)

        rc = handle_plan_gate(self._issue(
            confidence=Confidence.HIGH,
            requires_review=False,
        ))

        self.assertEqual(rc, 0)
        mock_post.assert_not_called()
        # The confidence-gated call must have been made with the default transition.
        gated_calls = [
            c for c in mock_fire.call_args_list
            if c.kwargs.get("_confidence_gated")
        ]
        self.assertEqual(len(gated_calls), 1)
        self.assertEqual(gated_calls[0].args[1], "planned_to_plan_approved")
        # No non-gated call (human-review divert path must NOT fire).
        non_gated_calls = [
            c for c in mock_fire.call_args_list
            if not c.kwargs.get("_confidence_gated")
        ]
        self.assertEqual(non_gated_calls, [])

    @patch("cai_lib.actions.plan._post_issue_comment")
    @patch("cai_lib.actions.plan.fire_trigger")
    @patch("cai_lib.actions.plan.log_run")
    def test_requires_review_refused_returns_1(
        self, _mock_log, mock_fire, _mock_post
    ):
        from cai_lib.actions.plan import handle_plan_gate
        mock_fire.return_value = (False, False)  # transition refused

        rc = handle_plan_gate(self._issue(
            confidence=Confidence.MEDIUM,
            requires_review=True,
        ))

        self.assertEqual(rc, 1)
        gated_calls = [
            c for c in mock_fire.call_args_list
            if c.kwargs.get("_confidence_gated")
        ]
        self.assertEqual(gated_calls, [])

    @patch("cai_lib.actions.plan._set_labels")
    @patch("cai_lib.actions.plan._post_issue_comment")
    @patch("cai_lib.actions.plan.fire_trigger")
    @patch("cai_lib.actions.plan.log_run")
    def test_requires_review_reparsed_from_body_when_stash_missing(
        self, _mock_log, mock_fire, mock_post, mock_set_labels
    ):
        """When the in-process stash is absent, the flag must be
        re-parsed from the stored plan block in the issue body."""
        from cai_lib.actions.plan import handle_plan_gate
        from cai_lib.config import LABEL_PLAN_NEEDS_REVIEW
        mock_fire.return_value = (True, False)
        mock_set_labels.return_value = True

        body = (
            "<!-- cai-plan-start -->\n"
            "## Selected Implementation Plan\n\n"
            "plan body\n"
            "Confidence: MEDIUM\n"
            "Requires human review: true\n"
            "<!-- cai-plan-end -->"
        )
        issue = {
            "number": 982,
            "title": "t",
            "body": body,
            "labels": [{"name": "auto-improve:planned"}],
        }
        rc = handle_plan_gate(issue)

        self.assertEqual(rc, 0)
        gated_calls = [
            c for c in mock_fire.call_args_list
            if c.kwargs.get("_confidence_gated")
        ]
        self.assertEqual(gated_calls, [])
        calls_by_name = [c.args[1] for c in mock_fire.call_args_list]
        self.assertIn("planned_to_human", calls_by_name)
        fire_call_kwargs = mock_fire.call_args.kwargs
        divert_reason = fire_call_kwargs.get("divert_reason", "")
        self.assertIn("admin approval required", divert_reason)
        mock_post.assert_not_called()
        # #1128 — body-reparse path must also apply the supplementary
        # plan-needs-review label.
        mock_set_labels.assert_called_once()
        set_labels_kwargs = mock_set_labels.call_args.kwargs
        self.assertEqual(
            set_labels_kwargs.get("add"), [LABEL_PLAN_NEEDS_REVIEW],
        )

    @patch("cai_lib.actions.plan._set_labels")
    @patch("cai_lib.actions.plan._post_issue_comment")
    @patch("cai_lib.actions.plan.fire_trigger")
    @patch("cai_lib.actions.plan.log_run")
    def test_requires_review_set_labels_failure_still_returns_0(
        self, _mock_log, mock_fire, _mock_post, mock_set_labels
    ):
        """#1128 — a `_set_labels` failure must NOT flip the return code
        from 0 to 1. The FSM divert has already succeeded; failing to
        stamp the supplementary marker is a logged-but-ignorable error."""
        from cai_lib.actions.plan import handle_plan_gate
        mock_fire.return_value = (True, False)
        mock_set_labels.return_value = False  # label-apply failed

        rc = handle_plan_gate(self._issue(
            confidence=Confidence.MEDIUM,
            requires_review=True,
        ))

        self.assertEqual(rc, 0)
        mock_set_labels.assert_called_once()


class TestParseRequiresHumanReview(unittest.TestCase):
    """#982 — parse_requires_human_review extracts the plan-block marker."""

    def test_true_marker_returns_true(self):
        from cai_lib.fsm import parse_requires_human_review
        body = (
            "## Selected Implementation Plan\n\n"
            "plan\n"
            "Confidence: MEDIUM\n"
            "Requires human review: true\n"
        )
        self.assertTrue(parse_requires_human_review(body))

    def test_false_marker_returns_false(self):
        from cai_lib.fsm import parse_requires_human_review
        body = (
            "Confidence: HIGH\n"
            "Requires human review: false\n"
        )
        self.assertFalse(parse_requires_human_review(body))

    def test_missing_marker_returns_false(self):
        from cai_lib.fsm import parse_requires_human_review
        self.assertFalse(parse_requires_human_review(""))
        self.assertFalse(parse_requires_human_review(None))
        self.assertFalse(parse_requires_human_review(
            "Confidence: HIGH\n"
        ))

    def test_case_insensitive_flag(self):
        from cai_lib.fsm import parse_requires_human_review
        self.assertTrue(parse_requires_human_review(
            "Requires human review: TRUE\n"
        ))
        self.assertFalse(parse_requires_human_review(
            "Requires human review: FALSE\n"
        ))


class TestPlanTargetsOnlyDocsHelper(unittest.TestCase):
    """#989 — module-private docs-only structural helper."""

    def test_canonical_docs_only_plan_matches(self):
        from cai_lib.actions.plan import _plan_targets_only_docs
        plan = (
            "## Plan\n\n"
            "### Summary\nExpand module narratives.\n\n"
            "### Files to change\n"
            "- **`docs/modules/cli.md`**: expand narrative\n"
            "- **`docs/modules/fsm.md`**: expand narrative\n\n"
            "### Detailed steps\n- step 1\n"
        )
        self.assertTrue(_plan_targets_only_docs(plan))

    def test_single_docs_path_matches(self):
        from cai_lib.actions.plan import _plan_targets_only_docs
        plan = (
            "### Files to change\n"
            "- **`docs/modules/cli.md`**: expand narrative\n\n"
            "### Detailed steps\n"
        )
        self.assertTrue(_plan_targets_only_docs(plan))

    def test_missing_files_section_returns_false(self):
        from cai_lib.actions.plan import _plan_targets_only_docs
        plan = (
            "## Plan\n\n### Summary\nExpand.\n\n"
            "### Detailed steps\n- step 1\n"
        )
        self.assertFalse(_plan_targets_only_docs(plan))

    def test_empty_files_section_returns_false(self):
        from cai_lib.actions.plan import _plan_targets_only_docs
        plan = (
            "### Files to change\nsee description above\n\n"
            "### Detailed steps\n- step 1\n"
        )
        self.assertFalse(_plan_targets_only_docs(plan))

    def test_non_doc_path_in_files_section_returns_false(self):
        from cai_lib.actions.plan import _plan_targets_only_docs
        plan = (
            "### Files to change\n"
            "- **`docs/modules/cli.md`**: expand\n"
            "- **`cai_lib/foo.py`**: refactor\n\n"
            "### Detailed steps\n- step 1\n"
        )
        self.assertFalse(_plan_targets_only_docs(plan))

    def test_test_file_in_files_section_returns_false(self):
        from cai_lib.actions.plan import _plan_targets_only_docs
        plan = (
            "### Files to change\n"
            "- **`docs/modules/cli.md`**: expand\n"
            "- **`tests/test_foo.py`**: add coverage\n\n"
            "### Detailed steps\n- step 1\n"
        )
        self.assertFalse(_plan_targets_only_docs(plan))

    def test_section_bounded_by_next_heading(self):
        """Paths mentioned in a later section must not leak into the match."""
        from cai_lib.actions.plan import _plan_targets_only_docs
        plan = (
            "### Files to change\n"
            "- **`docs/modules/cli.md`**: expand\n\n"
            "### Detailed steps\n"
            "Read `cai_lib/foo.py` for context then edit `docs/modules/cli.md`.\n"
        )
        # `cai_lib/foo.py` lives under Detailed steps, not Files-to-change,
        # so the docs-only relaxation must still apply.
        self.assertTrue(_plan_targets_only_docs(plan))

    def test_case_insensitive_header(self):
        from cai_lib.actions.plan import _plan_targets_only_docs
        plan = (
            "### FILES TO CHANGE\n"
            "- **`docs/modules/cli.md`**: expand\n\n"
            "### Detailed steps\n"
        )
        self.assertTrue(_plan_targets_only_docs(plan))

    def test_none_and_empty_return_false(self):
        from cai_lib.actions.plan import _plan_targets_only_docs
        self.assertFalse(_plan_targets_only_docs(None))
        self.assertFalse(_plan_targets_only_docs(""))
        self.assertFalse(_plan_targets_only_docs("plan without any file section"))

    def test_bare_symbol_references_ignored(self):
        """`parse_config` (no slash, no extension) must not count as a path."""
        from cai_lib.actions.plan import _plan_targets_only_docs
        plan = (
            "### Files to change\n"
            "- **`docs/modules/cli.md`**: document `parse_config` and `README.md`\n\n"
            "### Detailed steps\n"
        )
        # `parse_config` has no slash; `README.md` has no directory —
        # both are ignored. Only `docs/modules/cli.md` qualifies.
        self.assertTrue(_plan_targets_only_docs(plan))

    def test_docs_substring_mid_path_rejected(self):
        """`cai_lib/docs_helper.py` must NOT qualify even though it contains 'docs'."""
        from cai_lib.actions.plan import _plan_targets_only_docs
        plan = (
            "### Files to change\n"
            "- **`cai_lib/docs_helper.py`**: refactor\n\n"
            "### Detailed steps\n"
        )
        self.assertFalse(_plan_targets_only_docs(plan))


class TestHandlePlanGateDocsOnly(unittest.TestCase):
    """#989 — handle_plan_gate routes docs-only plans via the
    MEDIUM-threshold sibling transition instead of the HIGH default.

    The detection is purely structural — no in-plan marker phrase
    is required. The planner's Files-to-change declaration is the
    trusted signal.
    """

    _DOCS_ONLY_PLAN = (
        "## Plan\n\n"
        "### Summary\nExpand module narratives.\n\n"
        "### Files to change\n"
        "- **`docs/modules/cli.md`**: expand narrative\n"
        "- **`docs/modules/fsm.md`**: expand narrative\n\n"
        "### Detailed steps\n- step 1\n"
    )

    def _issue(self, *, confidence, plan_text):
        return {
            "number": 989,
            "title": "t",
            "body": "",
            "labels": [{"name": "auto-improve:planned"}],
            "_cai_plan_confidence": confidence,
            "_cai_plan_confidence_reason": "some symbol names unverified",
            "_cai_plan_text": plan_text,
        }

    @patch("cai_lib.actions.plan.fire_trigger")
    @patch("cai_lib.actions.plan.log_run")
    def test_medium_docs_only_uses_docs_only_transition(
        self, _mock_log, mock_apply
    ):
        from cai_lib.actions.plan import handle_plan_gate
        mock_apply.return_value = (True, False)

        rc = handle_plan_gate(self._issue(
            confidence=Confidence.MEDIUM,
            plan_text=self._DOCS_ONLY_PLAN,
        ))

        self.assertEqual(rc, 0)
        args = mock_apply.call_args[0]
        self.assertEqual(args[0], 989)
        self.assertEqual(args[1], "planned_to_plan_approved_docs_only")
        # Reported confidence passes through unchanged — gating is a
        # property of the selected transition, not a confidence upgrade.
        self.assertEqual(mock_apply.call_args.kwargs.get("confidence"), Confidence.MEDIUM)

    @patch("cai_lib.actions.plan.fire_trigger")
    @patch("cai_lib.actions.plan.log_run")
    def test_low_docs_only_still_diverts(
        self, _mock_log, mock_apply
    ):
        from cai_lib.actions.plan import handle_plan_gate
        mock_apply.return_value = (True, True)

        rc = handle_plan_gate(self._issue(
            confidence=Confidence.LOW,
            plan_text=self._DOCS_ONLY_PLAN,
        ))

        self.assertEqual(rc, 0)
        args = mock_apply.call_args[0]
        # Docs-only transition is picked; LOW < MEDIUM so the gate
        # still diverts (required=MEDIUM, reported=LOW).
        self.assertEqual(args[1], "planned_to_plan_approved_docs_only")
        self.assertEqual(mock_apply.call_args.kwargs.get("confidence"), Confidence.LOW)

    @patch("cai_lib.actions.plan.fire_trigger")
    @patch("cai_lib.actions.plan.log_run")
    def test_high_docs_only_uses_docs_only_transition(
        self, _mock_log, mock_apply
    ):
        from cai_lib.actions.plan import handle_plan_gate
        mock_apply.return_value = (True, False)

        rc = handle_plan_gate(self._issue(
            confidence=Confidence.HIGH,
            plan_text=self._DOCS_ONLY_PLAN,
        ))

        self.assertEqual(rc, 0)
        args = mock_apply.call_args[0]
        # HIGH-confidence docs-only plans route through the docs-only
        # transition too; HIGH >= MEDIUM so the gate passes.
        self.assertEqual(args[1], "planned_to_plan_approved_docs_only")
        self.assertEqual(mock_apply.call_args.kwargs.get("confidence"), Confidence.HIGH)

    @patch("cai_lib.actions.plan.fire_trigger")
    @patch("cai_lib.actions.plan.log_run")
    def test_docs_only_takes_precedence_over_anchor_mitigation(
        self, _mock_log, mock_apply
    ):
        from cai_lib.actions.plan import handle_plan_gate
        mock_apply.return_value = (True, False)

        # Plan targets only docs/ AND also carries the anchor-mitigation
        # marker. Docs-only must win — its precondition is stronger.
        plan_both = (
            "> **Anchor-based edits:** locate edits by anchor text, "
            "not by line number.\n\n"
            + self._DOCS_ONLY_PLAN
        )
        rc = handle_plan_gate(self._issue(
            confidence=Confidence.MEDIUM,
            plan_text=plan_both,
        ))

        self.assertEqual(rc, 0)
        args = mock_apply.call_args[0]
        self.assertEqual(args[1], "planned_to_plan_approved_docs_only")

    @patch("cai_lib.actions.plan.fire_trigger")
    @patch("cai_lib.actions.plan.log_run")
    def test_medium_non_docs_falls_through_to_default(
        self, _mock_log, mock_apply
    ):
        from cai_lib.actions.plan import handle_plan_gate
        # Default HIGH transition diverts MEDIUM → (True, True).
        mock_apply.return_value = (True, True)

        plan_source_change = (
            "## Plan\n\n### Summary\nrefactor\n\n"
            "### Files to change\n- **`cai_lib/foo.py`**: refactor\n\n"
            "### Detailed steps\n- step 1\n"
        )
        rc = handle_plan_gate(self._issue(
            confidence=Confidence.MEDIUM,
            plan_text=plan_source_change,
        ))

        self.assertEqual(rc, 0)
        args = mock_apply.call_args[0]
        self.assertEqual(args[1], "planned_to_plan_approved")
        self.assertEqual(mock_apply.call_args.kwargs.get("confidence"), Confidence.MEDIUM)

    @patch("cai_lib.actions.plan.fire_trigger")
    @patch("cai_lib.actions.plan.log_run")
    def test_mixed_docs_and_source_uses_default(
        self, _mock_log, mock_apply
    ):
        from cai_lib.actions.plan import handle_plan_gate
        mock_apply.return_value = (True, True)

        # One docs path + one source path → NOT docs-only; must route
        # through the default HIGH-threshold transition and divert.
        mixed = (
            "### Files to change\n"
            "- **`docs/modules/cli.md`**: expand\n"
            "- **`cai_lib/foo.py`**: refactor\n\n"
            "### Detailed steps\n- step 1\n"
        )
        rc = handle_plan_gate(self._issue(
            confidence=Confidence.MEDIUM,
            plan_text=mixed,
        ))

        self.assertEqual(rc, 0)
        args = mock_apply.call_args[0]
        self.assertEqual(args[1], "planned_to_plan_approved")


class TestHandlePlanGateApprovableAtMedium(unittest.TestCase):
    """#1008 — handle_plan_gate routes cai-select-flagged MEDIUM plans
    via the planned_to_plan_approved_approvable MEDIUM-threshold sibling
    transition when approvable_at_medium is set."""

    _PLAIN_PLAN = (
        "## Plan\n\n### Summary\nrefactor a helper.\n\n"
        "### Files to change\n- **`cai_lib/foo.py`**: tighten typing\n\n"
        "### Detailed steps\n- step 1\n"
    )

    def _issue(self, *, confidence, approvable, plan_text=None):
        return {
            "number": 1008,
            "title": "t",
            "body": "",
            "labels": [{"name": "auto-improve:planned"}],
            "_cai_plan_confidence": confidence,
            "_cai_plan_confidence_reason": "only additive soft risks",
            "_cai_plan_text": plan_text if plan_text is not None else self._PLAIN_PLAN,
            "_cai_plan_approvable_at_medium": approvable,
        }

    @patch("cai_lib.actions.plan.fire_trigger")
    @patch("cai_lib.actions.plan.log_run")
    def test_medium_approvable_uses_approvable_transition(
        self, _mock_log, mock_apply
    ):
        from cai_lib.actions.plan import handle_plan_gate
        mock_apply.return_value = (True, False)

        rc = handle_plan_gate(self._issue(
            confidence=Confidence.MEDIUM,
            approvable=True,
        ))

        self.assertEqual(rc, 0)
        args = mock_apply.call_args[0]
        self.assertEqual(args[0], 1008)
        self.assertEqual(args[1], "planned_to_plan_approved_approvable")
        # Reported confidence passes through unchanged — gating is a
        # property of the selected transition, not a confidence upgrade.
        self.assertEqual(mock_apply.call_args.kwargs.get("confidence"), Confidence.MEDIUM)

    @patch("cai_lib.actions.plan.fire_trigger")
    @patch("cai_lib.actions.plan.log_run")
    def test_low_approvable_still_diverts(
        self, _mock_log, mock_apply
    ):
        from cai_lib.actions.plan import handle_plan_gate
        mock_apply.return_value = (True, True)

        rc = handle_plan_gate(self._issue(
            confidence=Confidence.LOW,
            approvable=True,
        ))

        self.assertEqual(rc, 0)
        args = mock_apply.call_args[0]
        # Flag picks the approvable transition; LOW < MEDIUM so the
        # gate still diverts (required=MEDIUM, reported=LOW).
        self.assertEqual(args[1], "planned_to_plan_approved_approvable")
        self.assertEqual(mock_apply.call_args.kwargs.get("confidence"), Confidence.LOW)

    @patch("cai_lib.actions.plan.fire_trigger")
    @patch("cai_lib.actions.plan.log_run")
    def test_medium_not_approvable_falls_through_to_default(
        self, _mock_log, mock_apply
    ):
        from cai_lib.actions.plan import handle_plan_gate
        # Default HIGH transition diverts MEDIUM → (True, True).
        mock_apply.return_value = (True, True)

        rc = handle_plan_gate(self._issue(
            confidence=Confidence.MEDIUM,
            approvable=False,
        ))

        self.assertEqual(rc, 0)
        args = mock_apply.call_args[0]
        self.assertEqual(args[1], "planned_to_plan_approved")
        self.assertEqual(mock_apply.call_args.kwargs.get("confidence"), Confidence.MEDIUM)

    @patch("cai_lib.actions.plan.fire_trigger")
    @patch("cai_lib.actions.plan.log_run")
    def test_docs_only_takes_precedence_over_approvable(
        self, _mock_log, mock_apply
    ):
        from cai_lib.actions.plan import handle_plan_gate
        mock_apply.return_value = (True, False)

        # Plan targets only docs/ AND approvable_at_medium is set.
        # Docs-only must win — its precondition is strictly stronger.
        docs_plan = (
            "### Files to change\n"
            "- **`docs/modules/cli.md`**: expand\n\n"
            "### Detailed steps\n- step 1\n"
        )
        rc = handle_plan_gate(self._issue(
            confidence=Confidence.MEDIUM,
            approvable=True,
            plan_text=docs_plan,
        ))

        self.assertEqual(rc, 0)
        args = mock_apply.call_args[0]
        self.assertEqual(args[1], "planned_to_plan_approved_docs_only")

    @patch("cai_lib.actions.plan.fire_trigger")
    @patch("cai_lib.actions.plan.log_run")
    def test_anchor_mitigation_takes_precedence_over_approvable(
        self, _mock_log, mock_apply
    ):
        from cai_lib.actions.plan import handle_plan_gate
        mock_apply.return_value = (True, False)

        anchor_plan = (
            "> **Anchor-based edits:** locate edits by anchor text, "
            "not by line number.\n\n"
            + self._PLAIN_PLAN
        )
        rc = handle_plan_gate(self._issue(
            confidence=Confidence.MEDIUM,
            approvable=True,
            plan_text=anchor_plan,
        ))

        self.assertEqual(rc, 0)
        args = mock_apply.call_args[0]
        self.assertEqual(args[1], "planned_to_plan_approved_mitigated")

    @patch("cai_lib.actions.plan.fire_trigger")
    @patch("cai_lib.actions.plan.log_run")
    def test_approvable_reparsed_from_body_when_stash_missing(
        self, _mock_log, mock_apply
    ):
        """When the in-process stash is absent, the flag must be
        re-parsed from the stored plan block in the issue body."""
        from cai_lib.actions.plan import handle_plan_gate
        mock_apply.return_value = (True, False)

        body = (
            "<!-- cai-plan-start -->\n"
            "## Selected Implementation Plan\n\n"
            "## Plan\n### Files to change\n- **`cai_lib/foo.py`**: x\n\n"
            "### Detailed steps\n- step 1\n"
            "Confidence: MEDIUM\n"
            "Approvable at medium: true\n"
            "<!-- cai-plan-end -->"
        )
        issue = {
            "number": 1008,
            "title": "t",
            "body": body,
            "labels": [{"name": "auto-improve:planned"}],
        }
        rc = handle_plan_gate(issue)

        self.assertEqual(rc, 0)
        args = mock_apply.call_args[0]
        self.assertEqual(args[1], "planned_to_plan_approved_approvable")
        self.assertEqual(mock_apply.call_args.kwargs.get("confidence"), Confidence.MEDIUM)


class TestParseApprovableAtMedium(unittest.TestCase):
    """#1008 — parse_approvable_at_medium extracts the plan-block marker."""

    def test_true_marker_returns_true(self):
        from cai_lib.fsm import parse_approvable_at_medium
        body = (
            "## Selected Implementation Plan\n\n"
            "plan\n"
            "Confidence: MEDIUM\n"
            "Approvable at medium: true\n"
        )
        self.assertTrue(parse_approvable_at_medium(body))

    def test_false_marker_returns_false(self):
        from cai_lib.fsm import parse_approvable_at_medium
        body = (
            "Confidence: MEDIUM\n"
            "Approvable at medium: false\n"
        )
        self.assertFalse(parse_approvable_at_medium(body))

    def test_missing_marker_returns_false(self):
        from cai_lib.fsm import parse_approvable_at_medium
        self.assertFalse(parse_approvable_at_medium(""))
        self.assertFalse(parse_approvable_at_medium(None))
        self.assertFalse(parse_approvable_at_medium(
            "Confidence: MEDIUM\n"
        ))

    def test_case_insensitive_flag(self):
        from cai_lib.fsm import parse_approvable_at_medium
        self.assertTrue(parse_approvable_at_medium(
            "Approvable at medium: TRUE\n"
        ))
        self.assertFalse(parse_approvable_at_medium(
            "Approvable at medium: FALSE\n"
        ))


class TestHandlePlanGateAutoFlaggedScaleComplexity(unittest.TestCase):
    """#1131 — handle_plan_gate auto-flags plans for human review at
    LOW confidence when the plan targets >= 15 files or a prior
    divert MARKER comment cites scale/complexity. Fires the same
    planned_to_human + LABEL_PLAN_NEEDS_REVIEW divert as the #982
    requires_human_review=true path, using a bespoke reason."""

    def _issue(self, *, body="", comments=None, confidence=Confidence.LOW):
        return {
            "number": 1131,
            "title": "t",
            "body": body,
            "labels": [{"name": "auto-improve:planned"}],
            "comments": comments or [],
            "_cai_plan_confidence": confidence,
            "_cai_plan_confidence_reason": "",
            "_cai_plan_text": body,
            "_cai_plan_requires_human_review": False,
            "_cai_plan_approvable_at_medium": False,
        }

    @patch("cai_lib.actions.plan._set_labels")
    @patch("cai_lib.actions.plan.fire_trigger")
    @patch("cai_lib.actions.plan.log_run")
    def test_large_scope_low_confidence_diverts(
        self, _mock_log, mock_fire, mock_set_labels
    ):
        from cai_lib.actions.plan import handle_plan_gate
        from cai_lib.config import LABEL_PLAN_NEEDS_REVIEW
        mock_fire.return_value = (True, False)
        mock_set_labels.return_value = True
        body = "### Files to change\n" + "\n".join(
            f"- `pkg/file{i}.py`: change it" for i in range(16)
        ) + "\n"
        rc = handle_plan_gate(self._issue(body=body))
        self.assertEqual(rc, 0)
        call_args = mock_fire.call_args
        self.assertEqual(call_args.args[1], "planned_to_human")
        divert_reason = call_args.kwargs.get("divert_reason", "")
        self.assertIn(
            "Auto-flagged scale/complexity checkpoint (#1131)",
            divert_reason,
        )
        mock_set_labels.assert_called_once()
        self.assertEqual(
            mock_set_labels.call_args.kwargs.get("add"),
            [LABEL_PLAN_NEEDS_REVIEW],
        )

    @patch("cai_lib.actions.plan._set_labels")
    @patch("cai_lib.actions.plan.fire_trigger")
    @patch("cai_lib.actions.plan.log_run")
    def test_prior_divert_scale_phrase_low_confidence_diverts(
        self, _mock_log, mock_fire, mock_set_labels
    ):
        from cai_lib.actions.plan import handle_plan_gate
        mock_fire.return_value = (True, False)
        mock_set_labels.return_value = True
        comments = [{
            "body": (
                "**\U0001f64b Human attention needed**\n"
                "the scale alone warrants admin review"
            ),
        }]
        rc = handle_plan_gate(self._issue(comments=comments))
        self.assertEqual(rc, 0)
        divert_reason = mock_fire.call_args.kwargs.get("divert_reason", "")
        self.assertIn("prior divert", divert_reason)
        self.assertIn("scale or complexity", divert_reason)

    @patch("cai_lib.actions.plan._set_labels")
    @patch("cai_lib.actions.plan.fire_trigger")
    @patch("cai_lib.actions.plan.log_run")
    def test_small_scope_low_confidence_falls_through(
        self, _mock_log, mock_fire, mock_set_labels
    ):
        from cai_lib.actions.plan import handle_plan_gate
        mock_fire.return_value = (True, True)
        body = "### Files to change\n- `pkg/one.py`: change it\n"
        rc = handle_plan_gate(self._issue(body=body))
        self.assertEqual(rc, 0)
        mock_set_labels.assert_not_called()

    @patch("cai_lib.actions.plan._set_labels")
    @patch("cai_lib.actions.plan.fire_trigger")
    @patch("cai_lib.actions.plan.log_run")
    def test_large_scope_high_confidence_does_not_trigger(
        self, _mock_log, mock_fire, mock_set_labels
    ):
        from cai_lib.actions.plan import handle_plan_gate
        mock_fire.return_value = (True, False)
        body = "### Files to change\n" + "\n".join(
            f"- `pkg/file{i}.py`: change" for i in range(20)
        ) + "\n"
        issue = self._issue(body=body, confidence=Confidence.HIGH)
        rc = handle_plan_gate(issue)
        self.assertEqual(rc, 0)
        mock_set_labels.assert_not_called()


class TestAutoFlaggedScaleComplexityHelpers(unittest.TestCase):
    """#1131 — helper functions backing the handle_plan_gate auto-flag."""

    def test_count_files_to_change_counts_unique_paths(self):
        from cai_lib.actions.plan import _count_files_to_change
        body = (
            "### Files to change\n"
            "- `pkg/a.py`: change\n"
            "- `pkg/b.py`: change\n"
            "- `pkg/a.py`: again (duplicate)\n"
            "### Other\n"
            "- `pkg/c.py`: outside the section\n"
        )
        self.assertEqual(_count_files_to_change(body), 2)

    def test_count_files_to_change_empty(self):
        from cai_lib.actions.plan import _count_files_to_change
        self.assertEqual(_count_files_to_change(""), 0)
        self.assertEqual(_count_files_to_change(None), 0)

    def test_count_files_to_change_no_section(self):
        from cai_lib.actions.plan import _count_files_to_change
        self.assertEqual(
            _count_files_to_change("no section here, just prose"), 0
        )

    def test_prior_divert_cites_scale_complexity_matches_marker(self):
        from cai_lib.actions.plan import _prior_divert_cites_scale_complexity
        comments = [{"body": (
            "**\U0001f64b Human attention needed**\n"
            "the scale alone warrants admin review"
        )}]
        self.assertTrue(_prior_divert_cites_scale_complexity(comments))

    def test_prior_divert_requires_marker_presence(self):
        from cai_lib.actions.plan import _prior_divert_cites_scale_complexity
        comments = [{"body": "the scale alone warrants admin review"}]
        self.assertFalse(_prior_divert_cites_scale_complexity(comments))

    def test_prior_divert_requires_scale_phrase(self):
        from cai_lib.actions.plan import _prior_divert_cites_scale_complexity
        comments = [{"body": (
            "**\U0001f64b Human attention needed**\n"
            "Automation paused because the confidence gate was not met."
        )}]
        self.assertFalse(_prior_divert_cites_scale_complexity(comments))

    def test_prior_divert_empty(self):
        from cai_lib.actions.plan import _prior_divert_cites_scale_complexity
        self.assertFalse(_prior_divert_cites_scale_complexity([]))
        self.assertFalse(_prior_divert_cites_scale_complexity(None))


class TestCountEditStepsHelper(unittest.TestCase):
    """#1139 — structural counter for `#### Step N — Edit/Write` headers."""

    def test_empty_and_none_return_zero(self):
        from cai_lib.actions.plan import _count_edit_steps
        self.assertEqual(_count_edit_steps(""), 0)
        self.assertEqual(_count_edit_steps(None), 0)

    def test_counts_edit_and_write_headers(self):
        from cai_lib.actions.plan import _count_edit_steps
        plan = (
            "## Plan\n\n"
            "### Detailed steps\n\n"
            "#### Step 1 \u2014 Edit `foo.py`\n"
            "body\n\n"
            "#### Step 2 \u2014 Write `bar.py`\n"
            "body\n\n"
            "#### Step 3 \u2014 Edit `baz.py`\n"
            "body\n"
        )
        self.assertEqual(_count_edit_steps(plan), 3)

    def test_ignores_non_edit_step_verbs(self):
        from cai_lib.actions.plan import _count_edit_steps
        plan = (
            "#### Step 1 \u2014 Read `foo.py`\n"
            "#### Step 2 \u2014 Verify behaviour\n"
            "#### Step 3 \u2014 Edit `bar.py`\n"
        )
        self.assertEqual(_count_edit_steps(plan), 1)

    def test_accepts_em_dash_en_dash_and_hyphen(self):
        from cai_lib.actions.plan import _count_edit_steps
        plan = (
            "#### Step 1 \u2014 Edit `a.py`\n"
            "#### Step 2 \u2013 Edit `b.py`\n"
            "#### Step 3 - Edit `c.py`\n"
        )
        self.assertEqual(_count_edit_steps(plan), 3)


class TestPlanIsLargeMechanicalRefactorHelper(unittest.TestCase):
    """#1139 — both-threshold gate for the large-mechanical-refactor
    detection."""

    def _plan(self, n_files, n_steps):
        files_section = "### Files to change\n" + "".join(
            f"- **`pkg/file_{i}.py`**: change\n" for i in range(n_files)
        )
        steps_section = "### Detailed steps\n" + "".join(
            f"#### Step {i + 1} \u2014 Edit "
            f"`pkg/file_{i % max(n_files, 1)}.py`\n\nbody\n\n"
            for i in range(n_steps)
        )
        return f"## Plan\n\n{files_section}\n{steps_section}"

    def test_fires_when_both_thresholds_met(self):
        from cai_lib.actions.plan import _plan_is_large_mechanical_refactor
        self.assertTrue(_plan_is_large_mechanical_refactor(
            self._plan(n_files=8, n_steps=50)
        ))

    def test_fires_on_large_excess(self):
        from cai_lib.actions.plan import _plan_is_large_mechanical_refactor
        self.assertTrue(_plan_is_large_mechanical_refactor(
            self._plan(n_files=20, n_steps=80)
        ))

    def test_below_file_threshold_returns_false(self):
        from cai_lib.actions.plan import _plan_is_large_mechanical_refactor
        self.assertFalse(_plan_is_large_mechanical_refactor(
            self._plan(n_files=7, n_steps=60)
        ))

    def test_below_step_threshold_returns_false(self):
        from cai_lib.actions.plan import _plan_is_large_mechanical_refactor
        self.assertFalse(_plan_is_large_mechanical_refactor(
            self._plan(n_files=12, n_steps=49)
        ))

    def test_empty_and_none_return_false(self):
        from cai_lib.actions.plan import _plan_is_large_mechanical_refactor
        self.assertFalse(_plan_is_large_mechanical_refactor(None))
        self.assertFalse(_plan_is_large_mechanical_refactor(""))
        self.assertFalse(_plan_is_large_mechanical_refactor(
            "plan without sections"
        ))


class TestPlanQualifiesForExtendedRetries(unittest.TestCase):
    """#1151 — both-threshold gate for the medium-scale-refactor
    detection that drives the LABEL_EXTENDED_RETRIES stamping."""

    def _plan(self, n_files, n_steps):
        files_section = "### Files to change\n" + "".join(
            f"- **`pkg/file_{i}.py`**: change\n" for i in range(n_files)
        )
        steps_section = "### Detailed steps\n" + "".join(
            f"#### Step {i + 1} — Edit "
            f"`pkg/file_{i % max(n_files, 1)}.py`\n\nbody\n\n"
            for i in range(n_steps)
        )
        return f"## Plan\n\n{files_section}\n{steps_section}"

    def test_fires_when_both_thresholds_met(self):
        from cai_lib.actions.plan import _plan_qualifies_for_extended_retries
        self.assertTrue(_plan_qualifies_for_extended_retries(
            self._plan(n_files=5, n_steps=40)
        ))

    def test_fires_on_excess(self):
        from cai_lib.actions.plan import _plan_qualifies_for_extended_retries
        self.assertTrue(_plan_qualifies_for_extended_retries(
            self._plan(n_files=7, n_steps=49)
        ))

    def test_below_file_threshold_returns_false(self):
        from cai_lib.actions.plan import _plan_qualifies_for_extended_retries
        self.assertFalse(_plan_qualifies_for_extended_retries(
            self._plan(n_files=4, n_steps=45)
        ))

    def test_below_step_threshold_returns_false(self):
        from cai_lib.actions.plan import _plan_qualifies_for_extended_retries
        self.assertFalse(_plan_qualifies_for_extended_retries(
            self._plan(n_files=6, n_steps=39)
        ))

    def test_empty_and_none_return_false(self):
        from cai_lib.actions.plan import _plan_qualifies_for_extended_retries
        self.assertFalse(_plan_qualifies_for_extended_retries(None))
        self.assertFalse(_plan_qualifies_for_extended_retries(""))
        self.assertFalse(_plan_qualifies_for_extended_retries(
            "plan without sections"
        ))

    def test_large_refactor_also_qualifies_for_extended(self):
        """A plan meeting BOTH the #1139 and #1151 thresholds must
        qualify for extended retries — stacking is intentional (the
        Opus-tier run silently no-ops the label lookup)."""
        from cai_lib.actions.plan import (
            _plan_qualifies_for_extended_retries,
            _plan_is_large_mechanical_refactor,
        )
        plan = self._plan(n_files=10, n_steps=60)
        self.assertTrue(_plan_is_large_mechanical_refactor(plan))
        self.assertTrue(_plan_qualifies_for_extended_retries(plan))


class TestHandlePlanGateAppliesExtendedRetriesLabel(unittest.TestCase):
    """#1151 — handle_plan_gate stamps LABEL_EXTENDED_RETRIES directly
    on a successfully-approved medium-scale plan, mirroring the #1139
    Opus-label stamping pattern."""

    def _medium_plan(self):
        files = "### Files to change\n" + "".join(
            f"- **`pkg/file_{i}.py`**: change\n" for i in range(5)
        )
        steps = "### Detailed steps\n" + "".join(
            f"#### Step {i + 1} — Edit "
            f"`pkg/file_{i % 5}.py`\n\nbody\n\n"
            for i in range(40)
        )
        return f"## Plan\n\n{files}\n{steps}"

    def _small_plan(self):
        return (
            "## Plan\n\n"
            "### Files to change\n"
            "- **`pkg/a.py`**: tweak\n\n"
            "### Detailed steps\n"
            "#### Step 1 — Edit `pkg/a.py`\n\nbody\n"
        )

    def _issue(self, *, plan_text, labels=None):
        return {
            "number": 1151,
            "title": "t",
            "body": "",
            "labels": [
                {"name": n} for n in (labels or ["auto-improve:planned"])
            ],
            "_cai_plan_confidence": Confidence.HIGH,
            "_cai_plan_confidence_reason": "",
            "_cai_plan_text": plan_text,
            "_cai_plan_requires_human_review": False,
            "_cai_plan_approvable_at_medium": False,
        }

    @patch("cai_lib.actions.plan._post_issue_comment", return_value=True)
    @patch("cai_lib.actions.plan._set_labels")
    @patch("cai_lib.actions.plan.fire_trigger")
    @patch("cai_lib.actions.plan.log_run")
    def test_medium_plan_applies_extended_retries_label(
        self, _mock_log, mock_fire, mock_set_labels, mock_post,
    ):
        from cai_lib.actions.plan import handle_plan_gate
        from cai_lib.config import LABEL_EXTENDED_RETRIES
        mock_fire.return_value = (True, False)  # approved, not diverted
        mock_set_labels.return_value = True

        rc = handle_plan_gate(self._issue(plan_text=self._medium_plan()))

        self.assertEqual(rc, 0)
        self.assertEqual(
            mock_fire.call_args[0][1], "planned_to_plan_approved",
        )
        extended_calls = [
            c for c in mock_set_labels.call_args_list
            if LABEL_EXTENDED_RETRIES in (c.kwargs.get("add") or [])
        ]
        self.assertEqual(len(extended_calls), 1)
        posted_bodies = [c.args[1] for c in mock_post.call_args_list]
        self.assertTrue(any(
            "Extended Sonnet-tier retries budget" in b for b in posted_bodies
        ))

    @patch("cai_lib.actions.plan._post_issue_comment", return_value=True)
    @patch("cai_lib.actions.plan._set_labels")
    @patch("cai_lib.actions.plan.fire_trigger")
    @patch("cai_lib.actions.plan.log_run")
    def test_small_plan_does_not_apply_extended_retries_label(
        self, _mock_log, mock_fire, mock_set_labels, mock_post,
    ):
        from cai_lib.actions.plan import handle_plan_gate
        from cai_lib.config import LABEL_EXTENDED_RETRIES
        mock_fire.return_value = (True, False)
        mock_set_labels.return_value = True

        rc = handle_plan_gate(self._issue(plan_text=self._small_plan()))

        self.assertEqual(rc, 0)
        extended_calls = [
            c for c in mock_set_labels.call_args_list
            if LABEL_EXTENDED_RETRIES in (c.kwargs.get("add") or [])
        ]
        self.assertEqual(len(extended_calls), 0)

    @patch("cai_lib.actions.plan._post_issue_comment", return_value=True)
    @patch("cai_lib.actions.plan._set_labels")
    @patch("cai_lib.actions.plan.fire_trigger")
    @patch("cai_lib.actions.plan.log_run")
    def test_diverted_plan_does_not_apply_extended_retries_label(
        self, _mock_log, mock_fire, mock_set_labels, mock_post,
    ):
        """Divert paths (gate rejection, requires_human_review,
        #1131 scale auto-flag) must NOT stamp the label — admin
        should pick the retry budget when resuming."""
        from cai_lib.actions.plan import handle_plan_gate
        from cai_lib.config import LABEL_EXTENDED_RETRIES
        mock_fire.return_value = (True, True)  # applied, but diverted
        mock_set_labels.return_value = True

        rc = handle_plan_gate(self._issue(plan_text=self._medium_plan()))

        self.assertEqual(rc, 0)
        extended_calls = [
            c for c in mock_set_labels.call_args_list
            if LABEL_EXTENDED_RETRIES in (c.kwargs.get("add") or [])
        ]
        self.assertEqual(len(extended_calls), 0)

    @patch("cai_lib.actions.plan._post_issue_comment", return_value=True)
    @patch("cai_lib.actions.plan._set_labels")
    @patch("cai_lib.actions.plan.fire_trigger")
    @patch("cai_lib.actions.plan.log_run")
    def test_already_labelled_issue_does_not_double_stamp(
        self, _mock_log, mock_fire, mock_set_labels, mock_post,
    ):
        from cai_lib.actions.plan import handle_plan_gate
        from cai_lib.config import LABEL_EXTENDED_RETRIES
        mock_fire.return_value = (True, False)
        mock_set_labels.return_value = True

        rc = handle_plan_gate(self._issue(
            plan_text=self._medium_plan(),
            labels=["auto-improve:planned", LABEL_EXTENDED_RETRIES],
        ))

        self.assertEqual(rc, 0)
        extended_calls = [
            c for c in mock_set_labels.call_args_list
            if LABEL_EXTENDED_RETRIES in (c.kwargs.get("add") or [])
        ]
        self.assertEqual(len(extended_calls), 0)


class TestHandlePlanGateAppliesOpusLabel(unittest.TestCase):
    """#1139 — handle_plan_gate applies LABEL_OPUS_ATTEMPTED directly on
    a successfully-approved large-mechanical-refactor plan, so
    handle_implement reads opus_escalation=True on the next tick."""

    def _large_plan(self):
        files = "### Files to change\n" + "".join(
            f"- **`pkg/file_{i}.py`**: change\n" for i in range(10)
        )
        steps = "### Detailed steps\n" + "".join(
            f"#### Step {i + 1} \u2014 Edit "
            f"`pkg/file_{i % 10}.py`\n\nbody\n\n"
            for i in range(60)
        )
        return f"## Plan\n\n{files}\n{steps}"

    def _small_plan(self):
        return (
            "## Plan\n\n"
            "### Files to change\n"
            "- **`pkg/a.py`**: tweak\n\n"
            "### Detailed steps\n"
            "#### Step 1 \u2014 Edit `pkg/a.py`\n\nbody\n"
        )

    def _issue(self, *, plan_text, labels=None):
        return {
            "number": 1139,
            "title": "t",
            "body": "",
            "labels": [
                {"name": n} for n in (labels or ["auto-improve:planned"])
            ],
            "_cai_plan_confidence": Confidence.HIGH,
            "_cai_plan_confidence_reason": "",
            "_cai_plan_text": plan_text,
            "_cai_plan_requires_human_review": False,
            "_cai_plan_approvable_at_medium": False,
        }

    @patch("cai_lib.actions.plan._post_issue_comment", return_value=True)
    @patch("cai_lib.actions.plan._set_labels")
    @patch("cai_lib.actions.plan.fire_trigger")
    @patch("cai_lib.actions.plan.log_run")
    def test_large_plan_applies_opus_label_on_approval(
        self, _mock_log, mock_fire, mock_set_labels, mock_post,
    ):
        from cai_lib.actions.plan import handle_plan_gate
        from cai_lib.config import LABEL_OPUS_ATTEMPTED
        mock_fire.return_value = (True, False)  # approved, not diverted
        mock_set_labels.return_value = True

        rc = handle_plan_gate(self._issue(plan_text=self._large_plan()))

        self.assertEqual(rc, 0)
        # The gate transition fired with planned_to_plan_approved.
        self.assertEqual(
            mock_fire.call_args[0][1], "planned_to_plan_approved",
        )
        # The post-approval label application ran.
        opus_calls = [
            c for c in mock_set_labels.call_args_list
            if LABEL_OPUS_ATTEMPTED in (c.kwargs.get("add") or [])
        ]
        self.assertEqual(len(opus_calls), 1)
        # An informational comment was posted for the Opus label.
        # (The extended-retries label also posts a comment on this
        # plan since 10/60 meets both thresholds — #1151 stacking is
        # intentional, see TestHandlePlanGateAppliesExtendedRetriesLabel.)
        opus_posts = [
            c for c in mock_post.call_args_list
            if "Pre-emptive Opus-tier escalation" in c.args[1]
        ]
        self.assertEqual(len(opus_posts), 1)

    @patch("cai_lib.actions.plan._post_issue_comment", return_value=True)
    @patch("cai_lib.actions.plan._set_labels")
    @patch("cai_lib.actions.plan.fire_trigger")
    @patch("cai_lib.actions.plan.log_run")
    def test_small_plan_does_not_apply_opus_label(
        self, _mock_log, mock_fire, mock_set_labels, mock_post,
    ):
        from cai_lib.actions.plan import handle_plan_gate
        from cai_lib.config import LABEL_OPUS_ATTEMPTED
        mock_fire.return_value = (True, False)
        mock_set_labels.return_value = True

        rc = handle_plan_gate(self._issue(plan_text=self._small_plan()))

        self.assertEqual(rc, 0)
        opus_calls = [
            c for c in mock_set_labels.call_args_list
            if LABEL_OPUS_ATTEMPTED in (c.kwargs.get("add") or [])
        ]
        self.assertEqual(opus_calls, [])
        mock_post.assert_not_called()

    @patch("cai_lib.actions.plan._post_issue_comment", return_value=True)
    @patch("cai_lib.actions.plan._set_labels")
    @patch("cai_lib.actions.plan.fire_trigger")
    @patch("cai_lib.actions.plan.log_run")
    def test_diverted_large_plan_does_not_apply_opus_label(
        self, _mock_log, mock_fire, mock_set_labels, mock_post,
    ):
        """When the confidence gate diverts to :human-needed we MUST
        NOT stamp LABEL_OPUS_ATTEMPTED — the admin should choose the
        tier when resuming."""
        from cai_lib.actions.plan import handle_plan_gate
        from cai_lib.config import LABEL_OPUS_ATTEMPTED
        # approved-call succeeds but diverted=True (below-threshold).
        mock_fire.return_value = (True, True)
        mock_set_labels.return_value = True

        issue = self._issue(plan_text=self._large_plan())
        issue["_cai_plan_confidence"] = Confidence.LOW
        rc = handle_plan_gate(issue)

        self.assertEqual(rc, 0)
        opus_calls = [
            c for c in mock_set_labels.call_args_list
            if LABEL_OPUS_ATTEMPTED in (c.kwargs.get("add") or [])
        ]
        self.assertEqual(opus_calls, [])
        mock_post.assert_not_called()

    @patch("cai_lib.actions.plan._post_issue_comment", return_value=True)
    @patch("cai_lib.actions.plan._set_labels")
    @patch("cai_lib.actions.plan.fire_trigger")
    @patch("cai_lib.actions.plan.log_run")
    def test_existing_opus_label_short_circuits(
        self, _mock_log, mock_fire, mock_set_labels, mock_post,
    ):
        """A plan that already carries LABEL_OPUS_ATTEMPTED (e.g. this
        is a re-plan after a rescue escalation) must NOT spam a second
        Opus-specific comment or a redundant _set_labels call."""
        from cai_lib.actions.plan import handle_plan_gate
        from cai_lib.config import (
            LABEL_OPUS_ATTEMPTED,
            LABEL_EXTENDED_RETRIES,
        )
        mock_fire.return_value = (True, False)
        mock_set_labels.return_value = True

        # Pre-stamp both labels so both short-circuit guards fire.
        # (A 10/60 plan meets both the #1139 and #1151 thresholds.)
        rc = handle_plan_gate(self._issue(
            plan_text=self._large_plan(),
            labels=[
                "auto-improve:planned",
                LABEL_OPUS_ATTEMPTED,
                LABEL_EXTENDED_RETRIES,
            ],
        ))

        self.assertEqual(rc, 0)
        opus_calls = [
            c for c in mock_set_labels.call_args_list
            if LABEL_OPUS_ATTEMPTED in (c.kwargs.get("add") or [])
        ]
        self.assertEqual(opus_calls, [])
        mock_post.assert_not_called()

    @patch("cai_lib.actions.plan._post_issue_comment", return_value=True)
    @patch("cai_lib.actions.plan._set_labels")
    @patch("cai_lib.actions.plan.fire_trigger")
    @patch("cai_lib.actions.plan.log_run")
    def test_set_labels_failure_skips_comment_and_continues(
        self, _mock_log, mock_fire, mock_set_labels, mock_post,
    ):
        """When _set_labels fails we must NOT post the comment (so the
        admin doesn't see 'pre-empting to Opus' on an issue still
        queued for Sonnet) and must still return 0 — the gate
        transition already succeeded."""
        from cai_lib.actions.plan import handle_plan_gate
        mock_fire.return_value = (True, False)
        mock_set_labels.return_value = False

        rc = handle_plan_gate(self._issue(plan_text=self._large_plan()))

        self.assertEqual(rc, 0)
        mock_post.assert_not_called()


if __name__ == "__main__":
    unittest.main()
