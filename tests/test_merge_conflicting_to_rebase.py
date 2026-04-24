"""Tests that handle_merge routes CONFLICTING PRs to REBASING.

Regression guard for the gap where a PR that became CONFLICTING against
main — either on entry (safety filter 2) or after ``gh pr merge`` raced
with an intervening merge to main — was parked at PR_HUMAN_NEEDED (or
left idling at APPROVED with ``trigger=""``). The handler must fire
``approved_to_rebasing`` in both cases so ``cai-rebase`` picks the PR
up on the next tick, resolves the conflict, and the merge flow
continues automatically.

Other ``gh pr merge`` failures (branch protection, missing required
checks, permissions) must keep the existing ``approved_to_human``
routing — only CONFLICTING re-routes to rebase.
"""
import json
import os
import sys
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cai_lib.actions import merge as merge_mod
from cai_lib.dispatcher import HandlerResult
from cai_lib.fsm import PRState


def _pr(number: int = 1210, mergeable: str = "MERGEABLE") -> dict:
    return {
        "number": number,
        "title": "auto-improve: example",
        "headRefOid": "398fd8ea965bbd364205faa1a4f6ff04bf983004",
        "headRefName": f"auto-improve/{number}-example",
        "labels": [{"name": "pr:approved"}],
        "mergeable": mergeable,
        "mergeStateStatus": "CLEAN" if mergeable == "MERGEABLE" else "DIRTY",
        "comments": [],
        "reviews": [],
        "state": "OPEN",
        "mergedAt": None,
        "createdAt": "2026-04-23T11:59:47Z",
    }


class TestEntryTimeConflictingRoutesToRebasing(unittest.TestCase):
    """Safety filter 2: PR already CONFLICTING on entry → REBASING."""

    def test_conflicting_pr_routes_to_rebasing(self):
        pr = _pr(mergeable="CONFLICTING")
        with patch.object(merge_mod, "log_run"):
            result = merge_mod.handle_merge(pr)
        self.assertIsInstance(result, HandlerResult)
        self.assertEqual(result.trigger, "approved_to_rebasing")


class TestPostMergeFailureConflictingRoutesToRebasing(unittest.TestCase):
    """Race path: ``gh pr merge`` fails AND the PR is now CONFLICTING."""

    def _invoke(self, post_merge_mergeable: str,
                ) -> tuple[HandlerResult, MagicMock]:
        pr = _pr(mergeable="MERGEABLE")

        # ``gh pr merge`` fails; all other ``gh`` calls succeed.
        def run_side_effect(args, **_kwargs):
            result = MagicMock()
            result.stdout = ""
            result.stderr = ""
            result.returncode = 0
            if args[:3] == ["gh", "pr", "merge"]:
                result.returncode = 1
                result.stderr = "merge conflict in cai_lib/subprocess_utils.py"
            return result

        run_mock = MagicMock(side_effect=run_side_effect)

        # Model verdict: high-confidence merge.
        claude_mock = MagicMock()
        claude_mock.return_value.returncode = 0
        claude_mock.return_value.stdout = json.dumps({
            "confidence": "high",
            "action": "merge",
            "reasoning": "looks good",
        })
        claude_mock.return_value.stderr = ""

        # gh json: issue carries :pr-open; post-merge pr view returns
        # the controlled ``mergeable`` value.
        def gh_json_side_effect(args):
            if "issue" in args and "view" in args:
                return {
                    "number": 1210,
                    "title": "auto-improve: example",
                    "labels": [{"name": "auto-improve:pr-open"}],
                    "state": "OPEN",
                    "body": "",
                }
            if "pr" in args and "view" in args:
                # Two distinct pr view calls happen — one inside the
                # pre-merge CI check (statusCheckRollup), one after the
                # failed merge (mergeable). Return the right shape for
                # either query.
                if "mergeable" in args:
                    return {"mergeable": post_merge_mergeable}
                return {"statusCheckRollup": []}
            return {}

        gh_json_mock = MagicMock(side_effect=gh_json_side_effect)

        with patch.object(merge_mod, "_run", run_mock), \
             patch.object(merge_mod, "_run_claude_p", claude_mock), \
             patch.object(merge_mod, "_gh_json", gh_json_mock), \
             patch.object(merge_mod, "_git", MagicMock()), \
             patch.object(merge_mod, "_filter_comments_with_haiku",
                          MagicMock(return_value=[])), \
             patch.object(merge_mod, "_fetch_review_comments",
                          MagicMock(return_value=[])), \
             patch.object(merge_mod, "_issue_has_label",
                          MagicMock(return_value=False)), \
             patch.object(merge_mod, "_set_labels",
                          MagicMock(return_value=True)), \
             patch.object(merge_mod, "fire_trigger",
                          MagicMock(return_value=True)), \
             patch.object(merge_mod, "get_pr_state",
                          return_value=PRState.APPROVED), \
             patch.object(merge_mod, "log_run", MagicMock()):
            result = merge_mod.handle_merge(pr)

        self.assertIsInstance(result, HandlerResult)
        return result, run_mock

    def test_post_merge_conflicting_routes_to_rebasing(self):
        result, _run_mock = self._invoke(post_merge_mergeable="CONFLICTING")
        self.assertEqual(result.trigger, "approved_to_rebasing")

    def test_post_merge_non_conflicting_still_parks_as_human(self):
        """Other ``gh pr merge`` failures (branch protection, missing
        required checks, permissions) must keep the human-needed
        routing — only CONFLICTING re-routes to rebase."""
        result, _run_mock = self._invoke(post_merge_mergeable="MERGEABLE")
        self.assertEqual(result.trigger, "approved_to_human")

    def test_post_merge_unknown_mergeable_still_parks_as_human(self):
        """If the re-query returns UNKNOWN or empty, fall back to
        human-needed rather than speculatively rebasing."""
        result, _run_mock = self._invoke(post_merge_mergeable="UNKNOWN")
        self.assertEqual(result.trigger, "approved_to_human")


if __name__ == "__main__":
    unittest.main()
