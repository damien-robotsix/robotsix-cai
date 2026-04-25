"""Regression tests for issue #1075 — cai-merge `hold` verdicts
tagged ``issue_type == "approach_mismatch"`` close the PR,
stamp ``LABEL_OPUS_ATTEMPTED`` on the linked issue, and fire the
new ``pr_to_plan_approved`` issue FSM transition. Verdicts without
that tag (or with any other ``issue_type`` value) keep the prior
``approved_to_human`` parking behaviour.
"""
import json
import os
import sys
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cai_lib.actions import merge as merge_mod
from cai_lib.config import (
    LABEL_OPUS_ATTEMPTED,
    LABEL_PLAN_APPROVED,
    LABEL_PR_OPEN,
)
from cai_lib.dispatcher import HandlerResult
from cai_lib.fsm_states import IssueState
from cai_lib.fsm_transitions import ISSUE_TRANSITIONS

from tests._helpers import _pr_fixture


class TestPrToPlanApprovedTransition(unittest.TestCase):
    """The new issue FSM transition must be registered with the right shape."""

    def test_transition_registered(self):
        names = {t.name for t in ISSUE_TRANSITIONS}
        self.assertIn("pr_to_plan_approved", names)

    def test_transition_label_shape(self):
        t = next(
            t for t in ISSUE_TRANSITIONS
            if t.name == "pr_to_plan_approved"
        )
        self.assertEqual(t.from_state, IssueState.PR)
        self.assertEqual(t.to_state, IssueState.PLAN_APPROVED)
        self.assertIn(LABEL_PR_OPEN, t.labels_remove)
        self.assertIn(LABEL_PLAN_APPROVED, t.labels_add)


class TestHandleMergeApproachMismatchRouting(unittest.TestCase):
    """``handle_merge`` fast-paths only ``hold`` + ``approach_mismatch``."""

    def _invoke(self, *, confidence: str, action: str,
                issue_type):
        """Drive handle_merge with a model verdict built from kwargs.

        *issue_type* may be a string, ``None`` (field absent), or the
        sentinel ``"<omit>"`` — the latter drops the key entirely from
        the emitted JSON so the handler sees no field.
        """
        pr = _pr_fixture()

        verdict: dict = {
            "confidence": confidence,
            "action": action,
            "reasoning": "wrong API entirely; needs fresh implement",
        }
        if issue_type != "<omit>":
            verdict["issue_type"] = issue_type

        run_mock = MagicMock()
        run_mock.return_value.returncode = 0
        run_mock.return_value.stdout = ""
        run_mock.return_value.stderr = ""

        claude_mock = MagicMock()
        claude_mock.return_value.returncode = 0
        claude_mock.return_value.stdout = json.dumps(verdict)
        claude_mock.return_value.stderr = ""

        def gh_json_side_effect(args):
            if "issue" in args and "view" in args:
                return {
                    "number": 1234,
                    "title": "auto-improve: example",
                    "labels": [{"name": "auto-improve:pr-open"}],
                    "state": "OPEN",
                    "body": "",
                }
            if "pr" in args and "view" in args:
                return {"statusCheckRollup": []}
            return {}

        gh_json_mock = MagicMock(side_effect=gh_json_side_effect)
        filter_mock = MagicMock(return_value=[])
        fetch_review_mock = MagicMock(return_value=[])
        has_label_mock = MagicMock(return_value=False)
        set_labels_mock = MagicMock(return_value=True)
        fire_trigger_mock = MagicMock(return_value=(True, False))
        log_mock = MagicMock()
        git_mock = MagicMock()

        with patch.object(merge_mod, "_run", run_mock), \
             patch.object(merge_mod, "_run_claude_p", claude_mock), \
             patch.object(merge_mod, "_gh_json", gh_json_mock), \
             patch.object(merge_mod, "_git", git_mock), \
             patch.object(merge_mod, "_filter_comments_with_haiku",
                          filter_mock), \
             patch.object(merge_mod, "_fetch_review_comments",
                          fetch_review_mock), \
             patch.object(merge_mod, "_issue_has_label",
                          has_label_mock), \
             patch.object(merge_mod, "_set_labels", set_labels_mock), \
             patch.object(merge_mod, "fire_trigger",
                          fire_trigger_mock), \
             patch.object(merge_mod, "log_run", log_mock):
            result = merge_mod.handle_merge(pr)

        self.assertIsInstance(result, HandlerResult)
        return {
            "result": result,
            "issue_transition": fire_trigger_mock,
            "set_labels": set_labels_mock,
            "run": run_mock,
        }

    def _pr_transition_names(self, mocks) -> list:
        """PR-side transitions are returned in the HandlerResult. Empty
        trigger is the no-op sentinel."""
        trig = mocks["result"].trigger
        return [trig] if trig else []

    def _issue_transition_names(self, fire_trigger_mock) -> list:
        return [
            c.args[1] for c in fire_trigger_mock.call_args_list
            if len(c.args) >= 2 and isinstance(c.args[1], str)
            and not c.kwargs.get("is_pr", False)
        ]

    def test_hold_with_approach_mismatch_closes_and_transitions(self):
        mocks = self._invoke(
            confidence="medium", action="hold",
            issue_type="approach_mismatch",
        )
        # No PR FSM transition fires on the success path (close + issue
        # transition is the entire recovery).
        self.assertEqual(self._pr_transition_names(mocks), [])
        # Exactly one issue FSM transition, pointing at PLAN_APPROVED.
        self.assertEqual(
            self._issue_transition_names(mocks["issue_transition"]),
            ["pr_to_plan_approved"],
        )
        # LABEL_OPUS_ATTEMPTED was stamped on the issue.
        set_labels_calls = mocks["set_labels"].call_args_list
        self.assertTrue(
            any(
                LABEL_OPUS_ATTEMPTED in (c.kwargs.get("add") or [])
                for c in set_labels_calls
            ),
            f"LABEL_OPUS_ATTEMPTED was not added; got: {set_labels_calls!r}",
        )
        # gh pr close --delete-branch was invoked exactly once.
        close_calls = [
            c for c in mocks["run"].call_args_list
            if c.args and c.args[0][:3] == ["gh", "pr", "close"]
        ]
        self.assertEqual(len(close_calls), 1)
        self.assertIn("--delete-branch", close_calls[0].args[0])

    def test_hold_without_issue_type_still_parks_as_human(self):
        mocks = self._invoke(
            confidence="medium", action="hold",
            issue_type="<omit>",
        )
        self.assertEqual(
            self._pr_transition_names(mocks),
            ["approved_to_human"],
        )
        self.assertEqual(
            self._issue_transition_names(mocks["issue_transition"]),
            [],
        )

    def test_hold_with_issue_type_none_still_parks_as_human(self):
        mocks = self._invoke(
            confidence="medium", action="hold",
            issue_type=None,
        )
        self.assertEqual(
            self._pr_transition_names(mocks),
            ["approved_to_human"],
        )
        self.assertEqual(
            self._issue_transition_names(mocks["issue_transition"]),
            [],
        )

    def test_hold_with_issue_type_other_still_parks_as_human(self):
        mocks = self._invoke(
            confidence="medium", action="hold",
            issue_type="other",
        )
        self.assertEqual(
            self._pr_transition_names(mocks),
            ["approved_to_human"],
        )
        self.assertEqual(
            self._issue_transition_names(mocks["issue_transition"]),
            [],
        )

    def test_hold_with_scope_creep_does_not_trigger_approach_path(self):
        """Only ``approach_mismatch`` triggers the close+opus path;
        other enum values are pass-through for this handler."""
        mocks = self._invoke(
            confidence="medium", action="hold",
            issue_type="scope_creep",
        )
        self.assertEqual(
            self._pr_transition_names(mocks),
            ["approved_to_human"],
        )
        self.assertEqual(
            self._issue_transition_names(mocks["issue_transition"]),
            [],
        )


if __name__ == "__main__":
    unittest.main()
