"""Tests for cai_lib.dispatcher — the FSM-dispatcher architecture.

Covers:
- actionable_issue_states / actionable_pr_states registry shape
- dispatch_issue routing by FSM label
- dispatch_pr routing by FSM label
- dispatch_oldest_actionable picks oldest across issues+PRs
"""
import os
import sys
import unittest
from unittest.mock import patch, MagicMock

# Ensure the repo root is on the import path.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cai_lib import dispatcher
from cai_lib.fsm import IssueState, PRState


class TestActionableStateSets(unittest.TestCase):
    """The registries must cover the pipeline states (not terminal/parked)."""

    def test_actionable_issue_states(self):
        expected = {
            IssueState.RAISED,
            IssueState.TRIAGING,
            IssueState.REFINING,
            IssueState.NEEDS_EXPLORATION,
            IssueState.REFINED,
            IssueState.PLANNING,
            IssueState.PLANNED,
            IssueState.PLAN_APPROVED,
            IssueState.IN_PROGRESS,
            IssueState.PR,
            IssueState.MERGED,
        }
        self.assertEqual(dispatcher.actionable_issue_states(), expected)
        # HUMAN_NEEDED and SOLVED are explicitly not actionable.
        self.assertNotIn(IssueState.HUMAN_NEEDED, dispatcher.actionable_issue_states())
        self.assertNotIn(IssueState.SOLVED, dispatcher.actionable_issue_states())

    def test_actionable_pr_states(self):
        expected = {
            PRState.OPEN,
            PRState.REVIEWING_CODE,
            PRState.REVISION_PENDING,
            PRState.REVIEWING_DOCS,
            PRState.APPROVED,
            PRState.REBASING,
            PRState.CI_FAILING,
        }
        self.assertEqual(dispatcher.actionable_pr_states(), expected)
        self.assertNotIn(PRState.MERGED, dispatcher.actionable_pr_states())
        self.assertNotIn(PRState.PR_HUMAN_NEEDED, dispatcher.actionable_pr_states())


# ---------------------------------------------------------------------------
# dispatch_issue routing
# ---------------------------------------------------------------------------

def _issue(number: int, label: str | None, state: str = "OPEN",
           created_at: str = "2024-01-01T00:00:00Z") -> dict:
    labels = [{"name": label}] if label else []
    return {
        "number": number,
        "title": "t",
        "body": "b",
        "labels": labels,
        "createdAt": created_at,
        "comments": [],
        "state": state,
    }


def _pr(number: int, label: str | None, merged: bool = False,
        created_at: str = "2024-01-01T00:00:00Z") -> dict:
    labels = [{"name": label}] if label else []
    return {
        "number": number,
        "title": "t",
        "headRefName": "feature",
        "headRefOid": "deadbeef",
        "labels": labels,
        "state": "MERGED" if merged else "OPEN",
        "mergeable": "MERGEABLE",
        "merged": merged,
        "mergedAt": "2024-01-02T00:00:00Z" if merged else None,
        "comments": [],
        "reviews": [],
        "createdAt": created_at,
    }


class TestDispatchIssue(unittest.TestCase):
    """dispatch_issue fetches the issue and routes by FSM label."""

    def _run_routing(self, label: str, expected_state: IssueState):
        handler = MagicMock(return_value=0)
        handler.__name__ = "mock_handler"
        issue = _issue(42, label)
        # Force-rebuild the registry with our mock in place of the real handler.
        registry = {expected_state: handler}
        with patch.object(dispatcher, "_gh_json", return_value=issue), \
             patch.object(dispatcher, "_issue_registry", return_value=registry):
            rc = dispatcher.dispatch_issue(42)
        self.assertEqual(rc, 0)
        handler.assert_called_once_with(issue)

    def test_raised_routes_to_handle_triage(self):
        self._run_routing("auto-improve:raised", IssueState.RAISED)

    def test_refining_routes_to_handle_refine(self):
        self._run_routing("auto-improve:refining", IssueState.REFINING)

    def test_in_progress_routes_to_handle_implement(self):
        self._run_routing("auto-improve:in-progress", IssueState.IN_PROGRESS)

    def test_merged_routes_to_handle_confirm(self):
        self._run_routing("auto-improve:merged", IssueState.MERGED)

    def test_closed_issue_returns_zero_without_handler(self):
        handler = MagicMock()
        registry = {IssueState.REFINING: handler}
        issue = _issue(42, "auto-improve:refining", state="CLOSED")
        with patch.object(dispatcher, "_gh_json", return_value=issue), \
             patch.object(dispatcher, "_issue_registry", return_value=registry):
            rc = dispatcher.dispatch_issue(42)
        self.assertEqual(rc, 0)
        handler.assert_not_called()

    def test_no_fsm_label_returns_zero_without_handler(self):
        handler = MagicMock()
        registry = {IssueState.RAISED: handler}
        issue = _issue(42, None)
        with patch.object(dispatcher, "_gh_json", return_value=issue), \
             patch.object(dispatcher, "_issue_registry", return_value=registry):
            rc = dispatcher.dispatch_issue(42)
        self.assertEqual(rc, 0)
        handler.assert_not_called()


# ---------------------------------------------------------------------------
# dispatch_pr routing
# ---------------------------------------------------------------------------

class TestDispatchPR(unittest.TestCase):
    """dispatch_pr fetches the PR and routes by FSM label / merged flag."""

    def _run_routing(self, pr: dict, expected_state: PRState):
        handler = MagicMock(return_value=0)
        handler.__name__ = "mock_handler"
        registry = {expected_state: handler}
        with patch.object(dispatcher, "_gh_json", return_value=pr), \
             patch.object(dispatcher, "_pr_registry", return_value=registry):
            rc = dispatcher.dispatch_pr(pr["number"])
        self.assertEqual(rc, 0)
        handler.assert_called_once_with(pr)

    def test_reviewing_code_routes_to_handle_review_pr(self):
        self._run_routing(_pr(99, "pr:reviewing-code"), PRState.REVIEWING_CODE)

    def test_approved_routes_to_handle_merge(self):
        self._run_routing(_pr(99, "pr:approved"), PRState.APPROVED)

    def test_no_pipeline_label_routes_to_handle_open_to_review(self):
        self._run_routing(_pr(99, None), PRState.OPEN)

    def test_merged_pr_returns_zero_without_handler(self):
        handler = MagicMock()
        registry = {PRState.OPEN: handler, PRState.APPROVED: handler}
        pr = _pr(99, None, merged=True)
        with patch.object(dispatcher, "_gh_json", return_value=pr), \
             patch.object(dispatcher, "_pr_registry", return_value=registry):
            rc = dispatcher.dispatch_pr(99)
        self.assertEqual(rc, 0)
        handler.assert_not_called()

    def test_conflicting_pr_diverts_to_rebase(self):
        """mergeable=CONFLICTING overrides the pipeline label and routes to handle_rebase."""
        pr = _pr(99, "pr:reviewing-code")
        pr["mergeable"] = "CONFLICTING"
        rebase_handler = MagicMock(return_value=0)
        rebase_handler.__name__ = "handle_rebase"
        review_handler = MagicMock(return_value=0)
        review_handler.__name__ = "handle_review_pr"
        registry = {PRState.REVIEWING_CODE: review_handler}
        applied: list[tuple[int, str]] = []

        def fake_apply_pr_transition(pr_number, transition_name, **kw):
            applied.append((pr_number, transition_name))
            return True

        with patch.object(dispatcher, "_gh_json", return_value=pr), \
             patch.object(dispatcher, "_pr_registry", return_value=registry), \
             patch("cai_lib.fsm.apply_pr_transition",
                   side_effect=fake_apply_pr_transition), \
             patch("cai_lib.actions.rebase.handle_rebase",
                   side_effect=rebase_handler):
            rc = dispatcher.dispatch_pr(99)

        self.assertEqual(rc, 0)
        review_handler.assert_not_called()
        rebase_handler.assert_called_once_with(pr)
        self.assertEqual(applied, [(99, "reviewing_code_to_rebasing")])

    def test_dirty_merge_state_diverts_to_rebase(self):
        """mergeStateStatus=DIRTY also triggers the rebase divert."""
        pr = _pr(99, "pr:approved")
        pr["mergeable"] = "MERGEABLE"
        pr["mergeStateStatus"] = "DIRTY"
        rebase_handler = MagicMock(return_value=0)
        rebase_handler.__name__ = "handle_rebase"
        merge_handler = MagicMock(return_value=0)
        merge_handler.__name__ = "handle_merge"
        registry = {PRState.APPROVED: merge_handler}

        with patch.object(dispatcher, "_gh_json", return_value=pr), \
             patch.object(dispatcher, "_pr_registry", return_value=registry), \
             patch("cai_lib.fsm.apply_pr_transition", return_value=True), \
             patch("cai_lib.actions.rebase.handle_rebase",
                   side_effect=rebase_handler):
            rc = dispatcher.dispatch_pr(99)

        self.assertEqual(rc, 0)
        merge_handler.assert_not_called()
        rebase_handler.assert_called_once_with(pr)

    def test_rebasing_state_does_not_re_divert(self):
        """A PR already at REBASING runs handle_rebase normally without re-applying entry transition."""
        pr = _pr(99, "pr:rebasing")
        pr["mergeable"] = "CONFLICTING"
        rebase_handler = MagicMock(return_value=0)
        rebase_handler.__name__ = "handle_rebase"
        registry = {PRState.REBASING: rebase_handler}

        with patch.object(dispatcher, "_gh_json", return_value=pr), \
             patch.object(dispatcher, "_pr_registry", return_value=registry):
            rc = dispatcher.dispatch_pr(99)

        self.assertEqual(rc, 0)
        rebase_handler.assert_called_once_with(pr)


# ---------------------------------------------------------------------------
# dispatch_drain (and the dispatch_oldest_actionable alias)
# ---------------------------------------------------------------------------

class TestDispatchOldestActionable(unittest.TestCase):
    """Picks the oldest (by createdAt) across issues + PRs.

    These cover the *single-pick* selection by simulating an
    advancing handler (each dispatch removes the picked item from the
    pool snapshot, so the drain naturally empties after one pick).
    """

    def test_picks_oldest_across_issues_and_prs(self):
        # Two issues + one PR; issue #10 is oldest.
        issues = [
            {"number": 10, "createdAt": "2024-01-01T00:00:00Z",
             "labels": [{"name": "auto-improve:refining"}]},
        ]
        prs: list[dict] = []

        def fake_gh_json(cmd):
            if "issue" in cmd and "list" in cmd:
                return issues
            if "pr" in cmd and "list" in cmd:
                return prs
            raise AssertionError(f"unexpected _gh_json call: {cmd}")

        # Add a second issue and PR to the snapshot but make dispatch_issue
        # remove the dispatched item — so we observe which one was picked
        # first without having the drain process the rest.
        issues.append({"number": 20, "createdAt": "2024-02-01T00:00:00Z",
                       "labels": [{"name": "auto-improve:in-progress"}]})
        prs.append({"number": 99, "createdAt": "2024-01-15T00:00:00Z",
                    "labels": [{"name": "pr:reviewing-code"}],
                    "merged": False, "mergedAt": None})

        di_calls: list[int] = []

        def fake_di(n):
            di_calls.append(n)
            issues[:] = [i for i in issues if i["number"] != n]
            return 0

        dp_calls: list[int] = []

        def fake_dp(n):
            dp_calls.append(n)
            prs[:] = [p for p in prs if p["number"] != n]
            return 0

        with patch.object(dispatcher, "_gh_json", side_effect=fake_gh_json), \
             patch.object(dispatcher, "dispatch_issue", side_effect=fake_di), \
             patch.object(dispatcher, "dispatch_pr", side_effect=fake_dp):
            rc = dispatcher.dispatch_oldest_actionable()

        self.assertEqual(rc, 0)
        # Oldest first: issue #10 (Jan 1) before PR #99 (Jan 15) before issue #20 (Feb 1).
        self.assertEqual(di_calls, [10, 20])
        self.assertEqual(dp_calls, [99])

    def test_picks_oldest_when_pr_wins(self):
        issues = [
            {"number": 20, "createdAt": "2024-03-01T00:00:00Z",
             "labels": [{"name": "auto-improve:refining"}]},
        ]
        prs = [
            {"number": 99, "createdAt": "2024-01-15T00:00:00Z",
             "labels": [{"name": "pr:reviewing-code"}],
             "merged": False, "mergedAt": None},
        ]

        def fake_gh_json(cmd):
            if "issue" in cmd and "list" in cmd:
                return issues
            if "pr" in cmd and "list" in cmd:
                return prs
            raise AssertionError(f"unexpected _gh_json call: {cmd}")

        di_calls: list[int] = []

        def fake_di(n):
            di_calls.append(n)
            issues[:] = [i for i in issues if i["number"] != n]
            return 0

        dp_calls: list[int] = []

        def fake_dp(n):
            dp_calls.append(n)
            prs[:] = [p for p in prs if p["number"] != n]
            return 0

        with patch.object(dispatcher, "_gh_json", side_effect=fake_gh_json), \
             patch.object(dispatcher, "dispatch_issue", side_effect=fake_di), \
             patch.object(dispatcher, "dispatch_pr", side_effect=fake_dp):
            rc = dispatcher.dispatch_oldest_actionable()

        self.assertEqual(rc, 0)
        # Oldest first: PR #99 (Jan 15) before issue #20 (Mar 1).
        self.assertEqual(dp_calls, [99])
        self.assertEqual(di_calls, [20])

    def test_empty_pools_returns_zero(self):
        def fake_gh_json(cmd):
            return []

        with patch.object(dispatcher, "_gh_json", side_effect=fake_gh_json), \
             patch.object(dispatcher, "dispatch_issue") as di, \
             patch.object(dispatcher, "dispatch_pr") as dp:
            rc = dispatcher.dispatch_oldest_actionable()

        self.assertEqual(rc, 0)
        di.assert_not_called()
        dp.assert_not_called()


class TestDispatchDrain(unittest.TestCase):
    """dispatch_drain loops pick+dispatch until empty / loop-guard / cap."""

    def test_drains_multiple_distinct_targets(self):
        """Each iteration picks a different oldest until queue is empty."""
        # Simulate the queue shrinking each tick: handler "advances state"
        # (we drop the dispatched item from the next pool snapshot).
        remaining = [
            ("issue", 10, "2024-01-01T00:00:00Z", "auto-improve:refining"),
            ("issue", 20, "2024-01-02T00:00:00Z", "auto-improve:in-progress"),
            ("pr",    99, "2024-01-03T00:00:00Z", "pr:reviewing-code"),
        ]

        def snapshot_issues():
            return [
                {"number": n, "createdAt": ca, "labels": [{"name": lb}]}
                for k, n, ca, lb in remaining if k == "issue"
            ]

        def snapshot_prs():
            return [
                {"number": n, "createdAt": ca, "labels": [{"name": lb}],
                 "merged": False, "mergedAt": None}
                for k, n, ca, lb in remaining if k == "pr"
            ]

        def fake_gh_json(cmd):
            if "issue" in cmd and "list" in cmd:
                return snapshot_issues()
            if "pr" in cmd and "list" in cmd:
                return snapshot_prs()
            raise AssertionError(f"unexpected _gh_json call: {cmd}")

        di_calls: list[int] = []
        dp_calls: list[int] = []

        def fake_di(n):
            di_calls.append(n)
            remaining[:] = [t for t in remaining if not (t[0] == "issue" and t[1] == n)]
            return 0

        def fake_dp(n):
            dp_calls.append(n)
            remaining[:] = [t for t in remaining if not (t[0] == "pr" and t[1] == n)]
            return 0

        with patch.object(dispatcher, "_gh_json", side_effect=fake_gh_json), \
             patch.object(dispatcher, "dispatch_issue", side_effect=fake_di), \
             patch.object(dispatcher, "dispatch_pr", side_effect=fake_dp):
            rc = dispatcher.dispatch_drain()

        self.assertEqual(rc, 0)
        # Drained in oldest-first order, queue emptied.
        self.assertEqual(di_calls, [10, 20])
        self.assertEqual(dp_calls, [99])

    def test_target_dispatched_at_most_once_per_drain(self):
        """Each ``(kind, number)`` runs at most once per drain, even when
        the handler returns 0 and the pool never shrinks.

        Regression for the loop class where a routing handler
        (``pr_bounce``) or an idempotent no-op handler
        (``handle_merge`` short-circuiting on "already evaluated")
        returns 0 on a target whose underlying state never changes.
        Before per-drain dedup, this ran the full ``max_iter`` cap
        every tick; now the drain empties cleanly after one pass.
        """
        issues = [
            {"number": 10, "createdAt": "2024-01-01T00:00:00Z",
             "labels": [{"name": "auto-improve:refining"}]},
        ]

        def fake_gh_json(cmd):
            if "issue" in cmd and "list" in cmd:
                return issues
            return []

        with patch.object(dispatcher, "_gh_json", side_effect=fake_gh_json), \
             patch.object(dispatcher, "dispatch_issue", return_value=0) as di:
            rc = dispatcher.dispatch_drain(max_iter=10)

        self.assertEqual(rc, 0)
        # Exactly one call even though pool never shrinks and we gave
        # max_iter=10 headroom — per-drain dedup adds the target to the
        # skip set after the first dispatch so the picker returns None
        # on the next iteration.
        di.assert_called_once_with(10)

    def test_max_iter_cap(self):
        """A pool that keeps providing distinct targets stops at max_iter."""
        # Generate as many distinct issues as we want and never shrink.
        all_issues = [
            {"number": n, "createdAt": f"2024-01-{n:02d}T00:00:00Z",
             "labels": [{"name": "auto-improve:refining"}]}
            for n in range(1, 21)
        ]

        def fake_gh_json(cmd):
            if "issue" in cmd and "list" in cmd:
                return all_issues
            return []

        # dispatch_issue does NOT shrink the pool — but each call advances
        # the pool's "next oldest" via a counter so targets differ.
        # Simpler: give each issue a unique createdAt and remove it after dispatch.
        pool = list(all_issues)

        def fake_di(n):
            pool[:] = [i for i in pool if i["number"] != n]
            return 0

        def fake_gh_json2(cmd):
            if "issue" in cmd and "list" in cmd:
                return pool
            return []

        with patch.object(dispatcher, "_gh_json", side_effect=fake_gh_json2), \
             patch.object(dispatcher, "dispatch_issue", side_effect=fake_di) as di:
            rc = dispatcher.dispatch_drain(max_iter=3)

        self.assertEqual(rc, 0)
        # Cap stopped us at 3 dispatches even though more remain.
        self.assertEqual(di.call_count, 3)
        self.assertEqual(len(pool), 17)

    def test_returns_worst_exit_code(self):
        """If any handler returns non-zero, drain returns the worst code."""
        issues_pool = [
            {"number": 10, "createdAt": "2024-01-01T00:00:00Z",
             "labels": [{"name": "auto-improve:refining"}]},
            {"number": 20, "createdAt": "2024-01-02T00:00:00Z",
             "labels": [{"name": "auto-improve:in-progress"}]},
        ]

        def fake_gh_json(cmd):
            if "issue" in cmd and "list" in cmd:
                return issues_pool
            return []

        def fake_di(n):
            issues_pool[:] = [i for i in issues_pool if i["number"] != n]
            return 0 if n == 10 else 2

        with patch.object(dispatcher, "_gh_json", side_effect=fake_gh_json), \
             patch.object(dispatcher, "dispatch_issue", side_effect=fake_di):
            rc = dispatcher.dispatch_drain()

        self.assertEqual(rc, 2)


    def test_handler_exception_skips_target_and_continues_drain(self):
        """A crashing handler must not stall the queue — drain skips it and
        processes the rest of the actionable items (#657)."""
        pool = [
            {"number": 10, "createdAt": "2024-01-01T00:00:00Z",
             "labels": [{"name": "auto-improve:refining"}]},
            {"number": 20, "createdAt": "2024-01-02T00:00:00Z",
             "labels": [{"name": "auto-improve:in-progress"}]},
        ]

        def fake_gh_json(cmd):
            if "issue" in cmd and "list" in cmd:
                return pool
            return []

        calls: list[int] = []

        def fake_di(n):
            calls.append(n)
            if n == 10:
                raise RuntimeError("boom")
            pool[:] = [i for i in pool if i["number"] != n]
            return 0

        with patch.object(dispatcher, "_gh_json", side_effect=fake_gh_json), \
             patch.object(dispatcher, "dispatch_issue", side_effect=fake_di):
            rc = dispatcher.dispatch_drain()

        # Crash on #10 is recorded as worst_rc=1, but #20 still ran.
        self.assertEqual(rc, 1)
        self.assertEqual(calls, [10, 20])

    def test_nonzero_handler_skips_target_and_continues_drain(self):
        """A handler that returns nonzero is also skipped so the drain can
        still reach the next actionable target in the same pass."""
        pool = [
            {"number": 10, "createdAt": "2024-01-01T00:00:00Z",
             "labels": [{"name": "auto-improve:refining"}]},
            {"number": 20, "createdAt": "2024-01-02T00:00:00Z",
             "labels": [{"name": "auto-improve:in-progress"}]},
        ]

        def fake_gh_json(cmd):
            if "issue" in cmd and "list" in cmd:
                return pool
            return []

        calls: list[int] = []

        def fake_di(n):
            calls.append(n)
            if n == 10:
                return 1  # non-advancing failure — don't shrink pool
            pool[:] = [i for i in pool if i["number"] != n]
            return 0

        with patch.object(dispatcher, "_gh_json", side_effect=fake_gh_json), \
             patch.object(dispatcher, "dispatch_issue", side_effect=fake_di):
            rc = dispatcher.dispatch_drain()

        self.assertEqual(rc, 1)
        self.assertEqual(calls, [10, 20])


if __name__ == "__main__":
    unittest.main()
