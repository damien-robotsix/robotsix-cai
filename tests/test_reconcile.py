"""Tests for _reconcile_interrupted classification logic."""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cai_lib as cai

_MOD = "cai_lib.cmd_lifecycle"


class TestReconcileFix(unittest.TestCase):
    """cmd='fix' cases."""

    @patch(f"{_MOD}._gh_json")
    def test_no_branch_no_pr(self, mock_gh):
        """No branch, no PR → not_started."""
        mock_gh.side_effect = [
            [],   # pr list → empty
            [],   # matching-refs → empty
        ]
        self.assertEqual(
            cai._reconcile_interrupted("implement", "issue", 123), "not_started"
        )

    @patch(f"{_MOD}._gh_json")
    def test_branch_exists_no_pr(self, mock_gh):
        """Branch exists but no open PR → partially_done."""
        mock_gh.side_effect = [
            [],  # pr list → no matching PRs
            [{"ref": "refs/heads/auto-improve/123-abc"}],  # matching-refs → hit
        ]
        self.assertEqual(
            cai._reconcile_interrupted("implement", "issue", 123), "partially_done"
        )

    @patch(f"{_MOD}._gh_json")
    def test_open_pr_exists(self, mock_gh):
        """Open PR with matching head → completed_externally."""
        mock_gh.side_effect = [
            [{"headRefName": "auto-improve/123-abc"}],  # pr list → match
            # matching-refs not called
        ]
        self.assertEqual(
            cai._reconcile_interrupted("implement", "issue", 123),
            "completed_externally",
        )

    def test_none_issue_number(self):
        """target_id=None → not_started without any gh calls."""
        self.assertEqual(
            cai._reconcile_interrupted("implement", "issue", None), "not_started"
        )


class TestReconcileRevise(unittest.TestCase):
    """cmd='revise' cases."""

    @patch(f"{_MOD}._issue_has_label", return_value=False)
    @patch(f"{_MOD}._gh_json", return_value=[])
    def test_no_pr_no_label(self, _gh, _lbl):
        self.assertEqual(
            cai._reconcile_interrupted("revise", "issue", 200), "not_started"
        )

    @patch(f"{_MOD}._issue_has_label", return_value=True)
    @patch(f"{_MOD}._gh_json", return_value=[
        {"headRefName": "auto-improve/200-slug"}
    ])
    def test_pr_and_label(self, _gh, _lbl):
        """PR open + :revising still set → partially_done."""
        self.assertEqual(
            cai._reconcile_interrupted("revise", "issue", 200),
            "partially_done",
        )

    @patch(f"{_MOD}._issue_has_label", return_value=False)
    @patch(f"{_MOD}._gh_json", return_value=[
        {"headRefName": "auto-improve/200-slug"}
    ])
    def test_pr_no_label(self, _gh, _lbl):
        """PR open + :revising removed → completed_externally."""
        self.assertEqual(
            cai._reconcile_interrupted("revise", "issue", 200),
            "completed_externally",
        )


class TestReconcileRefine(unittest.TestCase):
    """cmd='refine' cases."""

    @patch(f"{_MOD}._gh_json", return_value={"body": "Some issue text"})
    def test_no_plan(self, _gh):
        self.assertEqual(
            cai._reconcile_interrupted("refine", "issue", 300), "not_started"
        )

    @patch(f"{_MOD}._gh_json", return_value={"body": "## Problem\n\n### Plan\n1. Do X"})
    def test_has_plan(self, _gh):
        self.assertEqual(
            cai._reconcile_interrupted("refine", "issue", 300),
            "completed_externally",
        )


class TestReconcileDefault(unittest.TestCase):
    """Other commands return not_started (no gh calls)."""

    def test_analyze(self):
        self.assertEqual(
            cai._reconcile_interrupted("analyze", "issue", 400), "not_started"
        )

    def test_audit(self):
        self.assertEqual(
            cai._reconcile_interrupted("audit", "issue", None), "not_started"
        )

    def test_merge(self):
        self.assertEqual(
            cai._reconcile_interrupted("merge", "pr", 500), "not_started"
        )

    def test_unknown_command(self):
        self.assertEqual(
            cai._reconcile_interrupted("whatever", "issue", 1), "not_started"
        )


if __name__ == "__main__":
    unittest.main()
