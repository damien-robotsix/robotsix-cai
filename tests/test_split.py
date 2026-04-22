"""Tests for cai_lib.actions.split — the SPLITTING-state handler.

Covers the three verdict branches:

- Atomic + HIGH confidence → fires ``splitting_to_planning``.
- Decompose + HIGH confidence → creates sub-issues, labels parent
  ``auto-improve:parent``.
- Anything else (LOW confidence, missing marker, malformed
  decomposition, over-depth decomposition) → fires
  ``splitting_to_human`` with a reasoned divert.

The entry transition ``refined_to_splitting`` is fired by
:func:`cai_lib.dispatcher.drive_issue` before this handler runs; see
``tests/test_dispatcher.py::TestDriveIssue``. The handler rejects any
state other than :splitting to guard against label corruption.
"""
import os
import sys
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cai_lib.actions.split import handle_split
from cai_lib.actions.plan import _run_post_plan_resplit


_POST_PLAN_RESPLIT_FIXTURE_PLAN = (
    "<!-- cai-plan-start -->\n"
    "## Selected Implementation Plan\n\n"
    "### Files to change\n"
    + "".join(f"- `pkg/f{i}.py`: change it\n" for i in range(15))
    + "\n### Detailed steps\n\n"
    + "".join(
        f"#### Step {i+1} — Edit `pkg/f{i}.py`\nbody\n\n" for i in range(15)
    )
    + "Confidence: HIGH\n"
    "<!-- cai-plan-end -->"
)


def _refined_issue(number: int = 1) -> dict:
    labels = [
        {"name": "auto-improve"},
        {"name": "auto-improve:refined"},
    ]
    return {
        "number": number,
        "title": "Test issue",
        "body": "## Refined Issue\n\n### Description\nSomething.",
        "labels": labels,
    }


def _splitting_issue(number: int = 1) -> dict:
    labels = [
        {"name": "auto-improve"},
        {"name": "auto-improve:splitting"},
    ]
    return {
        "number": number,
        "title": "Test issue",
        "body": "## Refined Issue\n\n### Description\nSomething.",
        "labels": labels,
    }


class TestSplitEntryGuard(unittest.TestCase):
    """Entry transition ``refined_to_splitting`` lives in
    :func:`cai_lib.dispatcher.drive_issue` (tested there). ``handle_split``
    must never fire it and must refuse any state other than :splitting."""

    @patch("cai_lib.actions.split.log_run")
    @patch("cai_lib.actions.split._run_claude_p")
    @patch("cai_lib.actions.split.fire_trigger")
    @patch("cai_lib.actions.split._build_issue_block", return_value="issue text")
    def test_splitting_resume_does_not_fire_refined_to_splitting(
        self, mock_build, mock_fire, mock_claude, mock_log_run,
    ):
        mock_claude.return_value = MagicMock(
            returncode=0,
            stdout="## Split Verdict\n\nVERDICT: ATOMIC\n\nConfidence: HIGH\n",
            stderr="",
        )
        handle_split(_splitting_issue())
        fired = [c.args[1] for c in mock_fire.call_args_list]
        self.assertNotIn("refined_to_splitting", fired)

    @patch("cai_lib.actions.split.log_run")
    @patch("cai_lib.actions.split._run_claude_p")
    @patch("cai_lib.actions.split.fire_trigger")
    def test_refined_state_rejected(self, mock_fire, mock_claude, mock_log_run):
        """An issue still at :refined must abort (drive_issue is
        expected to have fired the entry transition first)."""
        rc = handle_split(_refined_issue())
        self.assertEqual(rc, 1)
        mock_claude.assert_not_called()


class TestSplitAtomicVerdict(unittest.TestCase):

    @patch("cai_lib.actions.split.log_run")
    @patch("cai_lib.actions.split._run_claude_p")
    @patch("cai_lib.actions.split.fire_trigger")
    @patch("cai_lib.actions.split._build_issue_block", return_value="issue text")
    def test_atomic_high_confidence_fires_splitting_to_planning(
        self, mock_build, mock_fire, mock_claude, mock_log_run,
    ):
        mock_claude.return_value = MagicMock(
            returncode=0,
            stdout=(
                "## Split Verdict\n\n"
                "VERDICT: ATOMIC\n\n"
                "### Reasoning\nFits in one PR.\n\n"
                "Confidence: HIGH\n"
            ),
            stderr="",
        )
        self.assertEqual(handle_split(_splitting_issue()), 0)
        fired = [c.args[1] for c in mock_fire.call_args_list]
        self.assertIn("splitting_to_planning", fired)
        self.assertNotIn("splitting_to_human", fired)

    @patch("cai_lib.actions.split.log_run")
    @patch("cai_lib.actions.split._run_claude_p")
    @patch("cai_lib.actions.split.fire_trigger")
    @patch("cai_lib.actions.split._build_issue_block", return_value="issue text")
    def test_atomic_low_confidence_diverts_to_human(
        self, mock_build, mock_fire, mock_claude, mock_log_run,
    ):
        mock_claude.return_value = MagicMock(
            returncode=0,
            stdout=(
                "## Split Verdict\n\n"
                "VERDICT: ATOMIC\n\n"
                "Confidence: LOW\n"
            ),
            stderr="",
        )
        handle_split(_splitting_issue())
        fired = [c.args[1] for c in mock_fire.call_args_list]
        self.assertIn("splitting_to_human", fired)
        self.assertNotIn("splitting_to_planning", fired)


class TestSplitDecomposeVerdict(unittest.TestCase):

    @patch("cai_lib.actions.split.log_run")
    @patch("cai_lib.actions.split._set_labels")
    @patch("cai_lib.actions.split._create_sub_issues", return_value=[10, 11])
    @patch("cai_lib.actions.split._run_claude_p")
    @patch("cai_lib.actions.split.fire_trigger")
    @patch("cai_lib.actions.split._build_issue_block", return_value="issue text")
    def test_decompose_high_confidence_creates_sub_issues(
        self, mock_build, mock_fire, mock_claude, mock_create_subs,
        mock_set_labels, mock_log_run,
    ):
        mock_claude.return_value = MagicMock(
            returncode=0,
            stdout=(
                "## Multi-Step Decomposition\n\n"
                "### Step 1: First step\n"
                "Body one.\n\n"
                "### Step 2: Second step\n"
                "Body two.\n\n"
                "Confidence: HIGH\n"
            ),
            stderr="",
        )
        handle_split(_splitting_issue())
        mock_create_subs.assert_called_once()
        # _set_labels must add auto-improve:parent and remove :splitting.
        call_kwargs = mock_set_labels.call_args.kwargs
        self.assertIn("auto-improve:parent", call_kwargs["add"])
        self.assertIn("auto-improve:splitting", call_kwargs["remove"])

    @patch("cai_lib.actions.split.log_run")
    @patch("cai_lib.actions.split._set_labels")
    @patch("cai_lib.actions.split._create_sub_issues")
    @patch("cai_lib.actions.split._run_claude_p")
    @patch("cai_lib.actions.split.fire_trigger")
    @patch("cai_lib.actions.split._build_issue_block", return_value="issue text")
    def test_decompose_low_confidence_diverts_to_human(
        self, mock_build, mock_fire, mock_claude, mock_create_subs,
        mock_set_labels, mock_log_run,
    ):
        mock_claude.return_value = MagicMock(
            returncode=0,
            stdout=(
                "## Multi-Step Decomposition\n\n"
                "### Step 1: First\nA.\n\n"
                "### Step 2: Second\nB.\n\n"
                "Confidence: LOW\n"
            ),
            stderr="",
        )
        handle_split(_splitting_issue())
        mock_create_subs.assert_not_called()
        mock_set_labels.assert_not_called()
        fired = [c.args[1] for c in mock_fire.call_args_list]
        self.assertIn("splitting_to_human", fired)

    @patch("cai_lib.actions.split.log_run")
    @patch("cai_lib.actions.split._set_labels")
    @patch("cai_lib.actions.split._create_sub_issues")
    @patch("cai_lib.actions.split._run_claude_p")
    @patch("cai_lib.actions.split.fire_trigger")
    @patch("cai_lib.actions.split._build_issue_block", return_value="issue text")
    @patch("cai_lib.actions.split._issue_depth", return_value=1)
    def test_decompose_over_max_depth_diverts_to_human(
        self, mock_depth, mock_build, mock_fire, mock_claude, mock_create_subs,
        mock_set_labels, mock_log_run,
    ):
        mock_claude.return_value = MagicMock(
            returncode=0,
            stdout=(
                "## Multi-Step Decomposition\n\n"
                "### Step 1: First\nA.\n\n"
                "### Step 2: Second\nB.\n\n"
                "Confidence: HIGH\n"
            ),
            stderr="",
        )
        with patch("cai_lib.actions.split.MAX_DECOMPOSITION_DEPTH", 1):
            handle_split(_splitting_issue())
        mock_create_subs.assert_not_called()
        fired = [c.args[1] for c in mock_fire.call_args_list]
        self.assertIn("splitting_to_human", fired)

    @patch("cai_lib.actions.split.log_run")
    @patch("cai_lib.actions.split._set_labels")
    @patch("cai_lib.actions.split._create_sub_issues")
    @patch("cai_lib.actions.split._run_claude_p")
    @patch("cai_lib.actions.split.fire_trigger")
    @patch("cai_lib.actions.split._build_issue_block", return_value="issue text")
    def test_decompose_single_step_is_malformed(
        self, mock_build, mock_fire, mock_claude, mock_create_subs,
        mock_set_labels, mock_log_run,
    ):
        mock_claude.return_value = MagicMock(
            returncode=0,
            stdout=(
                "## Multi-Step Decomposition\n\n"
                "### Step 1: Only step\nA.\n\n"
                "Confidence: HIGH\n"
            ),
            stderr="",
        )
        handle_split(_splitting_issue())
        mock_create_subs.assert_not_called()
        fired = [c.args[1] for c in mock_fire.call_args_list]
        self.assertIn("splitting_to_human", fired)


class TestSplitUnclearAndMalformed(unittest.TestCase):

    @patch("cai_lib.actions.split.log_run")
    @patch("cai_lib.actions.split._run_claude_p")
    @patch("cai_lib.actions.split.fire_trigger")
    @patch("cai_lib.actions.split._build_issue_block", return_value="issue text")
    def test_unclear_verdict_diverts_to_human(
        self, mock_build, mock_fire, mock_claude, mock_log_run,
    ):
        mock_claude.return_value = MagicMock(
            returncode=0,
            stdout=(
                "## Split Verdict\n\n"
                "VERDICT: UNCLEAR\n\n"
                "### Reasoning\nBoundary case.\n\n"
                "Confidence: LOW\n"
            ),
            stderr="",
        )
        handle_split(_splitting_issue())
        fired = [c.args[1] for c in mock_fire.call_args_list]
        self.assertIn("splitting_to_human", fired)

    @patch("cai_lib.actions.split.log_run")
    @patch("cai_lib.actions.split._run_claude_p")
    @patch("cai_lib.actions.split.fire_trigger")
    @patch("cai_lib.actions.split._build_issue_block", return_value="issue text")
    def test_no_marker_diverts_to_human(
        self, mock_build, mock_fire, mock_claude, mock_log_run,
    ):
        mock_claude.return_value = MagicMock(
            returncode=0,
            stdout="nothing structured here",
            stderr="",
        )
        handle_split(_splitting_issue())
        fired = [c.args[1] for c in mock_fire.call_args_list]
        self.assertIn("splitting_to_human", fired)


class TestPostPlanResplit(unittest.TestCase):
    """#1167 — post-plan re-split checkpoint driven by _run_post_plan_resplit.

    The helper lives in ``cai_lib.actions.plan`` but the tests live
    here because the agent driven by the checkpoint is ``cai-split``
    and the behaviour mirrors ``handle_split``'s decompose branch
    (create sub-issues, label the parent :parent). The pre-plan
    ``handle_split`` path is exercised by the TestSplit* suites above
    and is NOT affected by these tests — the post-plan helper is
    strictly additive.
    """

    def _planned_issue(self, number: int = 1167, body=None) -> dict:
        return {
            "number": number,
            "title": "post-plan resplit fixture",
            "body": body if body is not None else _POST_PLAN_RESPLIT_FIXTURE_PLAN,
            "labels": [
                {"name": "auto-improve"},
                {"name": "auto-improve:planned"},
            ],
        }

    @patch("cai_lib.actions.plan._run_claude_p")
    @patch("cai_lib.actions.plan._issue_depth", return_value=0)
    def test_no_stored_plan_block_short_circuits(
        self, mock_depth, mock_claude,
    ):
        """Without a ``<!-- cai-plan-start -->`` block the helper must
        return None without invoking the agent — guards against
        wasted spend on stale :planned labels."""
        issue = self._planned_issue(body="no plan block here")
        self.assertIsNone(_run_post_plan_resplit(issue, "ignored"))
        mock_claude.assert_not_called()
        mock_depth.assert_not_called()

    @patch("cai_lib.actions.plan._run_claude_p")
    @patch("cai_lib.actions.plan._issue_depth", return_value=0)
    def test_keep_verdict_returns_none(self, _mock_depth, mock_claude):
        """A ``VERDICT: KEEP`` response must fall through so the
        normal confidence gate runs unchanged."""
        mock_claude.return_value = MagicMock(
            returncode=0,
            stdout=(
                "## Split Verdict\n\nVERDICT: KEEP\n\n"
                "### Reasoning\nPlan scale matches ATOMIC.\n\n"
                "Confidence: HIGH\n"
            ),
            stderr="",
        )
        self.assertIsNone(
            _run_post_plan_resplit(
                self._planned_issue(), "plan text",
            )
        )

    @patch("cai_lib.actions.plan._run_claude_p")
    @patch("cai_lib.actions.plan._issue_depth", return_value=0)
    def test_agent_failure_returns_none(self, _mock_depth, mock_claude):
        """A non-zero agent exit must fall through so the gate's
        existing safety nets still fire."""
        mock_claude.return_value = MagicMock(
            returncode=2, stdout="", stderr="boom",
        )
        self.assertIsNone(
            _run_post_plan_resplit(
                self._planned_issue(), "plan text",
            )
        )

    @patch("cai_lib.actions.plan._set_labels")
    @patch("cai_lib.actions.plan._create_sub_issues", return_value=[200, 201])
    @patch("cai_lib.actions.plan.fire_trigger", return_value=(True, False))
    @patch("cai_lib.actions.plan._run")
    @patch("cai_lib.actions.plan._run_claude_p")
    @patch("cai_lib.actions.plan._issue_depth", return_value=0)
    def test_resplit_high_creates_sub_issues(
        self, _mock_depth, mock_claude, mock_run, mock_fire,
        mock_create_subs, mock_set_labels,
    ):
        """RESPLIT + HIGH + well-formed decomposition must fire
        planned_to_splitting, create sub-issues, and apply
        LABEL_PARENT / remove LABEL_SPLITTING."""
        mock_claude.return_value = MagicMock(
            returncode=0,
            stdout=(
                "## Multi-Step Decomposition\n\n"
                "### Step 1: First slice\nBody one.\n\n"
                "### Step 2: Second slice\nBody two.\n\n"
                "VERDICT: RESPLIT\n\n"
                "Confidence: HIGH\n"
            ),
            stderr="",
        )
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        mock_set_labels.return_value = True

        rc = _run_post_plan_resplit(
            self._planned_issue(), "plan text",
        )

        self.assertEqual(rc, 0)
        # fire_trigger must be called with planned_to_splitting.
        fired = [c.args[1] for c in mock_fire.call_args_list]
        self.assertIn("planned_to_splitting", fired)
        # Sub-issues were requested.
        mock_create_subs.assert_called_once()
        subs_args = mock_create_subs.call_args.args
        self.assertEqual(subs_args[1], 1167)  # parent number
        self.assertEqual(len(subs_args[0]), 2)  # two steps
        # Parent labelled :parent, splitting removed.
        mock_set_labels.assert_called_once()
        set_kwargs = mock_set_labels.call_args.kwargs
        from cai_lib.config import LABEL_PARENT, LABEL_SPLITTING
        self.assertEqual(set_kwargs.get("add"), [LABEL_PARENT])
        self.assertEqual(set_kwargs.get("remove"), [LABEL_SPLITTING])

    @patch("cai_lib.actions.plan._set_labels")
    @patch("cai_lib.actions.plan._create_sub_issues")
    @patch("cai_lib.actions.plan.fire_trigger")
    @patch("cai_lib.actions.plan._run")
    @patch("cai_lib.actions.plan._run_claude_p")
    @patch("cai_lib.actions.plan._issue_depth", return_value=0)
    def test_resplit_low_confidence_falls_through(
        self, _mock_depth, mock_claude, mock_run, mock_fire,
        mock_create_subs, mock_set_labels,
    ):
        """RESPLIT + LOW confidence must NOT act; the helper returns
        None so handle_plan_gate's #1131 safety net still fires."""
        mock_claude.return_value = MagicMock(
            returncode=0,
            stdout=(
                "## Multi-Step Decomposition\n\n"
                "### Step 1: First\nA.\n\n"
                "### Step 2: Second\nB.\n\n"
                "VERDICT: RESPLIT\n\n"
                "Confidence: LOW\n"
            ),
            stderr="",
        )
        self.assertIsNone(
            _run_post_plan_resplit(
                self._planned_issue(), "plan text",
            )
        )
        mock_run.assert_not_called()
        mock_fire.assert_not_called()
        mock_create_subs.assert_not_called()
        mock_set_labels.assert_not_called()

    @patch("cai_lib.actions.plan._set_labels")
    @patch("cai_lib.actions.plan._create_sub_issues")
    @patch("cai_lib.actions.plan.fire_trigger")
    @patch("cai_lib.actions.plan._run")
    @patch("cai_lib.actions.plan._run_claude_p")
    @patch("cai_lib.actions.plan._issue_depth", return_value=0)
    def test_resplit_single_step_is_malformed(
        self, _mock_depth, mock_claude, mock_run, mock_fire,
        mock_create_subs, mock_set_labels,
    ):
        """A decomposition with fewer than 2 steps must not act."""
        mock_claude.return_value = MagicMock(
            returncode=0,
            stdout=(
                "## Multi-Step Decomposition\n\n"
                "### Step 1: Only step\nA.\n\n"
                "VERDICT: RESPLIT\n\n"
                "Confidence: HIGH\n"
            ),
            stderr="",
        )
        self.assertIsNone(
            _run_post_plan_resplit(
                self._planned_issue(), "plan text",
            )
        )
        mock_run.assert_not_called()
        mock_fire.assert_not_called()
        mock_create_subs.assert_not_called()
        mock_set_labels.assert_not_called()

    @patch("cai_lib.actions.plan._run_claude_p")
    @patch("cai_lib.actions.plan._issue_depth", return_value=5)
    def test_depth_cap_short_circuits(self, _mock_depth, mock_claude):
        """An issue already at MAX_DECOMPOSITION_DEPTH must not
        re-split; the helper returns None without invoking the
        agent."""
        with patch("cai_lib.actions.plan.MAX_DECOMPOSITION_DEPTH", 5):
            self.assertIsNone(
                _run_post_plan_resplit(
                    self._planned_issue(), "plan text",
                )
            )
        mock_claude.assert_not_called()


if __name__ == "__main__":
    unittest.main()
