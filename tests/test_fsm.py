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
    find_transition,
    parse_confidence, parse_confidence_reason, parse_resume_target,
    fire_trigger,
)
from cai_lib.config import (
    LABEL_IN_PROGRESS, LABEL_RAISED, LABEL_REFINED, LABEL_REFINING,
    LABEL_PLANNING, LABEL_PLANNED,
    LABEL_HUMAN_NEEDED, LABEL_PARENT, LABEL_TRIAGING,
    LABEL_APPLYING, LABEL_APPLIED,
    LABEL_KIND_CODE, LABEL_KIND_MAINTENANCE,
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

    def test_parse_tolerates_markdown_and_punctuation(self):
        # Bolded label, bolded level, trailing period — all common
        # markdown variants the select agent may emit. See issue #685.
        self.assertEqual(parse_confidence("**Confidence:** HIGH"), Confidence.HIGH)
        self.assertEqual(parse_confidence("Confidence: **HIGH**"), Confidence.HIGH)
        self.assertEqual(parse_confidence("**Confidence:** **MEDIUM**"), Confidence.MEDIUM)
        self.assertEqual(parse_confidence("Confidence: HIGH."), Confidence.HIGH)

    def test_parse_rejects_pipe_menu_echo(self):
        # The agent echoing the template verbatim must NOT count as a
        # valid confidence line — that would pick whichever level the
        # regex happens to land on and bypass the human gate.
        self.assertIsNone(
            parse_confidence("Confidence: HIGH | MEDIUM | LOW")
        )

    def test_parse_reason_valid(self):
        body = "Confidence: MEDIUM\nConfidence reason: The plan has unverified assumptions."
        self.assertEqual(
            parse_confidence_reason(body),
            "The plan has unverified assumptions.",
        )

    def test_parse_reason_multiword(self):
        body = (
            "Confidence: LOW\n"
            "Confidence reason: Plan 1 and Plan 2 contradict each other on "
            "which file to edit, leaving ambiguous scope for the fix agent.\n"
        )
        self.assertEqual(
            parse_confidence_reason(body),
            "Plan 1 and Plan 2 contradict each other on which file to edit, "
            "leaving ambiguous scope for the fix agent.",
        )

    def test_parse_reason_missing_returns_none(self):
        self.assertIsNone(parse_confidence_reason(""))
        self.assertIsNone(parse_confidence_reason("Confidence: HIGH"))
        self.assertIsNone(parse_confidence_reason("no reason line here"))

    def test_parse_reason_case_insensitive(self):
        body = "CONFIDENCE REASON: Missing edge cases for empty input."
        self.assertEqual(
            parse_confidence_reason(body),
            "Missing edge cases for empty input.",
        )

    def test_transition_accepts(self):
        t = find_transition("refining_to_refined")
        # default threshold is HIGH
        self.assertTrue(t.accepts(Confidence.HIGH))
        self.assertFalse(t.accepts(Confidence.MEDIUM))
        self.assertFalse(t.accepts(None))


class TestPlannedToPlanApprovedMitigated(unittest.TestCase):
    """#918 — MEDIUM-gated sibling of planned_to_plan_approved."""

    def test_mitigated_transition_accepts_medium(self):
        t = find_transition("planned_to_plan_approved_mitigated")
        self.assertEqual(t.min_confidence, Confidence.MEDIUM)
        self.assertTrue(t.accepts(Confidence.HIGH))
        self.assertTrue(t.accepts(Confidence.MEDIUM))
        self.assertFalse(t.accepts(Confidence.LOW))
        self.assertFalse(t.accepts(None))

    def test_mitigated_transition_shares_label_move(self):
        default = find_transition("planned_to_plan_approved")
        mitigated = find_transition("planned_to_plan_approved_mitigated")
        self.assertEqual(default.from_state, mitigated.from_state)
        self.assertEqual(default.to_state, mitigated.to_state)
        self.assertEqual(default.labels_add, mitigated.labels_add)
        self.assertEqual(default.labels_remove, mitigated.labels_remove)
        # Only the threshold differs between the two siblings.
        self.assertEqual(default.min_confidence, Confidence.HIGH)
        self.assertEqual(mitigated.min_confidence, Confidence.MEDIUM)

    def test_default_transition_still_requires_high(self):
        # Regression guard: the mitigated transition must not replace the
        # HIGH-gated default — both must coexist, one for each path.
        default = find_transition("planned_to_plan_approved")
        self.assertEqual(default.min_confidence, Confidence.HIGH)


class TestApplyingToAppliedInferredOps(unittest.TestCase):
    """#986 — MEDIUM-gated sibling of applying_to_applied for inferred-ops runs."""

    def test_inferred_ops_transition_accepts_medium(self):
        t = find_transition("applying_to_applied_inferred_ops")
        self.assertEqual(t.min_confidence, Confidence.MEDIUM)
        self.assertTrue(t.accepts(Confidence.HIGH))
        self.assertTrue(t.accepts(Confidence.MEDIUM))
        self.assertFalse(t.accepts(Confidence.LOW))
        self.assertFalse(t.accepts(None))

    def test_inferred_ops_shares_label_move(self):
        default = find_transition("applying_to_applied")
        inferred = find_transition("applying_to_applied_inferred_ops")
        self.assertEqual(default.from_state, inferred.from_state)
        self.assertEqual(default.to_state, inferred.to_state)
        self.assertEqual(default.labels_add, inferred.labels_add)
        self.assertEqual(default.labels_remove, inferred.labels_remove)
        # Only the threshold differs between the two siblings.
        self.assertEqual(default.min_confidence, Confidence.HIGH)
        self.assertEqual(inferred.min_confidence, Confidence.MEDIUM)

    def test_default_transition_still_requires_high(self):
        # Regression guard: the inferred-ops sibling must not replace the
        # HIGH-gated default — both must coexist, one for each path.
        default = find_transition("applying_to_applied")
        self.assertEqual(default.min_confidence, Confidence.HIGH)


class TestPlannedToPlanApprovedDocsOnly(unittest.TestCase):
    """#989 — MEDIUM-gated sibling of planned_to_plan_approved for
    documentation-only plans (structural relaxation)."""

    def test_docs_only_transition_accepts_medium(self):
        t = find_transition("planned_to_plan_approved_docs_only")
        self.assertEqual(t.min_confidence, Confidence.MEDIUM)
        self.assertTrue(t.accepts(Confidence.HIGH))
        self.assertTrue(t.accepts(Confidence.MEDIUM))
        self.assertFalse(t.accepts(Confidence.LOW))
        self.assertFalse(t.accepts(None))

    def test_docs_only_shares_label_move(self):
        default = find_transition("planned_to_plan_approved")
        docs_only = find_transition("planned_to_plan_approved_docs_only")
        self.assertEqual(default.from_state, docs_only.from_state)
        self.assertEqual(default.to_state, docs_only.to_state)
        self.assertEqual(default.labels_add, docs_only.labels_add)
        self.assertEqual(default.labels_remove, docs_only.labels_remove)
        # Only the threshold differs between the two siblings.
        self.assertEqual(default.min_confidence, Confidence.HIGH)
        self.assertEqual(docs_only.min_confidence, Confidence.MEDIUM)

    def test_default_and_mitigated_transitions_unchanged(self):
        # Regression guard: the docs-only sibling must not replace the
        # HIGH-gated default or the anchor-mitigation sibling — all
        # three must coexist with their original thresholds.
        self.assertEqual(
            find_transition("planned_to_plan_approved").min_confidence,
            Confidence.HIGH,
        )
        self.assertEqual(
            find_transition("planned_to_plan_approved_mitigated").min_confidence,
            Confidence.MEDIUM,
        )


class TestPlannedToPlanApprovedApprovable(unittest.TestCase):
    """#1008 — MEDIUM-gated sibling of planned_to_plan_approved for
    cai-select-flagged soft-risk plans (approvable_at_medium=true)."""

    def test_approvable_transition_accepts_medium(self):
        t = find_transition("planned_to_plan_approved_approvable")
        self.assertEqual(t.min_confidence, Confidence.MEDIUM)
        self.assertTrue(t.accepts(Confidence.HIGH))
        self.assertTrue(t.accepts(Confidence.MEDIUM))
        self.assertFalse(t.accepts(Confidence.LOW))
        self.assertFalse(t.accepts(None))

    def test_approvable_shares_label_move(self):
        default = find_transition("planned_to_plan_approved")
        approvable = find_transition("planned_to_plan_approved_approvable")
        self.assertEqual(default.from_state, approvable.from_state)
        self.assertEqual(default.to_state, approvable.to_state)
        self.assertEqual(default.labels_add, approvable.labels_add)
        self.assertEqual(default.labels_remove, approvable.labels_remove)
        # Only the threshold differs between the two siblings.
        self.assertEqual(default.min_confidence, Confidence.HIGH)
        self.assertEqual(approvable.min_confidence, Confidence.MEDIUM)

    def test_all_planned_siblings_coexist(self):
        # Regression guard: the approvable sibling must not replace the
        # HIGH-gated default or either of the existing MEDIUM siblings —
        # all four must coexist with their original thresholds.
        self.assertEqual(
            find_transition("planned_to_plan_approved").min_confidence,
            Confidence.HIGH,
        )
        self.assertEqual(
            find_transition("planned_to_plan_approved_mitigated").min_confidence,
            Confidence.MEDIUM,
        )
        self.assertEqual(
            find_transition("planned_to_plan_approved_docs_only").min_confidence,
            Confidence.MEDIUM,
        )


class TestPlanApprovedToRefined(unittest.TestCase):
    """#1142 — admin-sigil-driven rollback from :plan-approved to :refined."""

    def test_transition_exists(self):
        t = find_transition("plan_approved_to_refined")
        self.assertEqual(t.from_state, IssueState.PLAN_APPROVED)
        self.assertEqual(t.to_state, IssueState.REFINED)

    def test_transition_is_caller_gated(self):
        # The sigil literal-string match is the sole gate — no FSM-level
        # confidence threshold. accepts() therefore returns True for every
        # confidence value (including None).
        t = find_transition("plan_approved_to_refined")
        self.assertIsNone(t.min_confidence)
        self.assertTrue(t.accepts(Confidence.HIGH))
        self.assertTrue(t.accepts(Confidence.MEDIUM))
        self.assertTrue(t.accepts(Confidence.LOW))
        self.assertTrue(t.accepts(None))

    def test_label_move_is_plan_approved_to_refined(self):
        from cai_lib.config import LABEL_PLAN_APPROVED, LABEL_REFINED
        t = find_transition("plan_approved_to_refined")
        self.assertEqual(t.labels_remove, [LABEL_PLAN_APPROVED])
        self.assertEqual(t.labels_add, [LABEL_REFINED])

    def test_sibling_transition_still_exists(self):
        # Regression guard: the new sibling must not replace
        # ``approved_to_in_progress`` — both must coexist so the
        # default handle_implement path survives.
        default = find_transition("approved_to_in_progress")
        self.assertEqual(default.from_state, IssueState.PLAN_APPROVED)


class TestBackfillSilentHumanNeeded(unittest.TestCase):
    """Pins the self-healing backfill sweep (#1009, #932)."""

    def test_backfills_issues_without_marker_comment(self):
        from cai_lib.fsm import backfill_silent_human_needed_comments

        # Simulate two parked issues: one silent (no MARKER comment),
        # one already has a MARKER comment and must be skipped. Both
        # are normal implementation issues — neither carries the
        # rescue-prev fingerprint, so the auto-close branch must NOT
        # fire even though close_issue defaults to the real helper.
        issue_lists = {
            LABEL_HUMAN_NEEDED: [
                {
                    "number": 932,
                    "title": "Refactor frobnicator",
                    "body": "Move the frobnicator into cai_lib/frob.py.",
                    "labels": [{"name": LABEL_HUMAN_NEEDED}],
                    "comments": [{"body": "some unrelated comment"}],
                },
                {
                    "number": 980,
                    "title": "Already-resolved divert",
                    "body": "(implementation task body)",
                    "labels": [{"name": LABEL_HUMAN_NEEDED}],
                    "comments": [
                        {"body": "**🙋 Human attention needed**\n\n..."}
                    ],
                },
            ],
        }

        def _fake_gh_json(argv):
            # argv like: ["issue","list","--repo",REPO,"--label",LBL,...]
            for i, tok in enumerate(argv):
                if tok == "--label" and i + 1 < len(argv):
                    return issue_lists.get(argv[i + 1], [])
            return []

        posted = []
        closed = []

        def _fake_post_issue(n, body, *, log_prefix="cai"):
            posted.append({"n": n, "body": body})

        def _fake_post_pr(n, body, *, log_prefix="cai"):
            posted.append({"n": n, "body": body})

        def _fake_close(n, body, *, log_prefix="cai"):
            closed.append({"n": n, "body": body})

        backfilled = backfill_silent_human_needed_comments(
            gh_json=_fake_gh_json,
            post_issue_comment=_fake_post_issue,
            post_pr_comment=_fake_post_pr,
            close_issue=_fake_close,
        )

        self.assertEqual(backfilled, [("issue", 932)])
        self.assertEqual(len(posted), 1)
        self.assertEqual(posted[0]["n"], 932)
        self.assertIn("🙋 Human attention needed", posted[0]["body"])
        # Implementation-task suggested-action paragraph.
        self.assertIn("human:solved", posted[0]["body"])
        # Auto-close branch must not fire for non-prevention issues.
        self.assertEqual(closed, [])

    def test_auto_closes_prevention_finding_with_no_structural_prevention(self):
        """#1150 — rescue prevention findings whose remediation is
        'No structural prevention needed' must be auto-closed (not
        backfilled) so they stop cycling through the rescue agent."""
        from cai_lib.fsm import backfill_silent_human_needed_comments

        issue_lists = {
            LABEL_HUMAN_NEEDED: [
                {
                    "number": 1145,
                    "title": (
                        "Rescue prevention: The retries-exhausted guard "
                        "is working correctly."
                    ),
                    "body": (
                        "<!-- fingerprint: rescue-prev-1fdf080c5e26341c -->\n"
                        "**Category:** `reliability`\n\n"
                        "## Remediation\n\n"
                        "The guard is fine. No structural prevention "
                        "needed — the Opus escalation path is the "
                        "intended resolution.\n"
                    ),
                    "labels": [{"name": LABEL_HUMAN_NEEDED}],
                    "comments": [],
                },
            ],
        }

        def _fake_gh_json(argv):
            for i, tok in enumerate(argv):
                if tok == "--label" and i + 1 < len(argv):
                    return issue_lists.get(argv[i + 1], [])
            return []

        posted = []
        closed = []

        def _fake_post(n, body, *, log_prefix="cai"):
            posted.append({"n": n, "body": body})

        def _fake_close(n, body, *, log_prefix="cai"):
            closed.append({"n": n, "body": body})

        handled = backfill_silent_human_needed_comments(
            gh_json=_fake_gh_json,
            post_issue_comment=_fake_post,
            post_pr_comment=_fake_post,
            close_issue=_fake_close,
        )

        self.assertEqual(handled, [("issue", 1145)])
        # Comment posters MUST NOT fire on the auto-close path.
        self.assertEqual(posted, [])
        # close_issue MUST be called with a body that names the
        # auto-close path so the audit trail is unambiguous.
        self.assertEqual(len(closed), 1)
        self.assertEqual(closed[0]["n"], 1145)
        self.assertIn("No structural prevention needed", closed[0]["body"])
        self.assertIn("Auto-closed", closed[0]["body"])
        self.assertIn("issue #1150", closed[0]["body"])

    def test_prevention_finding_without_phrase_gets_typed_comment(self):
        """#1150 — a rescue prevention finding lacking the auto-close
        phrase must still be backfilled, and the comment must contain
        the prevention-typed suggested-action paragraph rather than
        the generic implementation-task wording."""
        from cai_lib.fsm import backfill_silent_human_needed_comments

        issue_lists = {
            LABEL_HUMAN_NEEDED: [
                {
                    "number": 1146,
                    "title": (
                        "Rescue prevention: Add a blocked-on label "
                        "for cyclic dependencies"
                    ),
                    "body": (
                        "<!-- fingerprint: rescue-prev-deadbeefcafebabe -->\n"
                        "**Category:** `reliability`\n\n"
                        "## Remediation\n\n"
                        "Add a `blocked-on:<N>` label mechanic to the "
                        "rescue dispatcher.\n"
                    ),
                    "labels": [{"name": LABEL_HUMAN_NEEDED}],
                    "comments": [],
                },
            ],
        }

        def _fake_gh_json(argv):
            for i, tok in enumerate(argv):
                if tok == "--label" and i + 1 < len(argv):
                    return issue_lists.get(argv[i + 1], [])
            return []

        posted = []
        closed = []

        def _fake_post(n, body, *, log_prefix="cai"):
            posted.append({"n": n, "body": body})

        def _fake_close(n, body, *, log_prefix="cai"):
            closed.append({"n": n, "body": body})

        handled = backfill_silent_human_needed_comments(
            gh_json=_fake_gh_json,
            post_issue_comment=_fake_post,
            post_pr_comment=_fake_post,
            close_issue=_fake_close,
        )

        self.assertEqual(handled, [("issue", 1146)])
        self.assertEqual(closed, [])
        self.assertEqual(len(posted), 1)
        self.assertEqual(posted[0]["n"], 1146)
        # Prevention-typed suggested-action paragraph must mention the
        # three options the admin can take.
        self.assertIn("rescue prevention finding", posted[0]["body"])
        self.assertIn("close this issue as completed", posted[0]["body"])
        self.assertIn("close this issue as not planned", posted[0]["body"])
        self.assertIn("human:solved", posted[0]["body"])

    def test_pr_targets_never_take_auto_close_path(self):
        """#1150 — the auto-close branch is issue-only. Even if a PR
        body coincidentally contains the prevention-finding fingerprint
        and the auto-close phrase (impossible in practice but worth
        pinning), the PR must be backfilled via comment, not closed."""
        from cai_lib.config import LABEL_PR_HUMAN_NEEDED
        from cai_lib.fsm import backfill_silent_human_needed_comments

        issue_lists = {
            LABEL_PR_HUMAN_NEEDED: [
                {
                    "number": 4242,
                    "title": "PR with weird body",
                    "body": (
                        "<!-- fingerprint: rescue-prev-faketoken -->\n"
                        "No structural prevention needed."
                    ),
                    "labels": [{"name": LABEL_PR_HUMAN_NEEDED}],
                    "comments": [],
                },
            ],
        }

        def _fake_gh_json(argv):
            for i, tok in enumerate(argv):
                if tok == "--label" and i + 1 < len(argv):
                    return issue_lists.get(argv[i + 1], [])
            return []

        posted = []
        closed = []

        def _fake_post(n, body, *, log_prefix="cai"):
            posted.append({"n": n, "body": body})

        def _fake_close(n, body, *, log_prefix="cai"):
            closed.append({"n": n, "body": body})

        handled = backfill_silent_human_needed_comments(
            gh_json=_fake_gh_json,
            post_issue_comment=_fake_post,
            post_pr_comment=_fake_post,
            close_issue=_fake_close,
        )

        self.assertEqual(handled, [("pr", 4242)])
        self.assertEqual(closed, [])
        self.assertEqual(len(posted), 1)
        self.assertEqual(posted[0]["n"], 4242)
        # PR comments use the implementation-task suggested action
        # (PRs are never rescue prevention findings even if their body
        # happens to contain the fingerprint string).
        self.assertIn("human:solved", posted[0]["body"])


class TestResumeFromHuman(unittest.TestCase):

    def test_parse_resume_target_valid(self):
        self.assertEqual(parse_resume_target("ResumeTo: REFINED"), "REFINED")
        self.assertEqual(parse_resume_target("lead\nResumeTo: PLAN_APPROVED\ntail"), "PLAN_APPROVED")
        self.assertEqual(parse_resume_target("ResumeTo=SOLVED"), "SOLVED")

    def test_parse_resume_target_missing(self):
        self.assertIsNone(parse_resume_target(""))
        self.assertIsNone(parse_resume_target("no resume line here"))


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
        """PLANNED → PLAN_APPROVED is confidence-gated; explicit human path too.
        Post-plan re-split (#1167) adds PLANNED → SPLITTING via planned_to_splitting."""
        dests = {
            t.to_state
            for t in ISSUE_TRANSITIONS
            if t.from_state == IssueState.PLANNED
        }
        self.assertEqual(dests, {IssueState.PLAN_APPROVED, IssueState.HUMAN_NEEDED, IssueState.SPLITTING})

    def test_refined_advances_to_splitting_or_planning(self):
        """REFINED is a waypoint — next stop is either SPLITTING
        (normal drive via cai-split) or PLANNING (legacy compat)."""
        dests = {
            t.to_state
            for t in ISSUE_TRANSITIONS
            if t.from_state == IssueState.REFINED
        }
        self.assertEqual(dests, {IssueState.SPLITTING, IssueState.PLANNING})

    def test_splitting_routes_to_planning_or_human(self):
        """SPLITTING exits only to PLANNING (atomic) or HUMAN_NEEDED (LOW)."""
        dests = {
            t.to_state
            for t in ISSUE_TRANSITIONS
            if t.from_state == IssueState.SPLITTING
        }
        self.assertEqual(dests, {IssueState.PLANNING, IssueState.HUMAN_NEEDED})

    def test_no_refine_to_in_progress_shortcut(self):
        """No transition may bypass PLANNED → PLAN_APPROVED en route to IN_PROGRESS."""
        forbidden_pairs = [
            (IssueState.REFINED,   IssueState.IN_PROGRESS),
            (IssueState.REFINING,  IssueState.IN_PROGRESS),
            (IssueState.SPLITTING, IssueState.IN_PROGRESS),
            (IssueState.PLANNING,  IssueState.IN_PROGRESS),
            (IssueState.PLANNED,   IssueState.IN_PROGRESS),
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
        # APPROVED must also be able to reach PR_HUMAN_NEEDED so the
        # merge handler's "hold" / recovery paths can park the PR
        # cleanly instead of layering an orthogonal needs-human-review
        # flag on top of pr:approved (which made the dispatcher loop).
        self.assertIn(PRState.PR_HUMAN_NEEDED, approved_dests)
        approved_to_human = next(
            (t for t in PR_TRANSITIONS if t.name == "approved_to_human"),
            None,
        )
        self.assertIsNotNone(approved_to_human)
        from cai_lib.config import LABEL_PR_APPROVED, LABEL_PR_HUMAN_NEEDED
        self.assertIn(LABEL_PR_APPROVED, approved_to_human.labels_remove)
        self.assertIn(LABEL_PR_HUMAN_NEEDED, approved_to_human.labels_add)

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


class TestTriagingHandlerPairCheck(unittest.TestCase):
    """handle_triage() must reject inconsistent RoutingDecision↔kind pairs."""

    def _make_issue(self, number=999):
        return {
            "number": number,
            "title": "Test issue",
            "body": "some body",
            "labels": [{"name": LABEL_TRIAGING}],
        }

    def _make_agent_result(self, verdict: dict):
        import json
        import subprocess
        return subprocess.CompletedProcess(
            args=["claude"], returncode=0,
            stdout=json.dumps(verdict), stderr="",
        )

    def test_apply_with_code_kind_falls_through_to_refine(self):
        """APPLY + kind:code + skip_confidence:HIGH → triaging_to_refining (not triaging_to_applying)."""
        import json
        import subprocess
        from unittest import mock
        import cai_lib.actions.triage as T

        verdict = {
            "routing_decision": "APPLY",
            "routing_confidence": "HIGH",
            "kind": "code",
            "skip_confidence": "HIGH",
            "reasoning": "looks like a maintenance job",
        }
        fake_result = subprocess.CompletedProcess(
            args=["claude"], returncode=0,
            stdout=json.dumps(verdict), stderr="",
        )
        transitions_called = []

        def fake_apply_transition(issue_number, trigger_name, **kwargs):
            transitions_called.append(trigger_name)
            return True, False

        labels_added = []

        def fake_set_labels(issue_number, *, add=(), remove=(), log_prefix="cai"):
            labels_added.extend(add)
            return True

        with mock.patch.object(T, "_run_claude_p", return_value=fake_result), \
             mock.patch.object(T, "fire_trigger", side_effect=fake_apply_transition), \
             mock.patch.object(T, "_set_labels", side_effect=fake_set_labels), \
             mock.patch.object(T, "check_duplicate_or_resolved", return_value=None), \
             mock.patch.object(T, "log_run"):
            rc = T.handle_triage(self._make_issue())

        self.assertEqual(rc, 0)
        self.assertIn("triaging_to_refining", transitions_called)
        self.assertNotIn("triaging_to_applying", transitions_called)

    def test_plan_approve_with_maintenance_kind_falls_through_to_refine(self):
        """PLAN_APPROVE + kind:maintenance + skip_confidence:HIGH → triaging_to_refining (not triaging_to_plan_approved)."""
        import json
        import subprocess
        from unittest import mock
        import cai_lib.actions.triage as T

        verdict = {
            "routing_decision": "PLAN_APPROVE",
            "routing_confidence": "HIGH",
            "kind": "maintenance",
            "skip_confidence": "HIGH",
            "reasoning": "looks like a code job",
        }
        fake_result = subprocess.CompletedProcess(
            args=["claude"], returncode=0,
            stdout=json.dumps(verdict), stderr="",
        )
        transitions_called = []

        def fake_apply_transition(issue_number, trigger_name, **kwargs):
            transitions_called.append(trigger_name)
            return True, False

        labels_added = []

        def fake_set_labels(issue_number, *, add=(), remove=(), log_prefix="cai"):
            labels_added.extend(add)
            return True

        with mock.patch.object(T, "_run_claude_p", return_value=fake_result), \
             mock.patch.object(T, "fire_trigger", side_effect=fake_apply_transition), \
             mock.patch.object(T, "_set_labels", side_effect=fake_set_labels), \
             mock.patch.object(T, "check_duplicate_or_resolved", return_value=None), \
             mock.patch.object(T, "log_run"):
            rc = T.handle_triage(self._make_issue())

        self.assertEqual(rc, 0)
        self.assertIn("triaging_to_refining", transitions_called)
        self.assertNotIn("triaging_to_plan_approved", transitions_called)


class TestTriagingHandlerOpsValidation(unittest.TestCase):
    """handle_triage() must reject APPLY+maintenance verdicts whose ops
    body is prose rather than cai-maintain op lines, re-routing the
    issue into the REFINE pathway with kind:code (issue #981)."""

    def _make_issue(self, number=981):
        return {
            "number": number,
            "title": "Pin claude-code version",
            "body": "Some best-practice finding.",
            "labels": [{"name": LABEL_TRIAGING}],
        }

    def test_ops_validator_accepts_valid_op_lines(self):
        from cai_lib.actions.triage import _ops_body_has_valid_maintenance_op
        self.assertTrue(_ops_body_has_valid_maintenance_op(
            "1. label add 42 kind:code\n"))
        self.assertTrue(_ops_body_has_valid_maintenance_op(
            "- close 12"))
        self.assertTrue(_ops_body_has_valid_maintenance_op(
            "label remove 7 stale"))
        self.assertTrue(_ops_body_has_valid_maintenance_op(
            "workflow edit .github/workflows/a.yml on push"))

    def test_ops_validator_rejects_prose_and_empty(self):
        from cai_lib.actions.triage import _ops_body_has_valid_maintenance_op
        self.assertFalse(_ops_body_has_valid_maintenance_op(
            "1. Open the Dockerfile and locate @latest\n"
            "2. Replace with @2.1.114 in the npm install line\n"))
        self.assertFalse(_ops_body_has_valid_maintenance_op(None))
        self.assertFalse(_ops_body_has_valid_maintenance_op(""))

    def test_apply_with_prose_ops_reroutes_to_refine_as_code(self):
        """APPLY+maintenance+HIGH with implement-style prose in ``ops``
        must fall through to REFINE and apply kind:code (not kind:maintenance)."""
        import json
        import subprocess
        from unittest import mock
        import cai_lib.actions.triage as T

        verdict = {
            "routing_decision": "APPLY",
            "routing_confidence": "HIGH",
            "kind": "maintenance",
            "skip_confidence": "HIGH",
            "reasoning": "maintenance-looking update",
            "ops": (
                "1. Open the Dockerfile\n"
                "2. Replace @latest with @2.1.114\n"
            ),
        }
        fake_result = subprocess.CompletedProcess(
            args=["claude"], returncode=0,
            stdout=json.dumps(verdict), stderr="",
        )
        transitions_called = []

        def fake_apply_transition(issue_number, trigger_name, **kwargs):
            transitions_called.append(trigger_name)
            return True, False

        labels_added = []

        def fake_set_labels(issue_number, *, add=(), remove=(), log_prefix="cai"):
            labels_added.extend(add)
            return True

        with mock.patch.object(T, "_run_claude_p", return_value=fake_result), \
             mock.patch.object(T, "fire_trigger", side_effect=fake_apply_transition), \
             mock.patch.object(T, "_set_labels", side_effect=fake_set_labels), \
             mock.patch.object(T, "check_duplicate_or_resolved", return_value=None), \
             mock.patch.object(T, "log_run"):
            rc = T.handle_triage(self._make_issue())

        self.assertEqual(rc, 0)
        self.assertIn("triaging_to_refining", transitions_called)
        self.assertNotIn("triaging_to_applying", transitions_called)
        self.assertIn(LABEL_KIND_CODE, labels_added)
        self.assertNotIn(LABEL_KIND_MAINTENANCE, labels_added)

    def test_apply_with_valid_ops_still_reaches_applying(self):
        """APPLY+maintenance+HIGH with genuine cai-maintain op lines
        must still transition to :applying and apply kind:maintenance."""
        import json
        import subprocess
        from unittest import mock
        import cai_lib.actions.triage as T

        verdict = {
            "routing_decision": "APPLY",
            "routing_confidence": "HIGH",
            "kind": "maintenance",
            "skip_confidence": "HIGH",
            "reasoning": "valid maintenance ops",
            "ops": "1. label add 981 kind:maintenance\n2. close 981\n",
        }
        fake_result = subprocess.CompletedProcess(
            args=["claude"], returncode=0,
            stdout=json.dumps(verdict), stderr="",
        )
        fake_run = subprocess.CompletedProcess(
            args=["gh"], returncode=0, stdout="", stderr="",
        )
        transitions_called = []

        def fake_apply_transition(issue_number, trigger_name, **kwargs):
            transitions_called.append(trigger_name)
            return True, False

        labels_added = []

        def fake_set_labels(issue_number, *, add=(), remove=(), log_prefix="cai"):
            labels_added.extend(add)
            return True

        with mock.patch.object(T, "_run_claude_p", return_value=fake_result), \
             mock.patch.object(T, "_run", return_value=fake_run), \
             mock.patch.object(T, "fire_trigger", side_effect=fake_apply_transition), \
             mock.patch.object(T, "_set_labels", side_effect=fake_set_labels), \
             mock.patch.object(T, "check_duplicate_or_resolved", return_value=None), \
             mock.patch.object(T, "log_run"):
            rc = T.handle_triage(self._make_issue())

        self.assertEqual(rc, 0)
        self.assertIn("triaging_to_applying", transitions_called)
        self.assertNotIn("triaging_to_refining", transitions_called)
        self.assertIn(LABEL_KIND_MAINTENANCE, labels_added)
        self.assertNotIn(LABEL_KIND_CODE, labels_added)


class TestTriagingPrelabeledKindOverride(unittest.TestCase):
    """_prelabeled_kind() + handle_triage() override logic: a
    kind:code / kind:maintenance label already present on the
    issue at triage entry is authoritative and overrides the
    haiku classifier's ``kind`` verdict (issue #991).
    """

    def test_prelabeled_kind_returns_code_when_kind_code_present(self):
        from cai_lib.actions.triage import _prelabeled_kind
        self.assertEqual(
            _prelabeled_kind([LABEL_TRIAGING, LABEL_KIND_CODE]),
            "code",
        )

    def test_prelabeled_kind_returns_maintenance_when_kind_maintenance_present(self):
        from cai_lib.actions.triage import _prelabeled_kind
        self.assertEqual(
            _prelabeled_kind([LABEL_KIND_MAINTENANCE]),
            "maintenance",
        )

    def test_prelabeled_kind_returns_none_when_absent(self):
        from cai_lib.actions.triage import _prelabeled_kind
        self.assertIsNone(_prelabeled_kind([LABEL_TRIAGING]))
        self.assertIsNone(_prelabeled_kind([]))

    def test_prelabel_overrides_agent_maintenance_verdict(self):
        """A pre-applied kind:code label must force handle_triage to
        re-route an APPLY+maintenance+HIGH verdict through REFINE as
        kind:code (the pair_ok check fails once kind is overridden)."""
        import json
        import subprocess
        from unittest import mock
        import cai_lib.actions.triage as T

        issue = {
            "number": 991,
            "title": "Bump claude-code version",
            "body": "Release notes mention a relevant fix.",
            "labels": [
                {"name": LABEL_TRIAGING},
                {"name": LABEL_KIND_CODE},
            ],
        }
        verdict = {
            "routing_decision": "APPLY",
            "routing_confidence": "HIGH",
            "kind": "maintenance",
            "skip_confidence": "HIGH",
            "reasoning": "looks ops-shaped to the classifier",
            "ops": "1. label add 991 kind:maintenance\n2. close 991\n",
        }
        fake_result = subprocess.CompletedProcess(
            args=["claude"], returncode=0,
            stdout=json.dumps(verdict), stderr="",
        )
        transitions_called = []

        def fake_apply_transition(issue_number, trigger_name, **kwargs):
            transitions_called.append(trigger_name)
            return True, False

        labels_added = []

        def fake_set_labels(issue_number, *, add=(), remove=(), log_prefix="cai"):
            labels_added.extend(add)
            return True

        with mock.patch.object(T, "_run_claude_p", return_value=fake_result), \
             mock.patch.object(T, "fire_trigger", side_effect=fake_apply_transition), \
             mock.patch.object(T, "_set_labels", side_effect=fake_set_labels), \
             mock.patch.object(T, "check_duplicate_or_resolved", return_value=None), \
             mock.patch.object(T, "log_run"):
            rc = T.handle_triage(issue)

        self.assertEqual(rc, 0)
        self.assertIn("triaging_to_refining", transitions_called)
        self.assertNotIn("triaging_to_applying", transitions_called)
        self.assertIn(LABEL_KIND_CODE, labels_added)
        self.assertNotIn(LABEL_KIND_MAINTENANCE, labels_added)


class TestFireTrigger(unittest.TestCase):
    """Direct fire_trigger() tests — Machine-based FSM dispatch (#1099)."""

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

    def test_fire_trigger_basic_transition(self):
        """fire_trigger dispatches correctly for a basic non-gated transition."""
        calls, fake_labels = self._recording_set_labels()
        ok, diverted = fire_trigger(
            123, "raise_to_refining",
            is_pr=False,
            current_labels=[LABEL_RAISED],
            set_labels=fake_labels,
        )
        self.assertTrue(ok)
        self.assertFalse(diverted)
        self.assertEqual(len(calls), 1)
        self.assertIn(LABEL_REFINING, calls[0]["add"])
        self.assertIn(LABEL_RAISED, calls[0]["remove"])

    def test_fire_trigger_confidence_gate_diverts_on_low(self):
        """Confidence-gated fire_trigger diverts to HUMAN_NEEDED on low confidence."""
        calls, fake_labels = self._recording_set_labels()
        comments, fake_comment = self._recording_post_comment()
        ok, diverted = fire_trigger(
            456, "refining_to_refined",
            is_pr=False,
            _confidence_gated=True,
            confidence=Confidence.MEDIUM,  # Below HIGH threshold
            current_labels=[LABEL_REFINING],
            set_labels=fake_labels,
            post_comment=fake_comment,
        )
        self.assertTrue(ok)
        self.assertTrue(diverted)
        self.assertEqual(len(calls), 1)
        self.assertIn(LABEL_HUMAN_NEEDED, calls[0]["add"])
        self.assertIn(LABEL_REFINING, calls[0]["remove"])
        self.assertNotIn(LABEL_REFINED, calls[0]["add"])
        self.assertEqual(len(comments), 1)
        self.assertIn("MEDIUM", comments[0]["body"])
        self.assertIn("HIGH", comments[0]["body"])

    def test_fire_trigger_confidence_gate_passes_on_high(self):
        """Confidence-gated fire_trigger applies the primary transition on HIGH."""
        calls, fake_labels = self._recording_set_labels()
        comments, fake_comment = self._recording_post_comment()
        ok, diverted = fire_trigger(
            457, "refining_to_refined",
            is_pr=False,
            _confidence_gated=True,
            confidence=Confidence.HIGH,
            current_labels=[LABEL_REFINING],
            set_labels=fake_labels,
            post_comment=fake_comment,
        )
        self.assertTrue(ok)
        self.assertFalse(diverted)
        self.assertEqual(len(calls), 1)
        self.assertIn(LABEL_REFINED, calls[0]["add"])
        self.assertNotIn(LABEL_HUMAN_NEEDED, calls[0]["add"])
        self.assertEqual(comments, [])

    def test_fire_trigger_invalid_source_state(self):
        """fire_trigger returns (False, False) on state mismatch."""
        calls, fake_labels = self._recording_set_labels()
        ok, diverted = fire_trigger(
            789, "raise_to_refining",  # Expects source=RAISED
            is_pr=False,
            current_labels=[LABEL_HUMAN_NEEDED],  # Wrong state
            set_labels=fake_labels,
        )
        self.assertFalse(ok)
        self.assertFalse(diverted)
        self.assertEqual(calls, [])

    def test_fire_trigger_unknown_trigger_raises_key_error(self):
        """fire_trigger raises KeyError for unrecognised trigger names."""
        with self.assertRaises(KeyError):
            fire_trigger(1, "not_a_real_transition", current_labels=[LABEL_RAISED])


if __name__ == "__main__":
    unittest.main()
