"""Tests for cai_lib.fsm — FSM data structures."""
import sys
import os
import unittest
from collections import deque

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cai_lib.fsm import (
    IssueState, PRState, Transition, Confidence,
    ISSUE_TRANSITIONS, PR_TRANSITIONS,
    get_issue_state, render_fsm_mermaid,
    apply_transition, apply_transition_with_confidence, find_transition,
    parse_confidence,
    render_pending_marker, parse_pending_marker, strip_pending_marker,
)
from cai_lib.config import (
    LABEL_IN_PROGRESS, LABEL_RAISED, LABEL_REFINED, LABEL_HUMAN_SUBMITTED,
    LABEL_HUMAN_NEEDED,
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
            self.assertIn(f"[≥{t.min_confidence.name}]", result,
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


class TestConfidenceEnum(unittest.TestCase):

    def test_ordering(self):
        self.assertTrue(Confidence.LOW < Confidence.MEDIUM < Confidence.HIGH)
        self.assertTrue(Confidence.HIGH >= Confidence.HIGH)
        self.assertFalse(Confidence.LOW >= Confidence.HIGH)

    def test_parse_valid(self):
        self.assertEqual(parse_confidence("Confidence: HIGH"), Confidence.HIGH)
        self.assertEqual(parse_confidence("some text\nConfidence: medium\nmore"), Confidence.MEDIUM)
        self.assertEqual(parse_confidence("Confidence=LOW"), Confidence.LOW)

    def test_parse_missing_returns_none(self):
        self.assertIsNone(parse_confidence(""))
        self.assertIsNone(parse_confidence("no confidence line here"))
        self.assertIsNone(parse_confidence("Confidence: BOGUS"))

    def test_transition_accepts(self):
        t = find_transition("raise_to_refine")
        # default threshold is HIGH
        self.assertTrue(t.accepts(Confidence.HIGH))
        self.assertFalse(t.accepts(Confidence.MEDIUM))
        self.assertFalse(t.accepts(None))


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
            current_labels=[LABEL_REFINED],
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


class TestApplyTransitionWithConfidence(unittest.TestCase):

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

    def test_high_confidence_applies_nominal_transition(self):
        calls, fake = self._recording_set_labels()
        ok, diverted = apply_transition_with_confidence(
            11, "raise_to_refine", Confidence.HIGH,
            current_labels=[LABEL_RAISED],
            set_labels=fake,
        )
        self.assertTrue(ok)
        self.assertFalse(diverted)
        self.assertIn(LABEL_REFINED, calls[0]["add"])

    def test_medium_confidence_diverts_to_human(self):
        calls, fake = self._recording_set_labels()
        ok, diverted = apply_transition_with_confidence(
            12, "raise_to_refine", Confidence.MEDIUM,
            current_labels=[LABEL_RAISED],
            set_labels=fake,
        )
        self.assertTrue(ok)
        self.assertTrue(diverted)
        self.assertIn(LABEL_HUMAN_NEEDED, calls[0]["add"])
        self.assertIn(LABEL_RAISED, calls[0]["remove"])
        self.assertNotIn(LABEL_REFINED, calls[0]["add"])

    def test_missing_confidence_diverts_to_human(self):
        calls, fake = self._recording_set_labels()
        ok, diverted = apply_transition_with_confidence(
            13, "raise_to_refine", None,
            current_labels=[LABEL_RAISED],
            set_labels=fake,
        )
        self.assertTrue(ok)
        self.assertTrue(diverted)
        self.assertIn(LABEL_HUMAN_NEEDED, calls[0]["add"])

    def test_divert_respects_from_state_mismatch(self):
        calls, fake = self._recording_set_labels()
        ok, diverted = apply_transition_with_confidence(
            14, "raise_to_refine", None,
            current_labels=[LABEL_REFINED],  # wrong state
            set_labels=fake,
        )
        self.assertFalse(ok)
        self.assertFalse(diverted)
        self.assertEqual(calls, [])


class TestPendingMarker(unittest.TestCase):

    def test_roundtrip_with_confidence(self):
        marker = render_pending_marker(
            transition_name="raise_to_refine",
            from_state=IssueState.RAISED,
            intended_state=IssueState.REFINED,
            confidence=Confidence.MEDIUM,
        )
        parsed = parse_pending_marker(f"body text\n{marker}\nmore text")
        self.assertEqual(parsed["transition"], "raise_to_refine")
        self.assertEqual(parsed["from"], "RAISED")
        self.assertEqual(parsed["intended"], "REFINED")
        self.assertEqual(parsed["conf"], "MEDIUM")

    def test_roundtrip_with_missing_confidence(self):
        marker = render_pending_marker(
            transition_name="raise_to_refine",
            from_state=IssueState.RAISED,
            intended_state=IssueState.REFINED,
            confidence=None,
        )
        parsed = parse_pending_marker(marker)
        self.assertEqual(parsed["conf"], "MISSING")

    def test_parse_returns_none_when_absent(self):
        self.assertIsNone(parse_pending_marker("a plain issue body"))
        self.assertIsNone(parse_pending_marker(""))

    def test_strip_removes_marker(self):
        marker = render_pending_marker(
            transition_name="raise_to_refine",
            from_state=IssueState.RAISED,
            intended_state=IssueState.REFINED,
            confidence=Confidence.LOW,
        )
        body = f"leading text\n\n{marker}\n\ntrailing text\n"
        stripped = strip_pending_marker(body)
        self.assertNotIn("cai-fsm-pending", stripped)
        self.assertIn("leading text", stripped)
        self.assertIn("trailing text", stripped)


if __name__ == "__main__":
    unittest.main()
