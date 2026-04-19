"""Tests for _rollback_stale_in_progress immediate=True/False behaviour."""
import sys
import os
import unittest
from datetime import datetime, timezone, timedelta
from unittest.mock import patch, MagicMock

# Ensure the repo root is on the import path so imports work
# regardless of how the test runner is invoked.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cai_lib as cai


def _make_issue(number, label, age_hours):
    """Build a minimal fake issue dict with updatedAt set to age_hours ago."""
    updated = datetime.now(timezone.utc) - timedelta(hours=age_hours)
    return {
        "number": number,
        "title": f"Test issue {number}",
        "updatedAt": updated.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "createdAt": updated.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "labels": [{"name": label}],
    }


class TestRollbackStaleInProgress(unittest.TestCase):

    def _run_rollback(self, immediate, issues_by_label, set_labels_mock=None):
        """Run _rollback_stale_in_progress with mocked gh calls."""

        def fake_gh_json(args, **kwargs):
            # The :locked rollback path also calls
            # `gh api /repos/.../issues/<n>/comments` to find cai-lock
            # claims. Treat that call as "no comments".
            if args and args[0] == "api":
                return []
            # Extract the --label argument from `gh issue list ...`.
            if "--label" in args:
                label = args[args.index("--label") + 1]
                return issues_by_label.get(label, [])
            return []

        sl = set_labels_mock if set_labels_mock is not None else MagicMock(return_value=True)

        with patch("cai_lib.watchdog._gh_json", side_effect=fake_gh_json), \
             patch("cai_lib.watchdog._set_labels", sl), \
             patch("cai_lib.watchdog._delete_issue_comment", return_value=True), \
             patch("cai_lib.watchdog.log_run"), \
             patch("cai_lib.watchdog.LOG_PATH", MagicMock(exists=lambda: False)):
            return cai._rollback_stale_in_progress(immediate=immediate)

    def test_immediate_true_rolls_back_all(self):
        """immediate=True should roll back all locked issues regardless of age."""
        # 1-hour-old :in-progress (TTL is 6h — normally would NOT be rolled back)
        ip_issue = _make_issue(101, cai.LABEL_IN_PROGRESS, age_hours=1)
        # 30-minute-old :revising (TTL is 1h — normally would NOT be rolled back)
        rev_issue = _make_issue(102, cai.LABEL_REVISING, age_hours=0.5)

        result = self._run_rollback(
            immediate=True,
            issues_by_label={
                cai.LABEL_IN_PROGRESS: [ip_issue],
                cai.LABEL_REVISING: [rev_issue],
            },
        )
        nums = {i["number"] for i in result}
        self.assertIn(101, nums, "1h-old :in-progress should be rolled back when immediate=True")
        self.assertIn(102, nums, "0.5h-old :revising should be rolled back when immediate=True")

    def test_immediate_false_respects_ttl_in_progress(self):
        """immediate=False should NOT roll back :in-progress issues within the 6h TTL."""
        ip_issue = _make_issue(201, cai.LABEL_IN_PROGRESS, age_hours=1)

        result = self._run_rollback(
            immediate=False,
            issues_by_label={
                cai.LABEL_IN_PROGRESS: [ip_issue],
                cai.LABEL_REVISING: [],
            },
        )
        nums = {i["number"] for i in result}
        self.assertNotIn(201, nums, "1h-old :in-progress should NOT be rolled back when immediate=False (TTL=6h)")

    def test_immediate_false_respects_ttl_revising(self):
        """immediate=False should NOT roll back :revising issues within the 1h TTL."""
        rev_issue = _make_issue(301, cai.LABEL_REVISING, age_hours=0.5)

        result = self._run_rollback(
            immediate=False,
            issues_by_label={
                cai.LABEL_IN_PROGRESS: [],
                cai.LABEL_REVISING: [rev_issue],
            },
        )
        nums = {i["number"] for i in result}
        self.assertNotIn(301, nums, "0.5h-old :revising should NOT be rolled back when immediate=False (TTL=1h)")

    def test_immediate_false_rolls_back_stale(self):
        """immediate=False should roll back issues that exceed their TTL."""
        # 7-hour-old :in-progress (TTL is 6h — should be rolled back)
        ip_issue = _make_issue(401, cai.LABEL_IN_PROGRESS, age_hours=7)
        # 2-hour-old :revising (TTL is 1h — should be rolled back)
        rev_issue = _make_issue(402, cai.LABEL_REVISING, age_hours=2)

        result = self._run_rollback(
            immediate=False,
            issues_by_label={
                cai.LABEL_IN_PROGRESS: [ip_issue],
                cai.LABEL_REVISING: [rev_issue],
                cai.LABEL_APPLYING: [],
            },
        )
        nums = {i["number"] for i in result}
        self.assertIn(401, nums, "7h-old :in-progress should be rolled back (TTL=6h)")
        self.assertIn(402, nums, "2h-old :revising should be rolled back (TTL=1h)")

    def test_rollback_applying_stale(self):
        """immediate=False should roll back :applying issues that exceed the 2h TTL."""
        # 3-hour-old :applying (TTL is 2h — should be rolled back)
        applying_issue = _make_issue(501, cai.LABEL_APPLYING, age_hours=3)

        result = self._run_rollback(
            immediate=False,
            issues_by_label={
                cai.LABEL_IN_PROGRESS: [],
                cai.LABEL_REVISING: [],
                cai.LABEL_APPLYING: [applying_issue],
            },
        )
        nums = {i["number"] for i in result}
        self.assertIn(501, nums,
                      "3h-old :applying should be rolled back (TTL=2h)")

    def test_rollback_applying_fresh(self):
        """immediate=False should NOT roll back :applying issues within the 2h TTL."""
        # 1-hour-old :applying (TTL is 2h — should NOT be rolled back)
        applying_issue = _make_issue(601, cai.LABEL_APPLYING, age_hours=1)

        result = self._run_rollback(
            immediate=False,
            issues_by_label={
                cai.LABEL_IN_PROGRESS: [],
                cai.LABEL_REVISING: [],
                cai.LABEL_APPLYING: [applying_issue],
            },
        )
        nums = {i["number"] for i in result}
        self.assertNotIn(601, nums,
                         "1h-old :applying should NOT be rolled back (TTL=2h)")

    def test_rollback_locked_stale(self):
        """:locked issues older than _STALE_LOCKED_HOURS get the lock stripped.

        The watchdog must NOT touch the FSM state label (:locked is
        orthogonal). Verify by checking that _set_labels was called
        with remove=[LABEL_LOCKED] and no add=.
        """
        locked_issue = _make_issue(701, cai.LABEL_LOCKED,
                                   age_hours=cai._STALE_LOCKED_HOURS + 0.5)
        sl_mock = MagicMock(return_value=True)
        result = self._run_rollback(
            immediate=False,
            issues_by_label={
                cai.LABEL_IN_PROGRESS: [],
                cai.LABEL_REVISING: [],
                cai.LABEL_APPLYING: [],
                cai.LABEL_LOCKED: [locked_issue],
            },
            set_labels_mock=sl_mock,
        )
        nums = {i["number"] for i in result}
        self.assertIn(701, nums,
                      f"{cai._STALE_LOCKED_HOURS + 0.5}h-old :locked should "
                      f"be rolled back (TTL={cai._STALE_LOCKED_HOURS}h)")
        # Verify the call removed only LABEL_LOCKED and did not add any
        # FSM state label.
        called = False
        for call in sl_mock.call_args_list:
            kwargs = call.kwargs
            if kwargs.get("remove") == [cai.LABEL_LOCKED]:
                called = True
                self.assertFalse(
                    kwargs.get("add"),
                    "watchdog must not add an FSM state label when "
                    "rolling back :locked (orthogonal lock)",
                )
        self.assertTrue(
            called,
            "watchdog must call _set_labels with remove=[LABEL_LOCKED]",
        )

    def test_rollback_locked_fresh(self):
        """:locked issues within the TTL window must NOT be rolled back."""
        # Half the TTL — well within the window.
        locked_issue = _make_issue(801, cai.LABEL_LOCKED,
                                   age_hours=cai._STALE_LOCKED_HOURS / 2)
        result = self._run_rollback(
            immediate=False,
            issues_by_label={
                cai.LABEL_IN_PROGRESS: [],
                cai.LABEL_REVISING: [],
                cai.LABEL_APPLYING: [],
                cai.LABEL_LOCKED: [locked_issue],
            },
        )
        nums = {i["number"] for i in result}
        self.assertNotIn(801, nums,
                         f"fresh :locked (age < {cai._STALE_LOCKED_HOURS}h) "
                         "must NOT be rolled back")


if __name__ == "__main__":
    unittest.main()
