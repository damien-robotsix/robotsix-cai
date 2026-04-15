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
    parse_confidence, parse_resume_target,
    resume_transition_for, resume_pr_transition_for,
    render_pending_marker, parse_pending_marker, strip_pending_marker,
)
from cai_lib.config import (
    LABEL_IN_PROGRESS, LABEL_RAISED, LABEL_REFINED, LABEL_REFINING,
    LABEL_PLANNING, LABEL_PLANNED,
    LABEL_HUMAN_NEEDED, LABEL_PARENT, LABEL_TRIAGING,
    LABEL_APPLYING, LABEL_APPLIED,
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
            if t.min_confidence is not None:
                self.assertIn(f"[≥{t.min_confidence.name}]", result,
                    f"Confidence annotation missing for {t.name!r}")
            else:
                self.assertIn("[caller-gated]", result,
                    f"caller-gated annotation missing for {t.name!r}")

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
        t = find_transition("refining_to_refined")
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
            42, "raise_to_refining",
            current_labels=[LABEL_RAISED],
            set_labels=fake,
        )
        self.assertTrue(ok)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["issue_number"], 42)
        self.assertIn(LABEL_REFINING, calls[0]["add"])
        self.assertIn(LABEL_RAISED, calls[0]["remove"])

    def test_extra_remove_is_forwarded(self):
        calls, fake = self._recording_set_labels()
        apply_transition(
            7, "raise_to_refining",
            current_labels=[LABEL_RAISED],
            extra_remove=[LABEL_PARENT],
            set_labels=fake,
        )
        self.assertIn(LABEL_PARENT, calls[0]["remove"])
        self.assertIn(LABEL_RAISED, calls[0]["remove"])

    def test_state_mismatch_refuses(self):
        calls, fake = self._recording_set_labels()
        ok = apply_transition(
            9, "raise_to_refining",
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
        ok = apply_transition(1, "raise_to_refining", set_labels=fake)
        self.assertTrue(ok)
        self.assertEqual(len(calls), 1)

    def test_find_transition_roundtrip(self):
        t = find_transition("raise_to_refining")
        self.assertEqual(t.from_state, IssueState.RAISED)
        self.assertEqual(t.to_state, IssueState.REFINING)


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

    def _recording_post_comment(self):
        calls = []
        def _fake(issue_number, body, *, log_prefix="cai"):
            calls.append({
                "issue_number": issue_number,
                "body": body,
                "log_prefix": log_prefix,
            })
            return True
        return calls, _fake

    def test_high_confidence_applies_nominal_transition(self):
        calls, fake = self._recording_set_labels()
        comments, fake_comment = self._recording_post_comment()
        ok, diverted = apply_transition_with_confidence(
            11, "refining_to_refined", Confidence.HIGH,
            current_labels=[LABEL_REFINING],
            set_labels=fake,
            post_comment=fake_comment,
        )
        self.assertTrue(ok)
        self.assertFalse(diverted)
        self.assertIn(LABEL_REFINED, calls[0]["add"])
        # No divert → no human-needed comment should be posted.
        self.assertEqual(comments, [])

    def test_medium_confidence_diverts_to_human(self):
        calls, fake = self._recording_set_labels()
        comments, fake_comment = self._recording_post_comment()
        ok, diverted = apply_transition_with_confidence(
            12, "refining_to_refined", Confidence.MEDIUM,
            current_labels=[LABEL_REFINING],
            set_labels=fake,
            post_comment=fake_comment,
        )
        self.assertTrue(ok)
        self.assertTrue(diverted)
        self.assertIn(LABEL_HUMAN_NEEDED, calls[0]["add"])
        self.assertIn(LABEL_REFINING, calls[0]["remove"])
        self.assertNotIn(LABEL_REFINED, calls[0]["add"])
        # Divert → a reason comment must be posted with the failing transition
        # and confidence values so the admin knows why they were summoned.
        self.assertEqual(len(comments), 1)
        body = comments[0]["body"]
        self.assertIn("refining_to_refined", body)
        self.assertIn("MEDIUM", body)
        self.assertIn("HIGH", body)

    def test_missing_confidence_diverts_to_human(self):
        calls, fake = self._recording_set_labels()
        comments, fake_comment = self._recording_post_comment()
        ok, diverted = apply_transition_with_confidence(
            13, "refining_to_refined", None,
            current_labels=[LABEL_REFINING],
            set_labels=fake,
            post_comment=fake_comment,
        )
        self.assertTrue(ok)
        self.assertTrue(diverted)
        self.assertIn(LABEL_HUMAN_NEEDED, calls[0]["add"])
        self.assertEqual(len(comments), 1)
        self.assertIn("MISSING", comments[0]["body"])

    def test_divert_respects_from_state_mismatch(self):
        calls, fake = self._recording_set_labels()
        comments, fake_comment = self._recording_post_comment()
        ok, diverted = apply_transition_with_confidence(
            14, "refining_to_refined", None,
            current_labels=[LABEL_REFINED],  # wrong state
            set_labels=fake,
            post_comment=fake_comment,
        )
        self.assertFalse(ok)
        self.assertFalse(diverted)
        self.assertEqual(calls, [])
        # State mismatch aborts before the divert → no comment either.
        self.assertEqual(comments, [])


class TestPendingMarker(unittest.TestCase):

    def test_roundtrip_with_confidence(self):
        marker = render_pending_marker(
            transition_name="raise_to_refining",
            from_state=IssueState.RAISED,
            intended_state=IssueState.REFINED,
            confidence=Confidence.MEDIUM,
        )
        parsed = parse_pending_marker(f"body text\n{marker}\nmore text")
        self.assertEqual(parsed["transition"], "raise_to_refining")
        self.assertEqual(parsed["from"], "RAISED")
        self.assertEqual(parsed["intended"], "REFINED")
        self.assertEqual(parsed["conf"], "MEDIUM")

    def test_roundtrip_with_missing_confidence(self):
        marker = render_pending_marker(
            transition_name="raise_to_refining",
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
            transition_name="raise_to_refining",
            from_state=IssueState.RAISED,
            intended_state=IssueState.REFINED,
            confidence=Confidence.LOW,
        )
        body = f"leading text\n\n{marker}\n\ntrailing text\n"
        stripped = strip_pending_marker(body)
        self.assertNotIn("cai-fsm-pending", stripped)
        self.assertIn("leading text", stripped)
        self.assertIn("trailing text", stripped)


class TestResumeFromHuman(unittest.TestCase):

    def test_parse_resume_target_valid(self):
        self.assertEqual(parse_resume_target("ResumeTo: REFINED"), "REFINED")
        self.assertEqual(parse_resume_target("lead\nResumeTo: PLAN_APPROVED\ntail"), "PLAN_APPROVED")
        self.assertEqual(parse_resume_target("ResumeTo=SOLVED"), "SOLVED")

    def test_parse_resume_target_missing(self):
        self.assertIsNone(parse_resume_target(""))
        self.assertIsNone(parse_resume_target("no resume line here"))

    def test_resume_transition_for_known_targets(self):
        for name in ("RAISED", "REFINING", "PLAN_APPROVED",
                     "NEEDS_EXPLORATION", "SOLVED"):
            t = resume_transition_for(name)
            self.assertIsNotNone(t, f"no resume transition for {name}")
            self.assertEqual(t.from_state, IssueState.HUMAN_NEEDED)
            self.assertEqual(t.to_state, IssueState[name])

    def test_resume_transition_for_unknown_returns_none(self):
        self.assertIsNone(resume_transition_for("NOT_A_STATE"))
        self.assertIsNone(resume_transition_for(""))
        # States that exist but have no human_to_* path must return None.
        self.assertIsNone(resume_transition_for("IN_PROGRESS"))
        self.assertIsNone(resume_transition_for("MERGED"))
        # REFINED and PLANNED are auto-advance waypoints — admins resume
        # into the transient work states (REFINING / PLANNING) or into
        # PLAN_APPROVED, never into the stable waypoints themselves.
        self.assertIsNone(resume_transition_for("REFINED"))
        self.assertIsNone(resume_transition_for("PLANNED"))
        self.assertIsNone(resume_transition_for("PLANNING"))

    def test_every_widened_transition_is_reachable(self):
        """Every human_to_<state> transition must be discoverable via resume_transition_for."""
        widened = [
            t for t in ISSUE_TRANSITIONS
            if t.from_state == IssueState.HUMAN_NEEDED
        ]
        self.assertGreaterEqual(len(widened), 5)
        for t in widened:
            resolved = resume_transition_for(t.to_state.name)
            self.assertIs(resolved, t,
                f"resume_transition_for({t.to_state.name}) did not return {t.name}")


class TestResumePRTransition(unittest.TestCase):

    def test_known_pr_targets(self):
        for name in ("REVIEWING_CODE", "REVISION_PENDING", "REVIEWING_DOCS"):
            t = resume_pr_transition_for(name)
            self.assertIsNotNone(t, f"no PR resume transition for {name}")
            self.assertEqual(t.from_state, PRState.PR_HUMAN_NEEDED)
            self.assertEqual(t.to_state, PRState[name])

    def test_unknown_returns_none(self):
        self.assertIsNone(resume_pr_transition_for("NOT_A_STATE"))
        self.assertIsNone(resume_pr_transition_for(""))
        # PRState has no OPEN→from-PR_HUMAN_NEEDED path.
        self.assertIsNone(resume_pr_transition_for("OPEN"))
        # MERGED is deliberately excluded — PRs must funnel through
        # the review states; admins never merge from PR_HUMAN_NEEDED
        # directly.
        self.assertIsNone(resume_pr_transition_for("MERGED"))

    def test_issue_and_pr_resolvers_are_disjoint(self):
        # Passing a PRState name to the issue resolver must return None.
        self.assertIsNone(resume_transition_for("REVIEWING_CODE"))
        # Passing an IssueState-only name to the PR resolver must return None.
        self.assertIsNone(resume_pr_transition_for("REFINING"))


class TestRefineNextStepParser(unittest.TestCase):

    def setUp(self):
        # Parser now lives in cai_lib.actions.refine.
        from cai_lib.actions.refine import _parse_refine_next_step
        self._parse = _parse_refine_next_step

    def test_plan(self):
        self.assertEqual(self._parse("body\nNextStep: PLAN\n"), "PLAN")

    def test_explore(self):
        self.assertEqual(self._parse("NextStep: EXPLORE"), "EXPLORE")

    def test_case_insensitive(self):
        self.assertEqual(self._parse("nextstep: explore"), "EXPLORE")

    def test_missing(self):
        self.assertIsNone(self._parse(""))
        self.assertIsNone(self._parse("nothing here"))
        self.assertIsNone(self._parse("NextStep: BOGUS"))


class TestTransientStatesShape(unittest.TestCase):
    """Pin the REFINING / PLANNING transient-state FSM shape."""

    def test_no_direct_raise_to_exploration(self):
        names = {t.name for t in ISSUE_TRANSITIONS}
        self.assertNotIn("raise_to_exploration", names,
            "RAISED must no longer go directly to NEEDS_EXPLORATION")

    def test_refining_to_exploration_exists(self):
        t = find_transition("refining_to_exploration")
        self.assertEqual(t.from_state, IssueState.REFINING)
        self.assertEqual(t.to_state, IssueState.NEEDS_EXPLORATION)

    def test_exploration_loops_back_to_refining(self):
        t = find_transition("exploration_to_refining")
        self.assertEqual(t.from_state, IssueState.NEEDS_EXPLORATION)
        self.assertEqual(t.to_state, IssueState.REFINING)

    def test_raised_only_reaches_refining_or_human(self):
        dests = {
            t.to_state
            for t in ISSUE_TRANSITIONS
            if t.from_state == IssueState.RAISED
        }
        self.assertIn(IssueState.TRIAGING, dests,
            "RAISED must be able to reach TRIAGING via raise_to_triaging")
        self.assertIn(IssueState.REFINING, dests,
            "raise_to_refining bypass must still exist")
        self.assertIn(IssueState.HUMAN_NEEDED, dests,
            "raise_to_human must still exist")

    def test_refining_can_fall_back_to_human(self):
        """Every transient working state must have a path to HUMAN_NEEDED."""
        dests = {
            t.to_state
            for t in ISSUE_TRANSITIONS
            if t.from_state == IssueState.REFINING
        }
        self.assertIn(IssueState.HUMAN_NEEDED, dests)

    def test_planning_can_fall_back_to_human(self):
        dests = {
            t.to_state
            for t in ISSUE_TRANSITIONS
            if t.from_state == IssueState.PLANNING
        }
        self.assertIn(IssueState.HUMAN_NEEDED, dests)

    def test_planned_can_fall_back_to_human(self):
        """PLANNED → PLAN_APPROVED is confidence-gated; explicit human path too."""
        dests = {
            t.to_state
            for t in ISSUE_TRANSITIONS
            if t.from_state == IssueState.PLANNED
        }
        self.assertEqual(dests, {IssueState.PLAN_APPROVED, IssueState.HUMAN_NEEDED})

    def test_refined_only_auto_advances(self):
        """REFINED is a waypoint — the only next stop is PLANNING."""
        dests = {
            t.to_state
            for t in ISSUE_TRANSITIONS
            if t.from_state == IssueState.REFINED
        }
        self.assertEqual(dests, {IssueState.PLANNING})

    def test_no_refine_to_in_progress_shortcut(self):
        """No transition may bypass PLANNED → PLAN_APPROVED en route to IN_PROGRESS."""
        forbidden_pairs = [
            (IssueState.REFINED,  IssueState.IN_PROGRESS),
            (IssueState.REFINING, IssueState.IN_PROGRESS),
            (IssueState.PLANNING, IssueState.IN_PROGRESS),
            (IssueState.PLANNED,  IssueState.IN_PROGRESS),
        ]
        for f, to in forbidden_pairs:
            self.assertFalse(
                any(t.from_state == f and t.to_state == to for t in ISSUE_TRANSITIONS),
                f"No transition may go {f.name} → {to.name}",
            )

    def test_pr_human_cannot_skip_to_merged(self):
        """PR_HUMAN_NEEDED must not bypass the review pipeline to MERGED."""
        forbidden = [
            t for t in PR_TRANSITIONS
            if t.from_state == PRState.PR_HUMAN_NEEDED
            and t.to_state == PRState.MERGED
        ]
        self.assertEqual(forbidden, [],
            "PR_HUMAN_NEEDED → MERGED must not exist; admins funnel back "
            "through REVIEWING_CODE / REVISION_PENDING / REVIEWING_DOCS")


class TestPRStateShape(unittest.TestCase):
    """Pin the redesigned PRState shape (CI_FAILING + REVIEWING_DOCS first-class)."""

    def test_expected_pr_states(self):
        expected = {
            "OPEN", "REVIEWING_CODE", "REVISION_PENDING",
            "REVIEWING_DOCS", "APPROVED", "REBASING", "CI_FAILING", "MERGED",
            "PR_HUMAN_NEEDED",
        }
        self.assertEqual({s.name for s in PRState}, expected)

    def test_ci_failing_reachable_from_all_pre_merge(self):
        """Every pre-merge non-CI_FAILING state must have a path into CI_FAILING."""
        pre_merge = {
            PRState.REVIEWING_CODE, PRState.REVISION_PENDING, PRState.REVIEWING_DOCS,
        }
        have_path = {
            t.from_state for t in PR_TRANSITIONS
            if t.to_state == PRState.CI_FAILING
        }
        self.assertFalse(pre_merge - have_path,
                         f"no *_to_ci_failing from: {pre_merge - have_path}")

    def test_ci_failing_returns_to_reviewing_code(self):
        t = find_transition("ci_failing_to_reviewing_code")
        self.assertEqual(t.from_state, PRState.CI_FAILING)
        self.assertEqual(t.to_state, PRState.REVIEWING_CODE)

    def test_reviewing_docs_to_approved(self):
        """New flow: REVIEWING_DOCS → APPROVED → MERGED (two-step)."""
        # REVIEWING_DOCS outgoing: back to code, APPROVED, REBASING, or CI_FAILING.
        docs_dests = {
            t.to_state
            for t in PR_TRANSITIONS
            if t.from_state == PRState.REVIEWING_DOCS
        }
        self.assertEqual(
            docs_dests,
            {PRState.REVIEWING_CODE, PRState.APPROVED,
             PRState.REBASING, PRState.CI_FAILING},
        )
        # APPROVED must be able to reach MERGED (the terminal step).
        approved_dests = {
            t.to_state
            for t in PR_TRANSITIONS
            if t.from_state == PRState.APPROVED
        }
        self.assertIn(PRState.MERGED, approved_dests)

    def test_get_pr_state_from_labels(self):
        """get_pr_state derives from pipeline labels (post-redesign)."""
        from cai_lib.fsm import get_pr_state
        from cai_lib.config import (
            LABEL_PR_REVIEWING_CODE, LABEL_PR_REVIEWING_DOCS,
            LABEL_PR_CI_FAILING, LABEL_PR_REVISION_PENDING,
            LABEL_PR_HUMAN_NEEDED,
        )
        cases = [
            ([LABEL_PR_REVIEWING_CODE],    PRState.REVIEWING_CODE),
            ([LABEL_PR_REVISION_PENDING],  PRState.REVISION_PENDING),
            ([LABEL_PR_REVIEWING_DOCS],    PRState.REVIEWING_DOCS),
            ([LABEL_PR_CI_FAILING],        PRState.CI_FAILING),
            ([LABEL_PR_HUMAN_NEEDED],      PRState.PR_HUMAN_NEEDED),
            ([],                           PRState.OPEN),
        ]
        for labels, expected in cases:
            pr = {"labels": [{"name": l} for l in labels]}
            self.assertEqual(get_pr_state(pr), expected, f"labels={labels}")

        self.assertEqual(
            get_pr_state({"state": "MERGED", "labels": [{"name": LABEL_PR_REVIEWING_CODE}]}),
            PRState.MERGED,
        )
        self.assertEqual(
            get_pr_state({"mergedAt": "2026-04-01T00:00:00Z", "labels": []}),
            PRState.MERGED,
        )
        both = [{"name": LABEL_PR_REVIEWING_CODE}, {"name": LABEL_PR_CI_FAILING}]
        self.assertEqual(get_pr_state({"labels": both}), PRState.CI_FAILING)


class TestTriagingState(unittest.TestCase):
    """Pin the TRIAGING transient-state FSM shape."""

    def test_raise_to_triaging_exists(self):
        t = find_transition("raise_to_triaging")
        self.assertEqual(t.from_state, IssueState.RAISED)
        self.assertEqual(t.to_state, IssueState.TRIAGING)

    def test_triaging_to_refining_exists(self):
        t = find_transition("triaging_to_refining")
        self.assertEqual(t.from_state, IssueState.TRIAGING)
        self.assertEqual(t.to_state, IssueState.REFINING)

    def test_triaging_to_human_exists(self):
        t = find_transition("triaging_to_human")
        self.assertEqual(t.from_state, IssueState.TRIAGING)
        self.assertEqual(t.to_state, IssueState.HUMAN_NEEDED)

    def test_triaging_can_fall_back_to_human(self):
        """TRIAGING must have an explicit path to HUMAN_NEEDED."""
        dests = {
            t.to_state
            for t in ISSUE_TRANSITIONS
            if t.from_state == IssueState.TRIAGING
        }
        self.assertIn(IssueState.HUMAN_NEEDED, dests)

    def test_raise_to_refining_bypass_still_exists(self):
        """Direct bypass from RAISED → REFINING must remain."""
        t = find_transition("raise_to_refining")
        self.assertEqual(t.from_state, IssueState.RAISED)
        self.assertEqual(t.to_state, IssueState.REFINING)


class TestTriagingSkipAheadPaths(unittest.TestCase):
    """Step 2 skip-ahead paths: TRIAGING → PLAN_APPROVED / APPLYING."""

    def test_applying_in_issue_state(self):
        self.assertIn("APPLYING", [s.name for s in IssueState])

    def test_applied_in_issue_state(self):
        self.assertIn("APPLIED", [s.name for s in IssueState])

    def test_triaging_to_plan_approved_exists(self):
        t = find_transition("triaging_to_plan_approved")
        self.assertEqual(t.from_state, IssueState.TRIAGING)
        self.assertEqual(t.to_state, IssueState.PLAN_APPROVED)

    def test_triaging_to_applying_exists(self):
        t = find_transition("triaging_to_applying")
        self.assertEqual(t.from_state, IssueState.TRIAGING)
        self.assertEqual(t.to_state, IssueState.APPLYING)

    def test_applying_to_applied_requires_high_confidence(self):
        t = find_transition("applying_to_applied")
        self.assertTrue(t.accepts(Confidence.HIGH))
        self.assertFalse(t.accepts(Confidence.MEDIUM))
        self.assertFalse(t.accepts(Confidence.LOW))

    def test_applying_to_human_exists(self):
        t = find_transition("applying_to_human")
        self.assertEqual(t.from_state, IssueState.APPLYING)
        self.assertEqual(t.to_state, IssueState.HUMAN_NEEDED)

    def test_applied_to_solved_exists(self):
        t = find_transition("applied_to_solved")
        self.assertEqual(t.from_state, IssueState.APPLIED)
        self.assertEqual(t.to_state, IssueState.SOLVED)

    def test_applying_has_human_fallback(self):
        dests = {
            t.to_state
            for t in ISSUE_TRANSITIONS
            if t.from_state == IssueState.APPLYING
        }
        self.assertIn(IssueState.HUMAN_NEEDED, dests)

    def test_gate_override_low_skip_confidence(self):
        """triaging_to_plan_approved has no FSM-level confidence gate.

        Gating is done at the application level in cmd_triage. Confirm
        the transition itself carries no min_confidence requirement.
        """
        t = find_transition("triaging_to_plan_approved")
        # The transition itself has no min_confidence; gating is done in cmd_triage
        # at the application level.
        self.assertIsNone(t.min_confidence)

    def test_triaging_to_applying_has_no_fsm_gate(self):
        """triaging_to_applying also carries no FSM-level confidence gate."""
        t = find_transition("triaging_to_applying")
        self.assertIsNone(t.min_confidence)


if __name__ == "__main__":
    unittest.main()
