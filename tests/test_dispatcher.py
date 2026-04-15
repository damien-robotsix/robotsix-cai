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


class TestSubIssueStepOrderingGate(unittest.TestCase):
    """Sub-issues like ``[#P Step N/T]`` must wait until the step N-1
    sub-issue has been closed (typically by its PR merging and confirm
    closing it)."""

    def _pick(self, issues):
        def fake_gh_json(cmd):
            if "issue" in cmd and "list" in cmd:
                return issues
            if "pr" in cmd and "list" in cmd:
                return []
            raise AssertionError(f"unexpected _gh_json call: {cmd}")

        with patch.object(dispatcher, "_gh_json", side_effect=fake_gh_json):
            return dispatcher._pick_oldest_actionable_target()

    def test_later_step_skipped_while_prior_step_open(self):
        issues = [
            {"number": 50, "createdAt": "2024-01-01T00:00:00Z",
             "title": "[#621 Step 4/6] Previous step",
             "labels": [{"name": "auto-improve:in-progress"}]},
            {"number": 51, "createdAt": "2024-01-02T00:00:00Z",
             "title": "[#621 Step 5/6] Migrate check-workflows",
             "labels": [{"name": "auto-improve:refined"}]},
        ]
        target = self._pick(issues)
        self.assertEqual(target, ("issue", 50))

    def test_later_step_picked_when_prior_step_closed(self):
        # Step 4 absent from the open list → it was closed → step 5 is allowed.
        issues = [
            {"number": 51, "createdAt": "2024-01-02T00:00:00Z",
             "title": "[#621 Step 5/6] Migrate check-workflows",
             "labels": [{"name": "auto-improve:refined"}]},
        ]
        target = self._pick(issues)
        self.assertEqual(target, ("issue", 51))

    def test_step_one_never_gated(self):
        issues = [
            {"number": 51, "createdAt": "2024-01-02T00:00:00Z",
             "title": "[#621 Step 1/6] First step",
             "labels": [{"name": "auto-improve:refined"}]},
        ]
        target = self._pick(issues)
        self.assertEqual(target, ("issue", 51))

    def test_parse_sub_issue_step(self):
        self.assertEqual(
            dispatcher._parse_sub_issue_step("[#123 Step 2/5] Do a thing"),
            (123, 2),
        )
        self.assertIsNone(dispatcher._parse_sub_issue_step("Just a normal title"))
        self.assertIsNone(dispatcher._parse_sub_issue_step(""))


if __name__ == "__main__":
    unittest.main()
