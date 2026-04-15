"""Tests for cai_lib.actions.pr_bounce.handle_pr_bounce.

Covers the three branches:
  1. Open linked PR exists → calls dispatch_pr.
  2. No open PR + closed-merged PR found → applies pr_to_merged.
  3. No open PR + closed-unmerged PR found → applies pr_to_refined.
  4. No PR found at all → applies pr_to_human_needed.
"""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cai_lib.actions import pr_bounce


def _issue(number: int, label: str = "auto-improve:pr-open") -> dict:
    return {
        "number": number,
        "title": "t",
        "body": "b",
        "labels": [{"name": label}],
        "createdAt": "2024-01-01T00:00:00Z",
    }


class TestHandlePrBounce(unittest.TestCase):

    def test_open_pr_routes_to_dispatch_pr(self):
        issue = _issue(620)
        open_pr = {"number": 643, "headRefName": "auto-improve/620-foo"}
        with patch.object(pr_bounce, "_find_open_linked_pr", return_value=open_pr), \
             patch.object(pr_bounce, "_find_recent_closed_linked_pr") as closed_lookup, \
             patch.object(pr_bounce, "apply_transition") as apply_t, \
             patch("cai_lib.dispatcher.dispatch_pr", return_value=0) as dpr:
            rc = pr_bounce.handle_pr_bounce(issue)
        self.assertEqual(rc, 0)
        dpr.assert_called_once_with(643)
        closed_lookup.assert_not_called()
        apply_t.assert_not_called()

    def test_closed_merged_pr_advances_to_merged(self):
        issue = _issue(620)
        closed_pr = {
            "number": 643, "headRefName": "auto-improve/620-foo",
            "state": "MERGED", "mergedAt": "2024-01-02T00:00:00Z",
            "closedAt": "2024-01-02T00:00:00Z",
        }
        with patch.object(pr_bounce, "_find_open_linked_pr", return_value=None), \
             patch.object(pr_bounce, "_find_recent_closed_linked_pr",
                          return_value=closed_pr), \
             patch.object(pr_bounce, "apply_transition", return_value=True) as apply_t, \
             patch("cai_lib.dispatcher.dispatch_pr") as dpr:
            rc = pr_bounce.handle_pr_bounce(issue)
        self.assertEqual(rc, 0)
        dpr.assert_not_called()
        apply_t.assert_called_once()
        args, kwargs = apply_t.call_args
        self.assertEqual(args[0], 620)
        self.assertEqual(args[1], "pr_to_merged")

    def test_closed_unmerged_by_bot_reverts_to_refined(self):
        issue = _issue(644)
        closed_pr = {
            "number": 645, "headRefName": "auto-improve/644-foo",
            "state": "CLOSED", "mergedAt": None,
            "closedAt": "2024-01-02T00:00:00Z",
        }
        with patch.object(pr_bounce, "_find_open_linked_pr", return_value=None), \
             patch.object(pr_bounce, "_find_recent_closed_linked_pr",
                          return_value=closed_pr), \
             patch.object(pr_bounce, "_pr_close_actor", return_value="cai-bot"), \
             patch.object(pr_bounce, "_our_gh_login", return_value="cai-bot"), \
             patch.object(pr_bounce, "apply_transition", return_value=True) as apply_t, \
             patch("cai_lib.dispatcher.dispatch_pr") as dpr:
            rc = pr_bounce.handle_pr_bounce(issue)
        self.assertEqual(rc, 0)
        dpr.assert_not_called()
        apply_t.assert_called_once()
        self.assertEqual(apply_t.call_args[0][1], "pr_to_refined")

    def test_closed_unmerged_by_human_diverts_to_human_needed(self):
        issue = _issue(644)
        closed_pr = {
            "number": 645, "headRefName": "auto-improve/644-foo",
            "state": "CLOSED", "mergedAt": None,
            "closedAt": "2024-01-02T00:00:00Z",
        }
        with patch.object(pr_bounce, "_find_open_linked_pr", return_value=None), \
             patch.object(pr_bounce, "_find_recent_closed_linked_pr",
                          return_value=closed_pr), \
             patch.object(pr_bounce, "_pr_close_actor",
                          return_value="damien-robotsix"), \
             patch.object(pr_bounce, "_our_gh_login", return_value="cai-bot"), \
             patch.object(pr_bounce, "apply_transition", return_value=True) as apply_t, \
             patch("cai_lib.dispatcher.dispatch_pr") as dpr:
            rc = pr_bounce.handle_pr_bounce(issue)
        self.assertEqual(rc, 0)
        dpr.assert_not_called()
        apply_t.assert_called_once()
        self.assertEqual(apply_t.call_args[0][1], "pr_to_human_needed")

    def test_closed_unmerged_unknown_actor_diverts_to_human_needed(self):
        """When timeline lookup fails, default to human-needed (safer)."""
        issue = _issue(644)
        closed_pr = {
            "number": 645, "headRefName": "auto-improve/644-foo",
            "state": "CLOSED", "mergedAt": None,
            "closedAt": "2024-01-02T00:00:00Z",
        }
        with patch.object(pr_bounce, "_find_open_linked_pr", return_value=None), \
             patch.object(pr_bounce, "_find_recent_closed_linked_pr",
                          return_value=closed_pr), \
             patch.object(pr_bounce, "_pr_close_actor", return_value=None), \
             patch.object(pr_bounce, "_our_gh_login", return_value="cai-bot"), \
             patch.object(pr_bounce, "apply_transition", return_value=True) as apply_t, \
             patch("cai_lib.dispatcher.dispatch_pr") as dpr:
            rc = pr_bounce.handle_pr_bounce(issue)
        self.assertEqual(rc, 0)
        self.assertEqual(apply_t.call_args[0][1], "pr_to_human_needed")

    def test_no_pr_diverts_to_human_needed(self):
        issue = _issue(700)
        with patch.object(pr_bounce, "_find_open_linked_pr", return_value=None), \
             patch.object(pr_bounce, "_find_recent_closed_linked_pr",
                          return_value=None), \
             patch.object(pr_bounce, "apply_transition", return_value=True) as apply_t, \
             patch("cai_lib.dispatcher.dispatch_pr") as dpr:
            rc = pr_bounce.handle_pr_bounce(issue)
        self.assertEqual(rc, 0)
        dpr.assert_not_called()
        apply_t.assert_called_once()
        self.assertEqual(apply_t.call_args[0][1], "pr_to_human_needed")

    def test_apply_transition_failure_returns_one(self):
        issue = _issue(700)
        with patch.object(pr_bounce, "_find_open_linked_pr", return_value=None), \
             patch.object(pr_bounce, "_find_recent_closed_linked_pr",
                          return_value=None), \
             patch.object(pr_bounce, "apply_transition", return_value=False):
            rc = pr_bounce.handle_pr_bounce(issue)
        self.assertEqual(rc, 1)


if __name__ == "__main__":
    unittest.main()
