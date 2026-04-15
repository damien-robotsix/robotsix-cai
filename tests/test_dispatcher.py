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
# dispatch_oldest_actionable
# ---------------------------------------------------------------------------

class TestDispatchOldestActionable(unittest.TestCase):
    """Picks the oldest (by createdAt) across issues + PRs."""

    def test_picks_oldest_across_issues_and_prs(self):
        # Two issues + one PR; issue #10 is oldest.
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

        def fake_gh_json(cmd):
            # cmd is a list; distinguish issue vs pr list.
            if "issue" in cmd and "list" in cmd:
                return issues
            if "pr" in cmd and "list" in cmd:
                return prs
            raise AssertionError(f"unexpected _gh_json call: {cmd}")

        with patch.object(dispatcher, "_gh_json", side_effect=fake_gh_json), \
             patch.object(dispatcher, "dispatch_issue", return_value=0) as di, \
             patch.object(dispatcher, "dispatch_pr", return_value=0) as dp:
            rc = dispatcher.dispatch_oldest_actionable()

        self.assertEqual(rc, 0)
        di.assert_called_once_with(10)
        dp.assert_not_called()

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

        with patch.object(dispatcher, "_gh_json", side_effect=fake_gh_json), \
             patch.object(dispatcher, "dispatch_issue", return_value=0) as di, \
             patch.object(dispatcher, "dispatch_pr", return_value=0) as dp:
            rc = dispatcher.dispatch_oldest_actionable()

        self.assertEqual(rc, 0)
        dp.assert_called_once_with(99)
        di.assert_not_called()

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


if __name__ == "__main__":
    unittest.main()
