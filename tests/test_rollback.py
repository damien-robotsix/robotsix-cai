"""Tests for _rollback_stale_in_progress immediate=True/False behaviour."""
import sys
import os
import unittest
from datetime import datetime, timezone, timedelta
from unittest.mock import patch, MagicMock

# Ensure the repo root is on the import path so `import cai` works
# regardless of how the test runner is invoked.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cai


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

    def _run_rollback(self, immediate, issues_by_label):
        """Run _rollback_stale_in_progress with mocked gh calls."""

        def fake_gh_json(args, **kwargs):
            # Extract the --label argument
            label = args[args.index("--label") + 1]
            return issues_by_label.get(label, [])

        with patch.object(cai, "_gh_json", side_effect=fake_gh_json), \
             patch.object(cai, "_set_labels", return_value=True), \
             patch.object(cai, "log_run"), \
             patch.object(cai, "LOG_PATH", MagicMock(exists=lambda: False)):
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
            },
        )
        nums = {i["number"] for i in result}
        self.assertIn(401, nums, "7h-old :in-progress should be rolled back (TTL=6h)")
        self.assertIn(402, nums, "2h-old :revising should be rolled back (TTL=1h)")


if __name__ == "__main__":
    unittest.main()
