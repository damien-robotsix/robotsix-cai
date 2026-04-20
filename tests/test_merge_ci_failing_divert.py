"""Tests that handle_merge diverts APPROVED → CI_FAILING on red checks.

Regression guard for the gap where the merge handler used to log "has
failed CI checks; skipping" and return 0 with no state change, leaving
the PR stuck at ``pr:approved`` (dispatcher then treated it as
"blocked, moving on"). The handler must apply
``approved_to_ci_failing`` so ``handle_fix_ci`` picks the PR up.
"""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cai_lib.actions import merge as merge_mod
from cai_lib.fsm import PRState


def _pr(number: int) -> dict:
    return {
        "number": number,
        "title": "test",
        "headRefOid": "b9866ebbbe1884a6fc8b0bcf41c426b20c70dc58",
        "headRefName": f"auto-improve/{number}-something",
        "labels": [{"name": "pr:approved"}],
        "mergeable": "MERGEABLE",
        "mergeStateStatus": "CLEAN",
        "comments": [],
        "state": "OPEN",
        "mergedAt": None,
    }


class TestHandleMergeDivertsOnFailedCI(unittest.TestCase):

    def test_failed_check_triggers_approved_to_ci_failing(self):
        pr = _pr(1057)
        applied: list[tuple[int, str]] = []

        def fake_apply(pr_number, transition_name, **kw):
            applied.append((pr_number, transition_name))
            return True

        issue_payload = {
            "labels": [{"name": "auto-improve:pr-open"}],
            "state": "OPEN",
        }
        pr_detail_payload = {
            "statusCheckRollup": [
                {"name": "regenerate-docs", "conclusion": "FAILURE",
                 "status": "COMPLETED"},
            ],
        }

        def fake_gh_json(args):
            if "issue" in args and "view" in args:
                return issue_payload
            if "pr" in args and "view" in args:
                return pr_detail_payload
            return {}

        with patch.object(merge_mod, "_gh_json", side_effect=fake_gh_json), \
             patch.object(merge_mod, "apply_pr_transition",
                          side_effect=fake_apply), \
             patch.object(merge_mod, "get_pr_state",
                          return_value=PRState.APPROVED), \
             patch.object(merge_mod, "_filter_comments_with_haiku",
                          return_value=[]), \
             patch.object(merge_mod, "_fetch_review_comments",
                          return_value=[]), \
             patch.object(merge_mod, "log_run"):
            rc = merge_mod.handle_merge(pr)

        self.assertEqual(rc, 0)
        self.assertEqual(applied, [(1057, "approved_to_ci_failing")])


if __name__ == "__main__":
    unittest.main()
