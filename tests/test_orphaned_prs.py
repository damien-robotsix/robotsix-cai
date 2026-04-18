"""Tests for the shared `_close_orphaned_prs` helper in cai_lib.github.

Covers the branch-regex contract (issue number parsed from the first
`\\d+` group of the `auto-improve/<n>-…` prefix, so sub-step branches
like `auto-improve/832-827-step-3-3-…` resolve to 832) and the guard
that only closes PRs whose linked issue is actually CLOSED.
"""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cai_lib import github as gh  # noqa: E402


class TestCloseOrphanedPrs(unittest.TestCase):

    def _fake_gh(self, prs, issue_states):
        """Build a side-effect callable dispatching on `gh` sub-args."""

        def _side_effect(args):
            if args[:2] == ["pr", "list"]:
                return prs
            if args[:2] == ["issue", "view"]:
                num = int(args[2])
                return {"state": issue_states.get(num, "OPEN")}
            raise AssertionError(f"unexpected gh call: {args}")

        return _side_effect

    def test_closes_pr_when_linked_issue_closed(self):
        prs = [{"number": 857,
                "headRefName":
                    "auto-improve/832-827-step-3-3-remove-original-flat-agent-files"}]
        states = {832: "CLOSED"}
        closed_cmds: list[list[str]] = []

        def fake_run(args, **kwargs):
            if args[:3] == ["gh", "pr", "close"]:
                closed_cmds.append(args)

                class R:
                    returncode = 0
                    stderr = ""
                return R()
            raise AssertionError(f"unexpected _run: {args}")

        with patch.object(gh, "_gh_json",
                          side_effect=self._fake_gh(prs, states)), \
                patch.object(gh, "_set_labels", return_value=True), \
                patch.object(gh, "_run", side_effect=fake_run), \
                patch.object(gh, "log_run"):
            result = gh._close_orphaned_prs(log_prefix="cai audit")

        self.assertEqual(result, [{"pr": 857, "issue": 832}])
        self.assertEqual(len(closed_cmds), 1)
        self.assertIn("857", closed_cmds[0])
        self.assertIn("--delete-branch", closed_cmds[0])

    def test_skips_pr_when_linked_issue_still_open(self):
        prs = [{"number": 900,
                "headRefName": "auto-improve/500-some-fix"}]
        states = {500: "OPEN"}

        with patch.object(gh, "_gh_json",
                          side_effect=self._fake_gh(prs, states)), \
                patch.object(gh, "_set_labels", return_value=True), \
                patch.object(gh, "_run") as run_mock, \
                patch.object(gh, "log_run"):
            result = gh._close_orphaned_prs(log_prefix="cai audit")

        self.assertEqual(result, [])
        run_mock.assert_not_called()

    def test_skips_pr_with_non_auto_improve_branch(self):
        prs = [{"number": 123, "headRefName": "feature/manual-branch"}]

        with patch.object(gh, "_gh_json", return_value=prs), \
                patch.object(gh, "_run") as run_mock, \
                patch.object(gh, "log_run"):
            result = gh._close_orphaned_prs(log_prefix="cai audit")

        self.assertEqual(result, [])
        run_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
