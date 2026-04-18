"""Tests for _retroactive_no_action_sweep's state-reason guard.

Covers the root cause fixed for #862: `gh issue close --comment ...`
silently drops the comment when the issue is already closed, so the
sweep's own marker is not guaranteed to be present. The guard must
therefore rely on GitHub's native `stateReason == NOT_PLANNED` field
rather than a comment-substring match.
"""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cai_lib import cmd_agents  # noqa: E402


def _issue(number, state_reason, comments=None, labels=None):
    return {
        "number": number,
        "title": f"Issue {number}",
        "labels": [{"name": lbl} for lbl in (labels or [])],
        "closedAt": "2026-04-18T12:00:00Z",
        "stateReason": state_reason,
        "comments": comments or [],
    }


class TestRetroactiveSweepStateReason(unittest.TestCase):

    def test_already_not_planned_is_skipped_even_without_marker(self):
        """Issues already NOT_PLANNED must be skipped even when the
        marker comment is absent (reproduces the #862 regression)."""
        issues = [_issue(855, "NOT_PLANNED", comments=[])]
        closed_numbers: list[int] = []

        def fake_close(num, comment, log_prefix="cai"):
            closed_numbers.append(num)
            return True

        with patch.object(cmd_agents, "_gh_json", return_value=issues), \
                patch.object(cmd_agents, "close_issue_not_planned",
                             side_effect=fake_close), \
                patch.object(cmd_agents, "log_run"):
            swept = cmd_agents._retroactive_no_action_sweep()

        self.assertEqual(swept, [])
        self.assertEqual(closed_numbers, [])

    def test_completed_without_terminal_label_is_swept(self):
        """A COMPLETED issue without merged/solved labels must be
        swept (the real target of the retroactive sweep)."""
        issues = [_issue(900, "COMPLETED", comments=[], labels=[])]
        closed_numbers: list[int] = []

        def fake_close(num, comment, log_prefix="cai"):
            closed_numbers.append(num)
            return True

        with patch.object(cmd_agents, "_gh_json", return_value=issues), \
                patch.object(cmd_agents, "close_issue_not_planned",
                             side_effect=fake_close), \
                patch.object(cmd_agents, "log_run"):
            swept = cmd_agents._retroactive_no_action_sweep()

        self.assertEqual(len(swept), 1)
        self.assertEqual(swept[0]["number"], 900)
        self.assertEqual(closed_numbers, [900])

    def test_merged_label_still_wins(self):
        """Issues with a terminal lifecycle label (merged/solved)
        must be skipped regardless of stateReason."""
        issues = [
            _issue(910, "COMPLETED", comments=[],
                   labels=[cmd_agents.LABEL_MERGED]),
            _issue(911, "COMPLETED", comments=[],
                   labels=[cmd_agents.LABEL_SOLVED]),
        ]

        with patch.object(cmd_agents, "_gh_json", return_value=issues), \
                patch.object(cmd_agents, "close_issue_not_planned") as closer, \
                patch.object(cmd_agents, "log_run"):
            swept = cmd_agents._retroactive_no_action_sweep()

        self.assertEqual(swept, [])
        closer.assert_not_called()

    def test_comment_marker_still_honored_as_backup(self):
        """Even when stateReason is COMPLETED, a prior marker comment
        must still short-circuit (kept as a defense in depth)."""
        marker = ("Retroactively closing as **not planned** — issue "
                  "was closed without a terminal lifecycle label.")
        issues = [_issue(920, "COMPLETED",
                         comments=[{"body": marker,
                                    "author": {"login": "cai-bot"}}])]

        with patch.object(cmd_agents, "_gh_json", return_value=issues), \
                patch.object(cmd_agents, "close_issue_not_planned") as closer, \
                patch.object(cmd_agents, "log_run"):
            swept = cmd_agents._retroactive_no_action_sweep()

        self.assertEqual(swept, [])
        closer.assert_not_called()


if __name__ == "__main__":
    unittest.main()
