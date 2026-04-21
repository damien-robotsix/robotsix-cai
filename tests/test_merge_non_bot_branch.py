"""Regression test for the non-bot branch park path in handle_merge.

Issue #1013: when a human-authored PR carrying ``pr:approved`` (admin
applied the label hoping for auto-merge) sat on a non-``auto-improve/``
branch, ``handle_merge`` logged ``result=not_bot_branch`` and returned
0 without changing state, so the dispatcher re-routed the same PR to
``handle_merge`` on every drain tick. The fix applies the
``approved_to_human`` PR transition so the PR parks at
``PR_HUMAN_NEEDED``.
"""
import os
import sys
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cai_lib.actions import merge as merge_mod


def _pr_non_bot(number: int = 945) -> dict:
    return {
        "number": number,
        "title": "Human-authored PR on a non-bot branch",
        "headRefName": "feat/audit-modules-loader-886",
        "headRefOid": "deadbeefcafef00d",
        "labels": [{"name": "pr:approved"}],
        "state": "OPEN",
        "mergeable": "MERGEABLE",
        "mergeStateStatus": "CLEAN",
        "mergedAt": None,
        "comments": [],
        "reviews": [],
        "createdAt": "2024-01-01T00:00:00Z",
    }


class TestHandleMergeNonBotBranch(unittest.TestCase):
    """Non-bot branches must park via approved_to_human, not loop forever."""

    def test_non_bot_branch_parks_as_human_needed(self):
        pr = _pr_non_bot()
        run_mock = MagicMock()
        run_mock.return_value.returncode = 0
        run_mock.return_value.stdout = ""
        run_mock.return_value.stderr = ""
        transition_mock = MagicMock(return_value=True)
        log_mock = MagicMock()

        with patch.object(merge_mod, "_run", run_mock), \
             patch.object(merge_mod, "fire_trigger", transition_mock), \
             patch.object(merge_mod, "log_run", log_mock):
            rc = merge_mod.handle_merge(pr)

        self.assertEqual(rc, 0)
        transition_mock.assert_called_once()
        args, kwargs = transition_mock.call_args
        self.assertEqual(args[0], 945)
        self.assertEqual(args[1], "approved_to_human")

        # A single comment is posted explaining the park.
        gh_comment_calls = [
            call for call in run_mock.call_args_list
            if call.args and call.args[0][:3] == ["gh", "pr", "comment"]
        ]
        self.assertEqual(len(gh_comment_calls), 1)
        body_arg_idx = gh_comment_calls[0].args[0].index("--body") + 1
        body = gh_comment_calls[0].args[0][body_arg_idx]
        self.assertIn("feat/audit-modules-loader-886", body)
        self.assertIn("pr:human-needed", body)

        # Telemetry log tag preserved for audit compatibility.
        log_call_kwargs = log_mock.call_args.kwargs
        self.assertEqual(log_call_kwargs.get("result"), "not_bot_branch")
        self.assertEqual(log_call_kwargs.get("exit"), 0)

    def test_non_bot_branch_does_not_call_merge_agent(self):
        """Park must fire *before* any _run_claude_p invocation."""
        pr = _pr_non_bot()
        run_mock = MagicMock()
        run_mock.return_value.returncode = 0
        claude_mock = MagicMock()

        with patch.object(merge_mod, "_run", run_mock), \
             patch.object(merge_mod, "_run_claude_p", claude_mock), \
             patch.object(merge_mod, "fire_trigger",
                          MagicMock(return_value=True)), \
             patch.object(merge_mod, "log_run", MagicMock()):
            merge_mod.handle_merge(pr)

        claude_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
