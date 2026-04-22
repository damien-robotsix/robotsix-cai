"""Tests for cai_lib.actions.pr_bounce.handle_pr_bounce.

Covers the four branches of the recovery decision tree:
  1. Open linked PR exists → calls dispatch_pr (and returns its int rc).
  2. No open PR + closed-merged PR found → HandlerResult(pr_to_merged).
  3. No open PR + closed-unmerged PR found (bot vs human closer) →
     HandlerResult(pr_to_refined) or HandlerResult(pr_to_human_needed).
  4. No PR found at all → HandlerResult(pr_to_human_needed).
"""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cai_lib.actions import pr_bounce
from cai_lib.dispatcher import HandlerResult


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
             patch("cai_lib.dispatcher.dispatch_pr", return_value=0) as dpr:
            rc = pr_bounce.handle_pr_bounce(issue)
        self.assertEqual(rc, 0)
        dpr.assert_called_once_with(643)
        closed_lookup.assert_not_called()

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
             patch("cai_lib.dispatcher.dispatch_pr") as dpr:
            rc = pr_bounce.handle_pr_bounce(issue)
        self.assertIsInstance(rc, HandlerResult)
        self.assertEqual(rc.trigger, "pr_to_merged")
        dpr.assert_not_called()

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
             patch("cai_lib.dispatcher.dispatch_pr") as dpr:
            rc = pr_bounce.handle_pr_bounce(issue)
        self.assertIsInstance(rc, HandlerResult)
        self.assertEqual(rc.trigger, "pr_to_refined")
        dpr.assert_not_called()

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
             patch("cai_lib.dispatcher.dispatch_pr") as dpr:
            rc = pr_bounce.handle_pr_bounce(issue)
        self.assertIsInstance(rc, HandlerResult)
        self.assertEqual(rc.trigger, "pr_to_human_needed")
        self.assertTrue(rc.divert_reason)
        dpr.assert_not_called()

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
             patch("cai_lib.dispatcher.dispatch_pr") as dpr:
            rc = pr_bounce.handle_pr_bounce(issue)
        self.assertIsInstance(rc, HandlerResult)
        self.assertEqual(rc.trigger, "pr_to_human_needed")

    def test_no_pr_diverts_to_human_needed(self):
        issue = _issue(700)
        with patch.object(pr_bounce, "_find_open_linked_pr", return_value=None), \
             patch.object(pr_bounce, "_find_recent_closed_linked_pr",
                          return_value=None), \
             patch("cai_lib.dispatcher.dispatch_pr") as dpr:
            rc = pr_bounce.handle_pr_bounce(issue)
        self.assertIsInstance(rc, HandlerResult)
        self.assertEqual(rc.trigger, "pr_to_human_needed")
        self.assertTrue(rc.divert_reason)
        dpr.assert_not_called()


if __name__ == "__main__":
    unittest.main()
