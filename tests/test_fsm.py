"""Tests for cai_lib.fsm — FSM data structures."""
import sys
import os
import unittest
from collections import deque

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cai_lib.fsm import (
    IssueState, PRState, Transition,
    ISSUE_TRANSITIONS, PR_TRANSITIONS,
    get_issue_state, render_fsm_mermaid,
    apply_transition, find_transition,
)
from cai_lib.config import (
    LABEL_IN_PROGRESS, LABEL_RAISED, LABEL_REFINED, LABEL_HUMAN_SUBMITTED,
)


class TestFsm(unittest.TestCase):

    def test_get_issue_state_in_progress(self):
        result = get_issue_state([LABEL_IN_PROGRESS])
        self.assertEqual(result, IssueState.IN_PROGRESS)

    def test_no_orphan_states(self):
        """BFS from RAISED must reach every non-terminal IssueState."""
        terminal = {IssueState.SOLVED, IssueState.HUMAN_NEEDED}
        adj: dict = {}
        for t in ISSUE_TRANSITIONS:
            adj.setdefault(t.from_state, []).append(t.to_state)

        visited = set()
        queue = deque([IssueState.RAISED])
        while queue:
            state = queue.popleft()
            if state in visited:
                continue
            visited.add(state)
            for nxt in adj.get(state, []):
                if nxt not in visited:
                    queue.append(nxt)

        non_terminal = {s for s in IssueState if s not in terminal}
        unreachable = non_terminal - visited
        self.assertFalse(
            unreachable,
            f"States unreachable from RAISED via BFS: {unreachable}",
        )

    def test_no_pr_open_in_issue_state(self):
        names = [s.name for s in IssueState]
        self.assertNotIn("PR_OPEN", names,
            "PR_OPEN must not appear in IssueState — it belongs in PRState")
        self.assertNotIn("REVISING", names,
            "REVISING must not appear in IssueState — it belongs in PRState")
        self.assertIn("PR", names,
            "IssueState must have a PR state representing the PR submachine")

    def test_render_fsm_mermaid_contains_all_transitions(self):
        result = render_fsm_mermaid(ISSUE_TRANSITIONS)
        self.assertIn("stateDiagram-v2", result)
        for t in ISSUE_TRANSITIONS:
            self.assertIn(t.name, result,
                f"Transition {t.name!r} missing from mermaid output")
        for t in ISSUE_TRANSITIONS:
            if t.min_confidence > 0.0:
                self.assertIn(f"[≥{t.min_confidence}]", result,
                    f"Confidence annotation missing for {t.name!r}")

    def test_pr_transitions_are_transition_objects(self):
        self.assertTrue(len(PR_TRANSITIONS) > 0)
        for t in PR_TRANSITIONS:
            self.assertIsInstance(t, Transition,
                f"{t!r} is not a Transition instance")
            self.assertIsInstance(t.from_state, PRState,
                f"from_state {t.from_state!r} is not a PRState member")
            self.assertIsInstance(t.to_state, PRState,
                f"to_state {t.to_state!r} is not a PRState member")


class TestApplyTransition(unittest.TestCase):

    def _recording_set_labels(self):
        calls = []
        def _fake(issue_number, *, add=(), remove=(), log_prefix="cai"):
            calls.append({
                "issue_number": issue_number,
                "add": list(add),
                "remove": list(remove),
                "log_prefix": log_prefix,
            })
            return True
        return calls, _fake

    def test_happy_path_applies_labels(self):
        calls, fake = self._recording_set_labels()
        ok = apply_transition(
            42, "raise_to_refine",
            current_labels=[LABEL_RAISED],
            set_labels=fake,
        )
        self.assertTrue(ok)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["issue_number"], 42)
        self.assertIn(LABEL_REFINED, calls[0]["add"])
        self.assertIn(LABEL_RAISED, calls[0]["remove"])

    def test_extra_remove_is_forwarded(self):
        calls, fake = self._recording_set_labels()
        apply_transition(
            7, "raise_to_refine",
            current_labels=[LABEL_RAISED],
            extra_remove=[LABEL_HUMAN_SUBMITTED],
            set_labels=fake,
        )
        self.assertIn(LABEL_HUMAN_SUBMITTED, calls[0]["remove"])
        self.assertIn(LABEL_RAISED, calls[0]["remove"])

    def test_state_mismatch_refuses(self):
        calls, fake = self._recording_set_labels()
        ok = apply_transition(
            9, "raise_to_refine",
            current_labels=[LABEL_REFINED],  # wrong from_state
            set_labels=fake,
        )
        self.assertFalse(ok)
        self.assertEqual(calls, [])

    def test_unknown_transition_raises(self):
        with self.assertRaises(KeyError):
            apply_transition(1, "not_a_real_transition", current_labels=[LABEL_RAISED])

    def test_skip_validation_when_no_current_labels(self):
        calls, fake = self._recording_set_labels()
        ok = apply_transition(1, "raise_to_refine", set_labels=fake)
        self.assertTrue(ok)
        self.assertEqual(len(calls), 1)

    def test_find_transition_roundtrip(self):
        t = find_transition("raise_to_refine")
        self.assertEqual(t.from_state, IssueState.RAISED)
        self.assertEqual(t.to_state, IssueState.REFINED)


if __name__ == "__main__":
    unittest.main()
