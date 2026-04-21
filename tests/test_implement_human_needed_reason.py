"""Regression tests for the :human-needed parking path in cai_lib.actions.implement.

Issue #1083: four implement-side escalation paths (early-abort guard,
pre-screen spike verdict, subagent-no-change spike marker, repeated
test failures on a non-MEDIUM plan) called ``_set_labels(add=[LABEL_HUMAN_NEEDED])``
directly with a hand-rolled comment that carried only the marker
header and skipped the structured ``Automation paused``, ``Required
confidence:``, and ``Reported confidence:`` lines that
``_fetch_human_needed_issues`` in ``cmd_agents.py`` regex-matches.
That left #1044 (opus-retry post-rollback park) invisible to the
audit parser's ``human_needed_reason_missing`` finder.

These tests pin the refactored behaviour:

- ``in_progress_to_human_needed`` transition exists and is caller-gated.
- ``_park_in_progress_at_human_needed`` funnels through
  ``apply_transition`` so the PR #1072 invariant fires and the MARKER
  comment is auto-posted with the structured fields.
- ``_render_human_divert_reason`` is safe when ``transition.min_confidence``
  is ``None`` (previously crashed with ``AttributeError``).
"""
import os
import re
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cai_lib.actions import implement as impl_mod  # noqa: E402
from cai_lib.config import (  # noqa: E402
    LABEL_HUMAN_NEEDED, LABEL_IN_PROGRESS, LABEL_PLAN_APPROVED,
)
from cai_lib.fsm import (  # noqa: E402
    Confidence, ISSUE_TRANSITIONS, IssueState, Transition, find_transition,
)
from cai_lib.fsm_transitions import _render_human_divert_reason  # noqa: E402


class TestInProgressToHumanNeededTransition(unittest.TestCase):
    """The new FSM transition for implement-side parks (#1083)."""

    def test_transition_registered(self):
        t = find_transition("in_progress_to_human_needed")
        self.assertEqual(t.from_state, IssueState.IN_PROGRESS)
        self.assertEqual(t.to_state, IssueState.HUMAN_NEEDED)
        self.assertIn(t, ISSUE_TRANSITIONS)

    def test_transition_is_caller_gated(self):
        """No FSM-level confidence threshold — the handler decides."""
        t = find_transition("in_progress_to_human_needed")
        self.assertIsNone(t.min_confidence)

    def test_transition_label_deltas(self):
        t = find_transition("in_progress_to_human_needed")
        self.assertIn(LABEL_IN_PROGRESS, t.labels_remove)
        self.assertIn(LABEL_HUMAN_NEEDED, t.labels_add)


class TestRenderHumanDivertReasonNoneThreshold(unittest.TestCase):
    """``_render_human_divert_reason`` must not crash when min_confidence is None."""

    def test_caller_gated_transition_renders_placeholder(self):
        t = Transition(
            "fake_caller_gated",
            IssueState.IN_PROGRESS, IssueState.HUMAN_NEEDED,
            labels_remove=[LABEL_IN_PROGRESS],
            labels_add=[LABEL_HUMAN_NEEDED],
            min_confidence=None,
        )
        body = _render_human_divert_reason(
            transition_name="fake_caller_gated",
            transition=t,
            confidence=None,
            extra="reason text",
        )
        self.assertIn("Required confidence: `caller-gated`", body)
        self.assertIn("Reported confidence: `MISSING`", body)
        self.assertIn("reason text", body)


class TestParkInProgressAtHumanNeeded(unittest.TestCase):
    """``_park_in_progress_at_human_needed`` funnels through apply_transition."""

    # Regex mirrors ``_fetch_human_needed_issues`` in cai_lib/cmd_agents.py:
    # those three patterns are what the audit agent uses to report
    # ``human_needed_reason_missing``; if any stops matching, the audit
    # agent will flag the divert as silent again.
    _TRANSITION_RE = re.compile(r"Automation paused `([^`]+)`")
    _REQUIRED_RE = re.compile(r"Required confidence:\s*`([^`]+)`")
    _REPORTED_RE = re.compile(r"Reported confidence:\s*`([^`]+)`")

    def _install_fakes(self, *, first_set_labels_ok=True, second_set_labels_ok=True):
        calls = {"set_labels": [], "post_comment": []}

        def _fake_set_labels(issue_number, *, add=(), remove=(), log_prefix="cai"):
            calls["set_labels"].append({
                "issue": issue_number,
                "add": list(add),
                "remove": list(remove),
            })
            if len(calls["set_labels"]) == 1:
                return first_set_labels_ok
            return second_set_labels_ok

        def _fake_post_comment(issue_number, body, *, log_prefix="cai"):
            calls["post_comment"].append({"issue": issue_number, "body": body})
            return True

        p1 = mock.patch("cai_lib.github._set_labels", _fake_set_labels)
        p2 = mock.patch("cai_lib.github._post_issue_comment", _fake_post_comment)
        p1.start()
        p2.start()
        self.addCleanup(p1.stop)
        self.addCleanup(p2.stop)
        return calls

    def test_first_attempt_success_posts_structured_comment(self):
        calls = self._install_fakes()
        ok = impl_mod._park_in_progress_at_human_needed(
            1044, reason="## Test escalation\n\nBody text.",
        )
        self.assertTrue(ok)
        # Only one _set_labels call on the success path.
        self.assertEqual(len(calls["set_labels"]), 1)
        first = calls["set_labels"][0]
        self.assertEqual(first["issue"], 1044)
        self.assertIn(LABEL_HUMAN_NEEDED, first["add"])
        self.assertIn(LABEL_IN_PROGRESS, first["remove"])
        # Exactly one MARKER comment, carrying the three structured fields.
        self.assertEqual(len(calls["post_comment"]), 1)
        body = calls["post_comment"][0]["body"]
        self.assertIn("🙋 Human attention needed", body)
        m = self._TRANSITION_RE.search(body)
        self.assertIsNotNone(m, "missing `Automation paused \\`...\\`` line")
        self.assertEqual(m.group(1), "in_progress_to_human_needed")
        m = self._REQUIRED_RE.search(body)
        self.assertIsNotNone(m, "missing `Required confidence` line")
        self.assertEqual(m.group(1), "caller-gated")
        m = self._REPORTED_RE.search(body)
        self.assertIsNotNone(m, "missing `Reported confidence` line")
        self.assertEqual(m.group(1), "MISSING")
        # The reason text is appended verbatim after the structured fields.
        self.assertIn("## Test escalation", body)
        self.assertIn("Body text.", body)

    def test_extra_remove_is_appended_to_labels_remove(self):
        """The subagent-no-change spike path must also strip LABEL_PLAN_APPROVED."""
        calls = self._install_fakes()
        ok = impl_mod._park_in_progress_at_human_needed(
            1044,
            reason="## Needs spike\n\nresearch required",
            extra_remove=(LABEL_PLAN_APPROVED,),
        )
        self.assertTrue(ok)
        first = calls["set_labels"][0]
        self.assertIn(LABEL_IN_PROGRESS, first["remove"])
        self.assertIn(LABEL_PLAN_APPROVED, first["remove"])

    def test_double_retry_on_transient_label_failure(self):
        calls = self._install_fakes(
            first_set_labels_ok=False, second_set_labels_ok=True,
        )
        ok = impl_mod._park_in_progress_at_human_needed(
            1044, reason="## Retry\n\nretry body",
        )
        self.assertTrue(ok)
        self.assertEqual(len(calls["set_labels"]), 2)
        # First attempt's _set_labels failed, so apply_transition never
        # posted on that attempt. The second attempt succeeded and
        # posted exactly one comment.
        self.assertEqual(len(calls["post_comment"]), 1)

    def test_returns_false_when_both_attempts_fail(self):
        calls = self._install_fakes(
            first_set_labels_ok=False, second_set_labels_ok=False,
        )
        ok = impl_mod._park_in_progress_at_human_needed(
            1044, reason="## Always fails\n\nbody",
        )
        self.assertFalse(ok)
        self.assertEqual(len(calls["set_labels"]), 2)
        self.assertEqual(calls["post_comment"], [])

    def test_empty_reason_refused_by_apply_transition_invariant(self):
        """Empty divert_reason must be refused (PR #1072 invariant)."""
        calls = self._install_fakes()
        ok = impl_mod._park_in_progress_at_human_needed(1044, reason="")
        self.assertFalse(ok)
        self.assertEqual(calls["set_labels"], [])
        self.assertEqual(calls["post_comment"], [])


if __name__ == "__main__":
    unittest.main()
