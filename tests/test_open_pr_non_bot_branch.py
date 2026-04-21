"""Regression tests for the PR-open-time non-bot-branch park path.

Issue #1065: when a human-authored PR is opened on a non-
``auto-improve/<N>-…`` branch, ``handle_open_to_review`` must
immediately apply the ``open_to_human`` transition so the PR parks
at ``pr:human-needed`` **before** any review / rebase / docs cycle
is spent. The prior behaviour tagged every fresh PR
``pr:reviewing-code`` and only parked at merge time via
``not_bot_branch`` in ``handle_merge``, wasting agent time and
polluting the audit log with downstream pipeline transitions for
a PR that would never auto-merge.
"""
import os
import sys
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cai_lib.actions import open_pr as open_pr_mod


def _pr(number: int, branch: str) -> dict:
    return {
        "number": number,
        "title": "t",
        "headRefName": branch,
        "headRefOid": "deadbeef",
        "labels": [],
        "state": "OPEN",
        "mergeable": "MERGEABLE",
        "mergedAt": None,
        "comments": [],
        "reviews": [],
        "createdAt": "2024-01-01T00:00:00Z",
    }


class TestHandleOpenToReviewNonBotBranch(unittest.TestCase):
    """Non-bot-branch PRs must park via ``open_to_human`` at PR-open time."""

    def test_non_bot_branch_parks_as_human_needed(self):
        pr = _pr(945, "feat/audit-modules-loader-886")
        run_mock = MagicMock()
        run_mock.return_value.returncode = 0
        run_mock.return_value.stdout = ""
        run_mock.return_value.stderr = ""
        transition_mock = MagicMock(return_value=(True, False))
        log_mock = MagicMock()

        with patch.object(open_pr_mod, "_run", run_mock), \
             patch.object(open_pr_mod, "fire_trigger", transition_mock), \
             patch.object(open_pr_mod, "log_run", log_mock):
            rc = open_pr_mod.handle_open_to_review(pr)

        self.assertEqual(rc, 0)
        transition_mock.assert_called_once()
        args, _ = transition_mock.call_args
        self.assertEqual(args[0], 945)
        self.assertEqual(args[1], "open_to_human")

        gh_comment_calls = [
            call for call in run_mock.call_args_list
            if call.args and call.args[0][:3] == ["gh", "pr", "comment"]
        ]
        self.assertEqual(len(gh_comment_calls), 1)
        body_arg_idx = gh_comment_calls[0].args[0].index("--body") + 1
        body = gh_comment_calls[0].args[0][body_arg_idx]
        self.assertIn("feat/audit-modules-loader-886", body)
        self.assertIn("pr:human-needed", body)

        log_call_kwargs = log_mock.call_args.kwargs
        self.assertEqual(log_call_kwargs.get("result"), "not_bot_branch_open")
        self.assertEqual(log_call_kwargs.get("exit"), 0)

    def test_bot_branch_applies_open_to_reviewing_code(self):
        pr = _pr(946, "auto-improve/945-some-slug")
        run_mock = MagicMock()
        run_mock.return_value.returncode = 0
        transition_mock = MagicMock(return_value=(True, False))
        log_mock = MagicMock()

        with patch.object(open_pr_mod, "_run", run_mock), \
             patch.object(open_pr_mod, "fire_trigger", transition_mock), \
             patch.object(open_pr_mod, "log_run", log_mock):
            rc = open_pr_mod.handle_open_to_review(pr)

        self.assertEqual(rc, 0)
        transition_mock.assert_called_once()
        args, _ = transition_mock.call_args
        self.assertEqual(args[0], 946)
        self.assertEqual(args[1], "open_to_reviewing_code")

        gh_comment_calls = [
            call for call in run_mock.call_args_list
            if call.args and call.args[0][:3] == ["gh", "pr", "comment"]
        ]
        self.assertEqual(len(gh_comment_calls), 0)
        log_mock.assert_not_called()

    def test_empty_branch_parks_as_human_needed(self):
        pr = _pr(947, "")
        run_mock = MagicMock()
        run_mock.return_value.returncode = 0
        transition_mock = MagicMock(return_value=(True, False))
        log_mock = MagicMock()

        with patch.object(open_pr_mod, "_run", run_mock), \
             patch.object(open_pr_mod, "fire_trigger", transition_mock), \
             patch.object(open_pr_mod, "log_run", log_mock):
            rc = open_pr_mod.handle_open_to_review(pr)

        self.assertEqual(rc, 0)
        transition_mock.assert_called_once()
        args, _ = transition_mock.call_args
        self.assertEqual(args[0], 947)
        self.assertEqual(args[1], "open_to_human")


if __name__ == "__main__":
    unittest.main()
