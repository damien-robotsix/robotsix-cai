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

    @patch("cai_lib.actions.plan._post_issue_comment")
    @patch("cai_lib.actions.plan.apply_transition")
    @patch("cai_lib.actions.plan.apply_transition_with_confidence")
    @patch("cai_lib.actions.plan.log_run")
    def test_requires_review_high_conf_still_diverts(
        self, _mock_log, mock_gated, mock_apply, mock_post
    ):
        from cai_lib.actions.plan import handle_plan_gate
        mock_apply.return_value = True

        rc = handle_plan_gate(self._issue(
            confidence=Confidence.HIGH,
            requires_review=True,
        ))

        self.assertEqual(rc, 0)
        # The confidence-gated transition must NOT have been called.
        mock_gated.assert_not_called()
        # Instead, the direct planned_to_human transition must fire.
        args, kwargs = mock_apply.call_args
        self.assertEqual(args[0], 982)
        self.assertEqual(args[1], "planned_to_human")
        # The divert reason must be passed as a kwarg to apply_transition
        # (which now posts the MARKER comment itself — no direct _post_issue_comment).
        divert_reason = kwargs.get("divert_reason", "")
        self.assertIn(
            "Plan diverges from refined-issue preference",
            divert_reason,
        )
        self.assertIn("admin approval required", divert_reason)
        mock_post.assert_not_called()

    @patch("cai_lib.actions.plan._post_issue_comment")
    @patch("cai_lib.actions.plan.apply_transition")
    @patch("cai_lib.actions.plan.apply_transition_with_confidence")
    @patch("cai_lib.actions.plan.log_run")
    def test_requires_review_false_falls_through_to_confidence_gate(
        self, _mock_log, mock_gated, mock_apply, mock_post
    ):
        from cai_lib.actions.plan import handle_plan_gate
        mock_gated.return_value = (True, False)

        rc = handle_plan_gate(self._issue(
            confidence=Confidence.HIGH,
            requires_review=False,
        ))

        self.assertEqual(rc, 0)
        # No bespoke divert — the confidence gate handles the promotion.
        mock_apply.assert_not_called()
        mock_post.assert_not_called()
        # apply_transition_with_confidence must have been called on the
        # default transition because HIGH meets its threshold.
        call_args = mock_gated.call_args[0]
        self.assertEqual(call_args[1], "planned_to_plan_approved")

    @patch("cai_lib.actions.plan._post_issue_comment")
    @patch("cai_lib.actions.plan.apply_transition")
    @patch("cai_lib.actions.plan.apply_transition_with_confidence")
    @patch("cai_lib.actions.plan.log_run")
    def test_requires_review_refused_returns_1(
        self, _mock_log, mock_gated, mock_apply, _mock_post
    ):
        from cai_lib.actions.plan import handle_plan_gate
        mock_apply.return_value = False  # transition refused

        rc = handle_plan_gate(self._issue(
            confidence=Confidence.MEDIUM,
            requires_review=True,
        ))

        self.assertEqual(rc, 1)
        mock_gated.assert_not_called()

    @patch("cai_lib.actions.plan._post_issue_comment")
    @patch("cai_lib.actions.plan.apply_transition")
    @patch("cai_lib.actions.plan.apply_transition_with_confidence")
    @patch("cai_lib.actions.plan.log_run")
    def test_requires_review_reparsed_from_body_when_stash_missing(
        self, _mock_log, mock_gated, mock_apply, mock_post
    ):
        """When the in-process stash is absent, the flag must be
        re-parsed from the stored plan block in the issue body."""
        from cai_lib.actions.plan import handle_plan_gate
        mock_apply.return_value = True

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
        mock_gated.assert_not_called()
        args, kwargs = mock_apply.call_args
        self.assertEqual(args[1], "planned_to_human")
        # divert_reason kwarg must carry the admin-approval message
        divert_reason = kwargs.get("divert_reason", "")
        self.assertIn("admin approval required", divert_reason)
        mock_post.assert_not_called()


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

    @patch("cai_lib.actions.plan.apply_transition_with_confidence")
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
        self.assertEqual(args[2], Confidence.MEDIUM)

    @patch("cai_lib.actions.plan.apply_transition_with_confidence")
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
        self.assertEqual(args[2], Confidence.LOW)

    @patch("cai_lib.actions.plan.apply_transition_with_confidence")
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
        self.assertEqual(args[2], Confidence.HIGH)

    @patch("cai_lib.actions.plan.apply_transition_with_confidence")
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

    @patch("cai_lib.actions.plan.apply_transition_with_confidence")
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
        self.assertEqual(args[2], Confidence.MEDIUM)

    @patch("cai_lib.actions.plan.apply_transition_with_confidence")
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

    @patch("cai_lib.actions.plan.apply_transition_with_confidence")
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
        self.assertEqual(args[2], Confidence.MEDIUM)

    @patch("cai_lib.actions.plan.apply_transition_with_confidence")
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
        self.assertEqual(args[2], Confidence.LOW)

    @patch("cai_lib.actions.plan.apply_transition_with_confidence")
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
        self.assertEqual(args[2], Confidence.MEDIUM)

    @patch("cai_lib.actions.plan.apply_transition_with_confidence")
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

    @patch("cai_lib.actions.plan.apply_transition_with_confidence")
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

    @patch("cai_lib.actions.plan.apply_transition_with_confidence")
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
        self.assertEqual(args[2], Confidence.MEDIUM)


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


if __name__ == "__main__":
    unittest.main()
