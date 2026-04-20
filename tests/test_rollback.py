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


def _make_lock_claim(age_hours, owner="test-instance", comment_id=1):
    """Build a fake cai-lock claim-comment dict ``age_hours`` old."""
    created = datetime.now(timezone.utc) - timedelta(hours=age_hours)
    return {
        "id": comment_id,
        "owner": owner,
        "created_at": created.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


class TestRollbackStaleInProgress(unittest.TestCase):

    def _run_rollback(self, immediate, issues_by_label, set_labels_mock=None,
                      lock_comments_by_number=None):
        """Run _rollback_stale_in_progress with mocked gh calls.

        ``lock_comments_by_number`` lets tests seed cai-lock claim
        comments per issue/PR: ``{number: [{"id": int, "owner": str,
        "created_at": "YYYY-MM-DDTHH:MM:SSZ"}, ...]}``. Lists are
        returned oldest-first (matching the real ``_list_lock_comments``
        contract). Omitted numbers see an empty list.
        """

        comments = lock_comments_by_number or {}

        def fake_list_lock_comments(number):
            return list(comments.get(number, []))

        def fake_gh_json(args, **kwargs):
            # The :locked rollback path also calls
            # `gh api /repos/.../issues/<n>/comments` for claim-comment
            # deletion after a successful label strip. Treat as empty so
            # the deletion branch no-ops in tests.
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
             patch("cai_lib.watchdog._list_lock_comments",
                   side_effect=fake_list_lock_comments), \
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
            lock_comments_by_number={
                701: [_make_lock_claim(age_hours=cai._STALE_LOCKED_HOURS + 0.5)],
            },
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

    def test_rollback_locked_10min_not_rolled_back(self):
        """A 10-minute-old :locked issue must NOT be rolled back without immediate=True.

        This is the exact scenario observed live (age ~0.1h against TTL=1h):
        cmd_cycle was calling _rollback_stale_in_progress(immediate=True),
        which bypassed TTLs and killed active handlers.  With immediate=False
        (the correct call from cmd_cycle), a 10-min-old lock must survive.
        """
        locked_issue = _make_issue(811, cai.LABEL_LOCKED, age_hours=0.1)  # ~6 min
        result = self._run_rollback(
            immediate=False,
            issues_by_label={
                cai.LABEL_IN_PROGRESS: [],
                cai.LABEL_REVISING: [],
                cai.LABEL_APPLYING: [],
                cai.LABEL_LOCKED: [locked_issue],
            },
            lock_comments_by_number={
                811: [_make_lock_claim(age_hours=0.1)],
            },
        )
        nums = {i["number"] for i in result}
        self.assertNotIn(811, nums,
                         "0.1h-old :locked must NOT be rolled back "
                         f"(TTL={cai._STALE_LOCKED_HOURS}h) — "
                         "cmd_cycle must use TTL-based path, not immediate=True")

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
            lock_comments_by_number={
                801: [_make_lock_claim(age_hours=cai._STALE_LOCKED_HOURS / 2)],
            },
        )
        nums = {i["number"] for i in result}
        self.assertNotIn(801, nums,
                         f"fresh :locked (age < {cai._STALE_LOCKED_HOURS}h) "
                         "must NOT be rolled back")

    def test_rollback_locked_uses_claim_comment_over_stale_updated_at(self):
        """:locked rollback age must come from the oldest cai-lock comment.

        Regression for the production bug where issues kept a :locked label
        for hours because GitHub ``updatedAt`` was bumped by each cycle's
        losing ``_acquire_remote_lock`` race (post+delete of a claim
        comment) and by CI check-runs. The watchdog was using ``updatedAt``
        as the freshness fallback and never crossed the TTL threshold.
        """
        # Issue looks "fresh" via updatedAt (5 min), but the actual cai-lock
        # claim comment is 4 hours old → must be rolled back.
        locked_issue = _make_issue(
            851, cai.LABEL_LOCKED, age_hours=5 / 60.0)
        result = self._run_rollback(
            immediate=False,
            issues_by_label={
                cai.LABEL_IN_PROGRESS: [],
                cai.LABEL_REVISING: [],
                cai.LABEL_APPLYING: [],
                cai.LABEL_LOCKED: [locked_issue],
            },
            lock_comments_by_number={
                851: [_make_lock_claim(
                    age_hours=cai._STALE_LOCKED_HOURS + 3.0)],
            },
        )
        nums = {i["number"] for i in result}
        self.assertIn(
            851, nums,
            "a :locked issue with a claim comment older than "
            f"{cai._STALE_LOCKED_HOURS}h must be rolled back even when "
            "updatedAt is fresh (production bug — updatedAt is tainted "
            "by losing acquire races)",
        )

    def test_rollback_locked_ignores_stale_updated_at_when_claim_fresh(self):
        """A fresh claim comment must protect a :locked label even if ``updatedAt`` is old.

        Inverse of the regression: a healthy, actively-held lock whose
        issue hasn't been touched for a while (e.g. a long-running
        handler with no label churn) must NOT be rolled back.
        """
        locked_issue = _make_issue(
            852, cai.LABEL_LOCKED,
            age_hours=cai._STALE_LOCKED_HOURS + 10,  # stale updatedAt
        )
        result = self._run_rollback(
            immediate=False,
            issues_by_label={
                cai.LABEL_IN_PROGRESS: [],
                cai.LABEL_REVISING: [],
                cai.LABEL_APPLYING: [],
                cai.LABEL_LOCKED: [locked_issue],
            },
            lock_comments_by_number={
                852: [_make_lock_claim(age_hours=0.05)],  # ~3 min old
            },
        )
        nums = {i["number"] for i in result}
        self.assertNotIn(
            852, nums,
            "a :locked issue with a fresh claim comment must NOT be "
            "rolled back even if updatedAt is hours old",
        )

    def test_rollback_locked_uses_oldest_claim_when_many(self):
        """When multiple claim comments exist, the oldest wins (protocol contract)."""
        locked_issue = _make_issue(853, cai.LABEL_LOCKED, age_hours=0.1)
        result = self._run_rollback(
            immediate=False,
            issues_by_label={
                cai.LABEL_IN_PROGRESS: [],
                cai.LABEL_REVISING: [],
                cai.LABEL_APPLYING: [],
                cai.LABEL_LOCKED: [locked_issue],
            },
            lock_comments_by_number={
                # Oldest-first (contract of _list_lock_comments).
                853: [
                    _make_lock_claim(
                        age_hours=cai._STALE_LOCKED_HOURS + 2,
                        comment_id=1, owner="old-owner",
                    ),
                    _make_lock_claim(age_hours=0.02, comment_id=2,
                                     owner="fresh-loser"),
                ],
            },
        )
        nums = {i["number"] for i in result}
        self.assertIn(
            853, nums,
            "rollback must use the OLDEST claim-comment timestamp — a "
            "fresh losing-race claim must not protect a stale winner",
        )

    def test_rollback_locked_orphan_label_stripped(self):
        """:locked label with NO cai-lock comment is an anomaly → always strip."""
        locked_issue = _make_issue(854, cai.LABEL_LOCKED, age_hours=0.01)
        result = self._run_rollback(
            immediate=False,
            issues_by_label={
                cai.LABEL_IN_PROGRESS: [],
                cai.LABEL_REVISING: [],
                cai.LABEL_APPLYING: [],
                cai.LABEL_LOCKED: [locked_issue],
            },
            lock_comments_by_number={},  # no claims for #854
        )
        nums = {i["number"] for i in result}
        self.assertIn(
            854, nums,
            "orphan :locked (label without claim comment) must be "
            "rolled back regardless of age — the acquire protocol "
            "guarantees label+comment are posted together, so a label "
            "alone means a crashed/manual anomaly",
        )


def _make_pr(number, label, age_hours):
    """Build a minimal fake PR dict with updatedAt set to age_hours ago."""
    updated = datetime.now(timezone.utc) - timedelta(hours=age_hours)
    return {
        "number": number,
        "title": f"Test PR {number}",
        "updatedAt": updated.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "createdAt": updated.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "labels": [{"name": label}],
    }


class TestRollbackStalePrLocks(unittest.TestCase):

    def _run_pr_rollback(self, immediate, prs, set_pr_labels_mock=None,
                         lock_comments_by_number=None):
        """Run _rollback_stale_pr_locks with mocked gh + label calls.

        ``lock_comments_by_number`` seeds cai-lock claim comments per PR
        (same shape as the issue-side helper).
        """

        comments = lock_comments_by_number or {}

        def fake_list_lock_comments(number):
            return list(comments.get(number, []))

        def fake_gh_json(args, **kwargs):
            # Cai-lock claim comment scan — return empty list.
            if args and args[0] == "api":
                return []
            # gh pr list --label ... → return the seeded PRs.
            if args and args[0] == "pr" and args[1] == "list":
                return prs
            return []

        spl = (set_pr_labels_mock if set_pr_labels_mock is not None
               else MagicMock(return_value=True))

        with patch("cai_lib.watchdog._gh_json", side_effect=fake_gh_json), \
             patch("cai_lib.watchdog._set_pr_labels", spl), \
             patch("cai_lib.watchdog._delete_issue_comment", return_value=True), \
             patch("cai_lib.watchdog._list_lock_comments",
                   side_effect=fake_list_lock_comments), \
             patch("cai_lib.watchdog.log_run"):
            return cai._rollback_stale_pr_locks(immediate=immediate)

    def test_stale_pr_lock_rolled_back(self):
        """A PR older than _STALE_LOCKED_HOURS gets its :locked stripped."""
        pr = _make_pr(901, cai.LABEL_LOCKED,
                      age_hours=cai._STALE_LOCKED_HOURS + 0.5)
        spl_mock = MagicMock(return_value=True)
        result = self._run_pr_rollback(
            immediate=False, prs=[pr], set_pr_labels_mock=spl_mock,
            lock_comments_by_number={
                901: [_make_lock_claim(age_hours=cai._STALE_LOCKED_HOURS + 0.5)],
            },
        )
        nums = {p["number"] for p in result}
        self.assertIn(
            901, nums,
            f"{cai._STALE_LOCKED_HOURS + 0.5}h-old :locked PR should be "
            f"rolled back (TTL={cai._STALE_LOCKED_HOURS}h)",
        )
        # Verify _set_pr_labels was called with remove=[LABEL_LOCKED] and
        # no add=.
        called = False
        for call in spl_mock.call_args_list:
            kwargs = call.kwargs
            if kwargs.get("remove") == [cai.LABEL_LOCKED]:
                called = True
                self.assertFalse(
                    kwargs.get("add"),
                    "watchdog must not add an FSM state label when "
                    "rolling back :locked on a PR",
                )
        self.assertTrue(
            called,
            "watchdog must call _set_pr_labels with remove=[LABEL_LOCKED]",
        )

    def test_fresh_pr_lock_not_rolled_back(self):
        """PRs within _STALE_LOCKED_HOURS must NOT be rolled back."""
        pr = _make_pr(902, cai.LABEL_LOCKED,
                      age_hours=cai._STALE_LOCKED_HOURS / 2)
        result = self._run_pr_rollback(
            immediate=False, prs=[pr],
            lock_comments_by_number={
                902: [_make_lock_claim(age_hours=cai._STALE_LOCKED_HOURS / 2)],
            },
        )
        nums = {p["number"] for p in result}
        self.assertNotIn(
            902, nums,
            f"fresh :locked PR (age < {cai._STALE_LOCKED_HOURS}h) must NOT "
            "be rolled back",
        )

    def test_pr_lock_uses_claim_comment_over_stale_updated_at(self):
        """PR :locked rollback must read the claim comment, not ``updatedAt``.

        Production regression: PR #938 kept :locked for 4+ hours because
        GitHub bumped ``updatedAt`` for CI check-runs and losing
        lock-acquire races, while the real lock acquisition was hours old.
        """
        # updatedAt looks fresh (3 min), claim comment is 4h old.
        pr = _make_pr(904, cai.LABEL_LOCKED, age_hours=3 / 60.0)
        spl_mock = MagicMock(return_value=True)
        result = self._run_pr_rollback(
            immediate=False, prs=[pr], set_pr_labels_mock=spl_mock,
            lock_comments_by_number={
                904: [_make_lock_claim(
                    age_hours=cai._STALE_LOCKED_HOURS + 3.0)],
            },
        )
        nums = {p["number"] for p in result}
        self.assertIn(
            904, nums,
            "PR rollback must use the oldest cai-lock claim comment's "
            "created_at, not the PR's updatedAt (tainted by CI and "
            "losing-race post/delete churn)",
        )

    def test_pr_lock_orphan_label_stripped(self):
        """PR :locked label with no claim comment is an anomaly → strip."""
        pr = _make_pr(905, cai.LABEL_LOCKED, age_hours=0.01)
        result = self._run_pr_rollback(
            immediate=False, prs=[pr],
            lock_comments_by_number={},
        )
        nums = {p["number"] for p in result}
        self.assertIn(
            905, nums,
            "orphan PR :locked (no claim comment) must be rolled back "
            "regardless of age",
        )

    def test_immediate_true_rolls_back_fresh_pr(self):
        """immediate=True must roll back even fresh :locked PRs (restart)."""
        pr = _make_pr(903, cai.LABEL_LOCKED, age_hours=0.01)
        result = self._run_pr_rollback(immediate=True, prs=[pr])
        nums = {p["number"] for p in result}
        self.assertIn(
            903, nums,
            "immediate=True must roll back :locked PRs regardless of age",
        )


if __name__ == "__main__":
    unittest.main()
