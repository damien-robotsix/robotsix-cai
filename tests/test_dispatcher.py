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
from cai_lib.dispatcher import HandlerResult
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
            IssueState.SPLITTING,
            IssueState.PLANNING,
            IssueState.PLANNED,
            IssueState.PLAN_APPROVED,
            IssueState.IN_PROGRESS,
            IssueState.APPLYING,
            IssueState.APPLIED,
            IssueState.PR,
            IssueState.MERGED,
            # HUMAN_NEEDED is actionable via handle_human_needed — the
            # picker filters to only those issues carrying ``human:solved``
            # so parked-waiting ones stay out of the queue.
            IssueState.HUMAN_NEEDED,
        }
        self.assertEqual(dispatcher.actionable_issue_states(), expected)
        # SOLVED is terminal and must not be dispatched.
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
            # PR_HUMAN_NEEDED is actionable via handle_pr_human_needed —
            # the picker filters to only those PRs carrying ``human:solved``
            # so parked-waiting PRs stay out of the queue.
            PRState.PR_HUMAN_NEEDED,
        }
        self.assertEqual(dispatcher.actionable_pr_states(), expected)
        self.assertNotIn(PRState.MERGED, dispatcher.actionable_pr_states())


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


class _LockNoopMixin:
    """Patch the cross-instance ownership lock helpers to no-ops.

    Production dispatch acquires LABEL_LOCKED via gh on entry and releases
    it on exit. These tests stub the lock so the existing happy-path
    routing assertions don't have to thread a fake gh comments backend
    through every test. The dedicated lock semantics live in
    tests/test_remote_lock.py.
    """

    def setUp(self):
        self._lock_acq = patch.object(
            dispatcher, "_acquire_remote_lock", return_value=True
        )
        self._lock_rel = patch.object(
            dispatcher, "_release_remote_lock", return_value=True
        )
        self._lock_acq.start()
        self._lock_rel.start()
        super().setUp()

    def tearDown(self):
        super().tearDown()
        self._lock_acq.stop()
        self._lock_rel.stop()


class TestDispatchIssue(_LockNoopMixin, unittest.TestCase):
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

    def test_handler_returning_handler_result_routes_through_driver_fire(self):
        """A handler returning HandlerResult routes through _driver_fire
        with is_pr=False and the current label list; dispatch_issue returns
        0 when _driver_fire reports ok=True."""
        hr = HandlerResult(trigger="raise_to_triaging")
        handler = MagicMock(return_value=hr)
        handler.__name__ = "mock_handler"
        issue = _issue(42, "auto-improve:raised")
        registry = {IssueState.RAISED: handler}
        with patch.object(dispatcher, "_gh_json", return_value=issue), \
             patch.object(dispatcher, "_issue_registry", return_value=registry), \
             patch.object(dispatcher, "_driver_fire",
                          return_value=(True, False)) as driver_mock:
            rc = dispatcher.dispatch_issue(42)
        self.assertEqual(rc, 0)
        driver_mock.assert_called_once_with(
            42, hr, is_pr=False, current_labels=["auto-improve:raised"],
        )

    def test_handler_result_driver_fire_failure_returns_one(self):
        """When _driver_fire reports ok=False, dispatch_issue returns 1 so
        the cycle's worst_rc reflects the stall (mirrors the #657
        ``ok=False → rc=1`` invariant used by handle_plan_gate)."""
        hr = HandlerResult(trigger="raise_to_triaging")
        handler = MagicMock(return_value=hr)
        handler.__name__ = "mock_handler"
        issue = _issue(42, "auto-improve:raised")
        registry = {IssueState.RAISED: handler}
        with patch.object(dispatcher, "_gh_json", return_value=issue), \
             patch.object(dispatcher, "_issue_registry", return_value=registry), \
             patch.object(dispatcher, "_driver_fire",
                          return_value=(False, False)):
            rc = dispatcher.dispatch_issue(42)
        self.assertEqual(rc, 1)

    def test_empty_trigger_sentinel_applies_labels_inline(self):
        """Empty-string trigger is the no-op sentinel: labels from
        ``artifacts["extra_add"]`` / ``artifacts["extra_remove"]`` are
        applied via _set_labels and fire_trigger is NOT called."""
        hr = HandlerResult(
            trigger="",
            artifacts={"extra_remove": ["x"], "extra_add": ["y"]},
        )
        handler = MagicMock(return_value=hr)
        handler.__name__ = "mock_handler"
        issue = _issue(42, "auto-improve:raised")
        registry = {IssueState.RAISED: handler}
        set_labels_mock = MagicMock(return_value=True)
        fire_mock = MagicMock()
        with patch.object(dispatcher, "_gh_json", return_value=issue), \
             patch.object(dispatcher, "_issue_registry", return_value=registry), \
             patch.object(dispatcher, "_set_labels", new=set_labels_mock), \
             patch("cai_lib.fsm.fire_trigger", new=fire_mock):
            rc = dispatcher.dispatch_issue(42)
        self.assertEqual(rc, 0)
        set_labels_mock.assert_called_once_with(
            42, add=["y"], remove=["x"], log_prefix="cai dispatch",
        )
        fire_mock.assert_not_called()


# ---------------------------------------------------------------------------
# dispatch_pr routing
# ---------------------------------------------------------------------------

class TestDispatchPR(_LockNoopMixin, unittest.TestCase):
    """dispatch_pr fetches the PR and routes by FSM label / merged flag."""

    def _run_routing(self, pr: dict, expected_state: PRState):
        handler = MagicMock(return_value=HandlerResult(trigger=""))
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
        rebase_handler = MagicMock(
            return_value=HandlerResult(trigger="rebasing_to_reviewing_code"))
        rebase_handler.__name__ = "handle_rebase"
        review_handler = MagicMock(
            return_value=HandlerResult(trigger=""))
        review_handler.__name__ = "handle_review_pr"
        registry = {PRState.REVIEWING_CODE: review_handler}
        applied: list[tuple[int, str]] = []

        def fake_apply_pr_transition(pr_number, transition_name, **kw):
            applied.append((pr_number, transition_name))
            return (True, False)

        with patch.object(dispatcher, "_gh_json", return_value=pr), \
             patch.object(dispatcher, "_pr_registry", return_value=registry), \
             patch("cai_lib.fsm.fire_trigger",
                   side_effect=fake_apply_pr_transition), \
             patch("cai_lib.actions.rebase.handle_rebase",
                   side_effect=rebase_handler):
            rc = dispatcher.dispatch_pr(99)

        self.assertEqual(rc, 0)
        review_handler.assert_not_called()
        rebase_handler.assert_called_once_with(pr)
        self.assertIn((99, "reviewing_code_to_rebasing"), applied)
        self.assertIn((99, "rebasing_to_reviewing_code"), applied)

    def test_dirty_merge_state_diverts_to_rebase(self):
        """mergeStateStatus=DIRTY also triggers the rebase divert."""
        pr = _pr(99, "pr:approved")
        pr["mergeable"] = "MERGEABLE"
        pr["mergeStateStatus"] = "DIRTY"
        rebase_handler = MagicMock(
            return_value=HandlerResult(trigger="rebasing_to_reviewing_code"))
        rebase_handler.__name__ = "handle_rebase"
        merge_handler = MagicMock(
            return_value=HandlerResult(trigger=""))
        merge_handler.__name__ = "handle_merge"
        registry = {PRState.APPROVED: merge_handler}

        with patch.object(dispatcher, "_gh_json", return_value=pr), \
             patch.object(dispatcher, "_pr_registry", return_value=registry), \
             patch("cai_lib.fsm.fire_trigger", return_value=(True, False)), \
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
        rebase_handler = MagicMock(
            return_value=HandlerResult(trigger="rebasing_to_reviewing_code"))
        rebase_handler.__name__ = "handle_rebase"
        registry = {PRState.REBASING: rebase_handler}

        with patch.object(dispatcher, "_gh_json", return_value=pr), \
             patch.object(dispatcher, "_pr_registry", return_value=registry), \
             patch("cai_lib.fsm.fire_trigger", return_value=(True, False)):
            rc = dispatcher.dispatch_pr(99)

        self.assertEqual(rc, 0)
        rebase_handler.assert_called_once_with(pr)

    def test_pr_handler_returning_handler_result_routes_through_driver_fire(self):
        """PR-side mirror of the issue-side HandlerResult shim: a PR
        handler returning HandlerResult routes through _driver_fire with
        is_pr=True and current_pr=<pr dict>."""
        hr = HandlerResult(trigger="reviewing_code_to_approved")
        handler = MagicMock(return_value=hr)
        handler.__name__ = "mock_handler"
        pr = _pr(99, "pr:reviewing-code")
        registry = {PRState.REVIEWING_CODE: handler}
        with patch.object(dispatcher, "_gh_json", return_value=pr), \
             patch.object(dispatcher, "_pr_registry", return_value=registry), \
             patch.object(dispatcher, "_driver_fire",
                          return_value=(True, False)) as driver_mock:
            rc = dispatcher.dispatch_pr(99)
        self.assertEqual(rc, 0)
        driver_mock.assert_called_once_with(
            99, hr, is_pr=True, current_pr=pr,
        )


# ---------------------------------------------------------------------------
# dispatch_drain (and the dispatch_oldest_actionable alias)
# ---------------------------------------------------------------------------

class TestDispatchOldestActionable(_LockNoopMixin, unittest.TestCase):
    """Picks the oldest (by createdAt) across issues + PRs and drives each
    picked target end-to-end before moving on to the next."""

    def test_picks_oldest_across_issues_and_prs(self):
        issues = [
            {"number": 10, "createdAt": "2024-01-01T00:00:00Z",
             "labels": [{"name": "auto-improve:refining"}]},
            {"number": 20, "createdAt": "2024-02-01T00:00:00Z",
             "labels": [{"name": "auto-improve:in-progress"}]},
        ]
        prs = [
            {"number": 99, "createdAt": "2024-01-15T00:00:00Z",
             "labels": [{"name": "pr:reviewing-code"}],
             "merged": False, "mergedAt": None},
        ]

        def fake_pool(cmd):
            if "issue" in cmd and "list" in cmd:
                return issues
            if "pr" in cmd and "list" in cmd:
                return prs
            raise AssertionError(f"unexpected _gh_json call: {cmd}")

        di_calls: list[int] = []
        dp_calls: list[int] = []

        # Drive advances each target to a terminal state (SOLVED for
        # issues, MERGED for PRs) in one step. We stub the driver's
        # state-lookup helpers so the outer drain sees a shrinking pool.
        def fake_fetch_issue_state(n):
            if n in di_calls:
                # After drive: terminal, removed from pool.
                issues[:] = [i for i in issues if i["number"] != n]
                return None
            return dispatcher.IssueState.REFINING if n == 10 else \
                   dispatcher.IssueState.IN_PROGRESS

        def fake_fetch_pr_state_info(n):
            if n in dp_calls:
                prs[:] = [p for p in prs if p["number"] != n]
                return None
            return dispatcher.PRState.REVIEWING_CODE, {"headRefName": "x"}

        def fake_di(n):
            di_calls.append(n)
            return 0

        def fake_dp(n):
            dp_calls.append(n)
            return 0

        with patch.object(dispatcher, "_gh_json", side_effect=fake_pool), \
             patch.object(dispatcher, "_fetch_issue_state",
                          side_effect=fake_fetch_issue_state), \
             patch.object(dispatcher, "_fetch_pr_state_info",
                          side_effect=fake_fetch_pr_state_info), \
             patch.object(dispatcher, "dispatch_issue", side_effect=fake_di), \
             patch.object(dispatcher, "dispatch_pr", side_effect=fake_dp):
            rc = dispatcher.dispatch_oldest_actionable()

        self.assertEqual(rc, 0)
        # Oldest first: issue #10 (Jan 1), PR #99 (Jan 15), issue #20 (Feb 1).
        self.assertEqual(di_calls, [10, 20])
        self.assertEqual(dp_calls, [99])

    def test_human_needed_picked_only_with_human_solved(self):
        """HUMAN_NEEDED issues are only pickable when the admin has
        applied ``human:solved``. Parked-waiting issues must stay out of
        the drain queue so the cycle doesn't spin on them each tick.
        """
        from cai_lib.config import LABEL_HUMAN_SOLVED
        issues = [
            # Parked, no solved label → must be ignored.
            {"number": 30, "createdAt": "2024-01-01T00:00:00Z",
             "labels": [{"name": "auto-improve:human-needed"}]},
            # Parked with solved label → pickable.
            {"number": 31, "createdAt": "2024-01-02T00:00:00Z",
             "labels": [{"name": "auto-improve:human-needed"},
                        {"name": LABEL_HUMAN_SOLVED}]},
        ]

        def fake_pool(cmd):
            if "issue" in cmd and "list" in cmd:
                return issues
            if "pr" in cmd and "list" in cmd:
                return []
            raise AssertionError(f"unexpected _gh_json call: {cmd}")

        di_calls: list[int] = []

        def fake_fetch_issue_state(n):
            if n in di_calls:
                issues[:] = [i for i in issues if i["number"] != n]
                return None
            return dispatcher.IssueState.HUMAN_NEEDED

        def fake_di(n):
            di_calls.append(n)
            return 0

        with patch.object(dispatcher, "_gh_json", side_effect=fake_pool), \
             patch.object(dispatcher, "_fetch_issue_state",
                          side_effect=fake_fetch_issue_state), \
             patch.object(dispatcher, "dispatch_issue", side_effect=fake_di), \
             patch.object(dispatcher, "dispatch_pr") as dp:
            rc = dispatcher.dispatch_oldest_actionable()

        self.assertEqual(rc, 0)
        self.assertEqual(di_calls, [31])
        dp.assert_not_called()

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


class TestDispatchDrain(_LockNoopMixin, unittest.TestCase):
    """dispatch_drain picks oldest target, drives it end-to-end, repeats
    until the queue is empty or the per-drain cap is hit."""

    def _run_drain(self, pool, di_behavior=None, dp_behavior=None,
                   max_iter=50):
        """Helper: run dispatch_drain with a mutable pool of targets and
        stubbed dispatch/state helpers.

        ``pool`` is a list of dicts like
        ``{"kind": "issue", "number": 10, "createdAt": "...",
          "state": IssueState.REFINING, "advance_to": IssueState.SOLVED}``.
        ``advance_to=None`` means terminal after dispatch (dropped from pool).
        """
        di_calls: list[int] = []
        dp_calls: list[int] = []

        def snap_issues():
            return [
                {"number": p["number"], "createdAt": p["createdAt"],
                 "labels": [{"name": p["state"].value}]}
                for p in pool if p["kind"] == "issue"
            ]

        def snap_prs():
            return [
                {"number": p["number"], "createdAt": p["createdAt"],
                 "labels": [{"name": p["state"].value}],
                 "merged": False, "mergedAt": None}
                for p in pool if p["kind"] == "pr"
            ]

        def fake_gh_json(cmd):
            if "issue" in cmd and "list" in cmd:
                return snap_issues()
            if "pr" in cmd and "list" in cmd:
                return snap_prs()
            raise AssertionError(f"unexpected _gh_json call: {cmd}")

        def find(kind, number):
            for p in pool:
                if p["kind"] == kind and p["number"] == number:
                    return p
            return None

        def fake_fetch_issue_state(n):
            p = find("issue", n)
            if p is None:
                return None
            return p["state"]

        def fake_fetch_pr_state_info(n):
            p = find("pr", n)
            if p is None:
                return None
            return p["state"], {"headRefName": f"auto-improve/{n}-x",
                                "statusCheckRollup": [], "mergedAt": None}

        def fake_di(n):
            di_calls.append(n)
            if di_behavior is not None:
                rc = di_behavior(n)
                if rc is not None:
                    return rc
            p = find("issue", n)
            if p is None:
                return 0
            advance = p.get("advance_to")
            if advance is None:
                pool[:] = [x for x in pool if not (x["kind"] == "issue" and x["number"] == n)]
            else:
                p["state"] = advance
            return 0

        def fake_dp(n):
            dp_calls.append(n)
            if dp_behavior is not None:
                rc = dp_behavior(n)
                if rc is not None:
                    return rc
            p = find("pr", n)
            if p is None:
                return 0
            advance = p.get("advance_to")
            if advance is None:
                pool[:] = [x for x in pool if not (x["kind"] == "pr" and x["number"] == n)]
            else:
                p["state"] = advance
            return 0

        with patch.object(dispatcher, "_gh_json", side_effect=fake_gh_json), \
             patch.object(dispatcher, "_fetch_issue_state",
                          side_effect=fake_fetch_issue_state), \
             patch.object(dispatcher, "_fetch_pr_state_info",
                          side_effect=fake_fetch_pr_state_info), \
             patch.object(dispatcher, "dispatch_issue", side_effect=fake_di), \
             patch.object(dispatcher, "dispatch_pr", side_effect=fake_dp):
            rc = dispatcher.dispatch_drain(max_iter=max_iter)

        return rc, di_calls, dp_calls

    def test_drains_multiple_distinct_targets(self):
        pool = [
            {"kind": "issue", "number": 10, "createdAt": "2024-01-01T00:00:00Z",
             "state": IssueState.REFINING, "advance_to": None},
            {"kind": "issue", "number": 20, "createdAt": "2024-01-02T00:00:00Z",
             "state": IssueState.IN_PROGRESS, "advance_to": None},
            {"kind": "pr", "number": 99, "createdAt": "2024-01-03T00:00:00Z",
             "state": PRState.REVIEWING_CODE, "advance_to": None},
        ]
        rc, di_calls, dp_calls = self._run_drain(pool)
        self.assertEqual(rc, 0)
        self.assertEqual(di_calls, [10, 20])
        self.assertEqual(dp_calls, [99])

    def test_drive_runs_handler_multiple_times_as_state_advances(self):
        """Inner driver keeps re-dispatching the same target while state
        keeps advancing, until it hits a terminal state."""
        # REFINING → PLANNED → SOLVED in three dispatch calls on one issue.
        p = {"kind": "issue", "number": 10,
             "createdAt": "2024-01-01T00:00:00Z",
             "state": IssueState.REFINING,
             "advance_to": IssueState.PLANNED}
        pool = [p]

        call_counter = {"n": 0}

        def di_behavior(n):
            call_counter["n"] += 1
            if call_counter["n"] == 1:
                p["state"] = IssueState.PLANNED
            elif call_counter["n"] == 2:
                # PLANNED is a gate state — not actionable in this test's
                # registry terms; treat as terminal here.
                pool[:] = []
            return 0

        rc, di_calls, _ = self._run_drain(pool, di_behavior=di_behavior)
        self.assertEqual(rc, 0)
        # Driver dispatched until state left the actionable set.
        self.assertGreaterEqual(len(di_calls), 1)

    def test_target_blocked_when_state_does_not_change(self):
        """Handler returns 0 but state is identical before and after →
        blocked; the driver bails and the outer drain moves on."""
        pool = [
            {"kind": "issue", "number": 10, "createdAt": "2024-01-01T00:00:00Z",
             "state": IssueState.REFINING, "advance_to": IssueState.REFINING},
            {"kind": "issue", "number": 20, "createdAt": "2024-01-02T00:00:00Z",
             "state": IssueState.IN_PROGRESS, "advance_to": None},
        ]
        # #10 never advances; #20 still gets its turn.
        call_counts = {"10": 0}

        def di_behavior(n):
            if n == 10:
                call_counts["10"] += 1
                # Hold state fixed so driver sees no change.
                return 0
            return None  # fall through to default behavior for #20

        rc, di_calls, _ = self._run_drain(pool, di_behavior=di_behavior)
        self.assertEqual(rc, 0)
        # #10 dispatched once, then marked blocked (no retry in same drain).
        self.assertEqual(call_counts["10"], 1)
        # #20 was reached after #10 bailed.
        self.assertIn(20, di_calls)

    def test_max_iter_cap(self):
        pool = [
            {"kind": "issue", "number": n,
             "createdAt": f"2024-01-{n:02d}T00:00:00Z",
             "state": IssueState.REFINING, "advance_to": None}
            for n in range(1, 11)
        ]
        rc, di_calls, _ = self._run_drain(pool, max_iter=3)
        self.assertEqual(rc, 0)
        self.assertEqual(len(di_calls), 3)

    def test_returns_worst_exit_code(self):
        pool = [
            {"kind": "issue", "number": 10, "createdAt": "2024-01-01T00:00:00Z",
             "state": IssueState.REFINING, "advance_to": None},
            {"kind": "issue", "number": 20, "createdAt": "2024-01-02T00:00:00Z",
             "state": IssueState.IN_PROGRESS, "advance_to": None},
        ]

        def di_behavior(n):
            if n == 20:
                # Failure; also remove from pool so outer loop terminates.
                pool[:] = [p for p in pool if p["number"] != 20]
                return 2
            return None

        rc, _, _ = self._run_drain(pool, di_behavior=di_behavior)
        self.assertEqual(rc, 2)

    def test_handler_exception_stops_drive_and_continues_drain(self):
        """A crashing handler stops its drive but the drain moves on (#657)."""
        pool = [
            {"kind": "issue", "number": 10, "createdAt": "2024-01-01T00:00:00Z",
             "state": IssueState.REFINING, "advance_to": None},
            {"kind": "issue", "number": 20, "createdAt": "2024-01-02T00:00:00Z",
             "state": IssueState.IN_PROGRESS, "advance_to": None},
        ]

        def di_behavior(n):
            if n == 10:
                raise RuntimeError("boom")
            return None

        rc, di_calls, _ = self._run_drain(pool, di_behavior=di_behavior)
        self.assertEqual(rc, 1)
        self.assertEqual(di_calls, [10, 20])


class TestSubIssueOrderingGate(unittest.TestCase):
    """Sub-issues linked under an ``auto-improve:parent`` issue must wait
    until the immediately prior sibling (in the native sub-issues list
    order) has been closed — typically by its PR merging and confirm
    closing it."""

    def _pick(self, issues, sub_issues_by_parent):
        def fake_gh_json(cmd):
            # `_build_ordering_gate` lists parents first.
            if "issue" in cmd and "list" in cmd and "--label" in cmd:
                label_idx = cmd.index("--label") + 1
                if cmd[label_idx] == "auto-improve:parent":
                    return [{"number": p} for p in sub_issues_by_parent]
                return issues
            if "pr" in cmd and "list" in cmd:
                return []
            raise AssertionError(f"unexpected _gh_json call: {cmd}")

        def fake_list_sub_issues(parent_num):
            return sub_issues_by_parent.get(parent_num, [])

        with patch.object(dispatcher, "_gh_json", side_effect=fake_gh_json), \
             patch.object(dispatcher, "list_sub_issues",
                          side_effect=fake_list_sub_issues):
            return dispatcher._pick_oldest_actionable_target()

    def test_later_sibling_skipped_while_prior_sibling_open(self):
        issues = [
            {"number": 50, "createdAt": "2024-01-01T00:00:00Z",
             "title": "Previous step",
             "labels": [{"name": "auto-improve:in-progress"}]},
            {"number": 51, "createdAt": "2024-01-02T00:00:00Z",
             "title": "Later step",
             "labels": [{"name": "auto-improve:refined"}]},
        ]
        sub_issues_by_parent = {
            621: [
                {"number": 50, "state": "open"},
                {"number": 51, "state": "open"},
            ],
        }
        target = self._pick(issues, sub_issues_by_parent)
        self.assertEqual(target, ("issue", 50))

    def test_later_sibling_picked_when_prior_sibling_closed(self):
        issues = [
            {"number": 51, "createdAt": "2024-01-02T00:00:00Z",
             "title": "Later step",
             "labels": [{"name": "auto-improve:refined"}]},
        ]
        sub_issues_by_parent = {
            621: [
                {"number": 50, "state": "closed"},
                {"number": 51, "state": "open"},
            ],
        }
        target = self._pick(issues, sub_issues_by_parent)
        self.assertEqual(target, ("issue", 51))

    def test_first_sibling_never_gated(self):
        issues = [
            {"number": 51, "createdAt": "2024-01-02T00:00:00Z",
             "title": "First step",
             "labels": [{"name": "auto-improve:refined"}]},
        ]
        sub_issues_by_parent = {
            621: [
                {"number": 51, "state": "open"},
            ],
        }
        target = self._pick(issues, sub_issues_by_parent)
        self.assertEqual(target, ("issue", 51))

    def test_issue_with_no_parent_is_not_gated(self):
        issues = [
            {"number": 77, "createdAt": "2024-01-02T00:00:00Z",
             "title": "Standalone issue",
             "labels": [{"name": "auto-improve:refined"}]},
        ]
        target = self._pick(issues, sub_issues_by_parent={})
        self.assertEqual(target, ("issue", 77))

    def test_gate_skips_to_any_prior_still_open(self):
        # Step 2 closed, step 1 and step 3 open: step 3 is still gated by
        # step 2's position (immediately prior). But since step 2 is closed,
        # step 3 is blocked by step 1 transitively because step 1 is still
        # the last-open sibling before step 3 in link order.
        issues = [
            {"number": 10, "createdAt": "2024-01-01T00:00:00Z",
             "title": "Step 1",
             "labels": [{"name": "auto-improve:refined"}]},
            {"number": 30, "createdAt": "2024-01-03T00:00:00Z",
             "title": "Step 3",
             "labels": [{"name": "auto-improve:refined"}]},
        ]
        sub_issues_by_parent = {
            621: [
                {"number": 10, "state": "open"},
                {"number": 20, "state": "closed"},
                {"number": 30, "state": "open"},
            ],
        }
        target = self._pick(issues, sub_issues_by_parent)
        self.assertEqual(target, ("issue", 10))

    # ------------------------------------------------------------------
    # Nested-parent propagation tests (issue #922)
    # ------------------------------------------------------------------

    def test_nested_parent_blocks_grandchildren_while_ancestor_sibling_open(self):
        """Grandchildren of a gated nested parent must not be dispatched
        while the ancestor's prior sibling is still open.

        Tree:
          M=900 (parent)
          ├── S1=50  (:in-progress, open)   ← S2 must wait for S1
          └── S2=51  (parent, open)
              ├── ss1=60 (:raised, open)    ← BUG: was dispatched before fix
              └── ss2=61 (:raised, open)
        """
        # S2=51 has no FSM state label so it never appears in the issues list.
        issues = [
            {"number": 50, "createdAt": "2024-01-01T00:00:00Z",
             "labels": [{"name": "auto-improve:in-progress"}]},
            {"number": 60, "createdAt": "2024-01-02T00:00:00Z",
             "labels": [{"name": "auto-improve:raised"}]},
            {"number": 61, "createdAt": "2024-01-03T00:00:00Z",
             "labels": [{"name": "auto-improve:raised"}]},
        ]
        sub_issues_by_parent = {
            900: [
                {"number": 50, "state": "open"},
                {"number": 51, "state": "open"},
            ],
            51: [
                {"number": 60, "state": "open"},
                {"number": 61, "state": "open"},
            ],
        }
        # S1=50 is the only pickable target; ss1=60 must be gated.
        target = self._pick(issues, sub_issues_by_parent)
        self.assertEqual(target, ("issue", 50))

    def test_nested_parent_grandchildren_pickable_when_ancestor_sibling_closed(self):
        """Once the ancestor's prior sibling is closed the gate lifts and
        the first grandchild (ss1) becomes dispatchable.

        Tree (S1 now closed):
          M=900 (parent)
          ├── S1=50  (closed)
          └── S2=51  (parent, open)
              ├── ss1=60 (:raised, open)  ← should now be picked
              └── ss2=61 (:raised, open)
        """
        # S1=50 is closed and absent from the open-issues list.
        issues = [
            {"number": 60, "createdAt": "2024-01-02T00:00:00Z",
             "labels": [{"name": "auto-improve:raised"}]},
            {"number": 61, "createdAt": "2024-01-03T00:00:00Z",
             "labels": [{"name": "auto-improve:raised"}]},
        ]
        sub_issues_by_parent = {
            900: [
                {"number": 50, "state": "closed"},
                {"number": 51, "state": "open"},
            ],
            51: [
                {"number": 60, "state": "open"},
                {"number": 61, "state": "open"},
            ],
        }
        target = self._pick(issues, sub_issues_by_parent)
        self.assertEqual(target, ("issue", 60))


class TestBuildOrderingGate(unittest.TestCase):
    def _build(self, parents_list, subs):
        """Helper: run _build_ordering_gate with fake gh and list_sub_issues."""
        def fake_gh_json(cmd):
            if "issue" in cmd and "list" in cmd:
                return [{"number": p} for p in parents_list]
            raise AssertionError(f"unexpected _gh_json call: {cmd}")

        def fake_list_sub_issues(parent_num):
            return subs.get(parent_num, [])

        with patch.object(dispatcher, "_gh_json", side_effect=fake_gh_json), \
             patch.object(dispatcher, "list_sub_issues",
                          side_effect=fake_list_sub_issues):
            return dispatcher._build_ordering_gate()

    def test_gate_maps_each_child_to_last_open_prior_sibling(self):
        gate = self._build([900], {
            900: [
                {"number": 10, "state": "open"},
                {"number": 20, "state": "closed"},
                {"number": 30, "state": "open"},
                {"number": 40, "state": "open"},
            ],
        })

        # #10 is first — no prior sibling → not in gate.
        self.assertNotIn(10, gate)
        # #20 is preceded by #10 (open) → blocked by #10.
        self.assertEqual(gate[20], (900, 10))
        # #30 is preceded by #10 (open) and #20 (closed) →
        # still blocked by #10 (last open prior).
        self.assertEqual(gate[30], (900, 10))
        # #40 is preceded by #10 (open), #20 (closed), #30 (open) →
        # blocked by #30 (the most recent still-open prior).
        self.assertEqual(gate[40], (900, 30))

    def test_nested_parent_gate_propagates_to_ungated_grandchildren(self):
        """Gate propagation: ss1 (first child of gated S2) must inherit
        S2's ancestor blocker (M, S1) because S2 itself is gated.

        Tree:
          M=900 (parent)
          ├── S1=50  (open)
          └── S2=51  (parent, open)   ← gate[51] = (900, 50)
              ├── ss1=60 (open)        ← gate[60] should be (900, 50)
              └── ss2=61 (open)        ← gate[61] = (51, 60) from first pass
        """
        gate = self._build([900, 51], {
            900: [
                {"number": 50, "state": "open"},
                {"number": 51, "state": "open"},
            ],
            51: [
                {"number": 60, "state": "open"},
                {"number": 61, "state": "open"},
            ],
        })

        # S2=51 gated on S1=50 under M=900.
        self.assertEqual(gate[51], (900, 50))
        # ss1=60 is ungated in the flat first pass (first child of S2) but
        # must inherit S2's ancestor blocker after propagation.
        self.assertEqual(gate[60], (900, 50))
        # ss2=61 was already gated locally (on ss1=60); that gate must
        # be preserved — local gate is stricter than the inherited one.
        self.assertEqual(gate[61], (51, 60))

    def test_nested_parent_no_propagation_when_ancestor_sibling_closed(self):
        """When S2's prior sibling (S1) is closed, S2 is not in the gate
        and neither are its children — they are freely pickable.

        Tree (S1 closed):
          M=900 (parent)
          ├── S1=50  (closed)
          └── S2=51  (parent, open)   ← not gated
              ├── ss1=60 (open)        ← not gated
              └── ss2=61 (open)        ← gate[61] = (51, 60) from first pass
        """
        gate = self._build([900, 51], {
            900: [
                {"number": 50, "state": "closed"},
                {"number": 51, "state": "open"},
            ],
            51: [
                {"number": 60, "state": "open"},
                {"number": 61, "state": "open"},
            ],
        })

        self.assertNotIn(51, gate)
        self.assertNotIn(60, gate)
        # ss2=61 still has local gate within S2 (ss1 is open prior sibling).
        self.assertEqual(gate[61], (51, 60))


if __name__ == "__main__":
    unittest.main()
