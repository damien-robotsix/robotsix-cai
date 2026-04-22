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


if __name__ == "__main__":
    unittest.main()
