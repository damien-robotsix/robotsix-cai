"""Tests for the Opus-escalation path in :mod:`cai_lib.cmd_rescue`.

Covers the deterministic pieces added alongside the
``ATTEMPT_OPUS_IMPLEMENT`` verdict: the schema, the
``_issue_has_opus_attempted`` helper, the ``_schedule_opus_attempt``
driver, and the end-to-end plumbing in ``_try_rescue_issue`` (via a
mocked ``claude -p`` call).

The heavy live-container path (invoking the Sonnet ``cai-rescue``
subagent and the Opus ``cai-implement`` subagent) is deliberately out
of scope — these tests only guard the glue.
"""
import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cai_lib import cmd_rescue as R  # noqa: E402
from cai_lib.actions import implement as impl_mod  # noqa: E402
from cai_lib.config import LABEL_OPUS_ATTEMPTED  # noqa: E402
from cai_lib.fsm import ISSUE_TRANSITIONS  # noqa: E402


_PLAN_BLOCK = (
    "<!-- cai-plan-start -->\n"
    "## Selected Implementation Plan\n\n"
    "Do the thing.\n"
    "<!-- cai-plan-end -->\n"
)


class TestRescueJsonSchema(unittest.TestCase):
    """The JSON schema must advertise the new verdict."""

    def test_attempt_opus_implement_in_verdict_enum(self):
        enum = R._RESCUE_JSON_SCHEMA["properties"]["verdict"]["enum"]
        self.assertIn("ATTEMPT_OPUS_IMPLEMENT", enum)
        # Existing verdicts must still be accepted.
        self.assertIn("AUTONOMOUSLY_RESOLVABLE", enum)
        self.assertIn("TRULY_HUMAN_NEEDED", enum)

    def test_resume_to_enum_covers_issue_and_pr_states(self):
        enum = R._RESCUE_JSON_SCHEMA["properties"]["resume_to"]["enum"]
        # Issue-side targets.
        for state in ("RAISED", "REFINING", "NEEDS_EXPLORATION",
                      "PLAN_APPROVED", "SOLVED"):
            self.assertIn(state, enum)
        # PR-side targets.
        for state in ("REVIEWING_CODE", "REVIEWING_DOCS",
                      "REVISION_PENDING", "APPROVED"):
            self.assertIn(state, enum)


class TestIssueHasOpusAttempted(unittest.TestCase):

    def test_detects_label_on_issue(self):
        issue = {"labels": [{"name": "auto-improve:human-needed"},
                            {"name": LABEL_OPUS_ATTEMPTED}]}
        self.assertTrue(R._issue_has_opus_attempted(issue))

    def test_missing_label(self):
        issue = {"labels": [{"name": "auto-improve:human-needed"}]}
        self.assertFalse(R._issue_has_opus_attempted(issue))

    def test_accepts_string_label_shape(self):
        # Some gh JSON shapes return raw strings rather than dicts.
        issue = {"labels": [LABEL_OPUS_ATTEMPTED]}
        self.assertTrue(R._issue_has_opus_attempted(issue))

    def test_empty_labels(self):
        self.assertFalse(R._issue_has_opus_attempted({"labels": []}))
        self.assertFalse(R._issue_has_opus_attempted({}))


class TestScheduleOpusAttempt(unittest.TestCase):

    def _issue(self, *, labels=None, body=_PLAN_BLOCK):
        return {
            "number": 42,
            "title": "widget broke",
            "body": body,
            "labels": labels or [{"name": "auto-improve:human-needed"}],
            "comments": [],
        }

    def test_refuses_second_escalation(self):
        issue = self._issue(labels=[
            {"name": "auto-improve:human-needed"},
            {"name": LABEL_OPUS_ATTEMPTED},
        ])
        with mock.patch.object(R, "_set_labels") as set_labels, \
             mock.patch.object(R, "fire_trigger") as apply_t, \
             mock.patch.object(R, "_post_opus_escalation_comment") as cmt:
            tag = R._schedule_opus_attempt(issue, reasoning="r")
        self.assertEqual(tag, "opus_already_attempted")
        set_labels.assert_not_called()
        apply_t.assert_not_called()
        cmt.assert_not_called()

    def test_refuses_when_no_stored_plan(self):
        issue = self._issue(body="no plan block here")
        with mock.patch.object(R, "_set_labels") as set_labels, \
             mock.patch.object(R, "fire_trigger") as apply_t, \
             mock.patch.object(R, "_post_opus_escalation_comment") as cmt:
            tag = R._schedule_opus_attempt(issue, reasoning="r")
        self.assertEqual(tag, "opus_no_plan")
        set_labels.assert_not_called()
        apply_t.assert_not_called()
        cmt.assert_not_called()

    def test_happy_path_stamps_label_and_fires_transition(self):
        issue = self._issue()
        with mock.patch.object(R, "_set_labels", return_value=True) as set_labels, \
             mock.patch.object(R, "fire_trigger", return_value=(True, False)) as apply_t, \
             mock.patch.object(R, "_post_opus_escalation_comment", return_value=True) as cmt:
            tag = R._schedule_opus_attempt(issue, reasoning="plan looks sound")
        self.assertEqual(tag, "opus_attempt_scheduled")
        # Comment is posted BEFORE labels/transition so the audit trail
        # survives a partial failure.
        cmt.assert_called_once()
        self.assertEqual(cmt.call_args.kwargs["reasoning"], "plan looks sound")
        # Label stamp.
        set_labels.assert_called_once()
        self.assertEqual(
            set_labels.call_args.kwargs["add"], [LABEL_OPUS_ATTEMPTED]
        )
        # FSM transition.
        apply_t.assert_called_once()
        self.assertEqual(apply_t.call_args.args[1], "human_to_plan_approved")

    def test_propagates_label_apply_failure(self):
        issue = self._issue()
        with mock.patch.object(R, "_set_labels", return_value=False), \
             mock.patch.object(R, "fire_trigger") as apply_t, \
             mock.patch.object(R, "_post_opus_escalation_comment", return_value=True):
            tag = R._schedule_opus_attempt(issue, reasoning="r")
        self.assertEqual(tag, "agent_failed")
        # FSM transition must NOT fire when the label stamp failed.
        apply_t.assert_not_called()


class TestTryRescueIssueDispatchesOpusBranch(unittest.TestCase):
    """End-to-end: verdict=ATTEMPT_OPUS_IMPLEMENT routes to the Opus scheduler."""

    def _claude_reply(self, payload):
        proc = mock.Mock()
        proc.returncode = 0
        proc.stdout = json.dumps(payload)
        proc.stderr = ""
        return proc

    def test_high_confidence_opus_verdict_schedules_escalation(self):
        issue = {
            "number": 7,
            "title": "t",
            "body": _PLAN_BLOCK,
            "labels": [{"name": "auto-improve:human-needed"}],
            "comments": [],
        }
        claude_payload = {
            "verdict": "ATTEMPT_OPUS_IMPLEMENT",
            "confidence": "HIGH",
            "reasoning": "plan concrete; sonnet hit repeated test failures",
        }
        with mock.patch.object(
            R, "_run_claude_p", return_value=self._claude_reply(claude_payload)
        ), mock.patch.object(
            R, "_schedule_opus_attempt", return_value="opus_attempt_scheduled"
        ) as sched:
            tag = R._try_rescue_issue(issue, prevention_findings=[])
        self.assertEqual(tag, "opus_attempt_scheduled")
        sched.assert_called_once()
        # Reasoning is piped through to the comment-writer.
        self.assertEqual(
            sched.call_args.kwargs["reasoning"],
            "plan concrete; sonnet hit repeated test failures",
        )

    def test_low_confidence_opus_verdict_parks(self):
        issue = {
            "number": 8,
            "title": "t",
            "body": _PLAN_BLOCK,
            "labels": [{"name": "auto-improve:human-needed"}],
            "comments": [],
        }
        claude_payload = {
            "verdict": "ATTEMPT_OPUS_IMPLEMENT",
            "confidence": "MEDIUM",  # below HIGH — must not act.
            "reasoning": "not sure",
        }
        with mock.patch.object(
            R, "_run_claude_p", return_value=self._claude_reply(claude_payload)
        ), mock.patch.object(R, "_schedule_opus_attempt") as sched:
            tag = R._try_rescue_issue(issue, prevention_findings=[])
        self.assertEqual(tag, "low_confidence")
        sched.assert_not_called()


class TestTryRescuePr(unittest.TestCase):
    """End-to-end: PR-side rescue wiring routes through apply_pr_transition."""

    def _claude_reply(self, payload):
        proc = mock.Mock()
        proc.returncode = 0
        proc.stdout = json.dumps(payload)
        proc.stderr = ""
        return proc

    def _pr(self):
        return {
            "number": 77,
            "title": "fix widget",
            "body": "…",
            "labels": [{"name": "auto-improve:pr-human-needed"}],
            "comments": [],
        }

    def test_high_confidence_resume_fires_pr_transition(self):
        pr = self._pr()
        claude_payload = {
            "verdict": "AUTONOMOUSLY_RESOLVABLE",
            "confidence": "HIGH",
            "resume_to": "REVIEWING_CODE",
            "reasoning": "reviewer diverted on a transient; re-run review",
        }
        with mock.patch.object(
            R, "_run_claude_p", return_value=self._claude_reply(claude_payload)
        ), mock.patch.object(
            R, "fire_trigger", return_value=(True, False)
        ) as apply_pr, mock.patch.object(
            R, "_post_pr_rescue_comment", return_value=True
        ) as cmt:
            tag = R._try_rescue_pr(pr, prevention_findings=[])
        self.assertEqual(tag, "resumed")
        apply_pr.assert_called_once()
        self.assertEqual(apply_pr.call_args.args[1], "pr_human_to_reviewing_code")
        cmt.assert_called_once()
        self.assertEqual(cmt.call_args.kwargs["target"], "REVIEWING_CODE")

    def test_opus_verdict_on_pr_parks_target(self):
        pr = self._pr()
        claude_payload = {
            "verdict": "ATTEMPT_OPUS_IMPLEMENT",
            "confidence": "HIGH",
            "reasoning": "mis-targeted",
        }
        with mock.patch.object(
            R, "_run_claude_p", return_value=self._claude_reply(claude_payload)
        ), mock.patch.object(R, "fire_trigger") as apply_pr, \
             mock.patch.object(R, "_schedule_opus_attempt") as sched:
            tag = R._try_rescue_pr(pr, prevention_findings=[])
        self.assertEqual(tag, "truly_human_needed")
        apply_pr.assert_not_called()
        sched.assert_not_called()

    def test_low_confidence_resume_parks_pr(self):
        pr = self._pr()
        claude_payload = {
            "verdict": "AUTONOMOUSLY_RESOLVABLE",
            "confidence": "MEDIUM",
            "resume_to": "REVIEWING_CODE",
            "reasoning": "not sure",
        }
        with mock.patch.object(
            R, "_run_claude_p", return_value=self._claude_reply(claude_payload)
        ), mock.patch.object(R, "fire_trigger") as apply_pr:
            tag = R._try_rescue_pr(pr, prevention_findings=[])
        self.assertEqual(tag, "low_confidence")
        apply_pr.assert_not_called()


class TestPrHumanNeededLister(unittest.TestCase):
    """`_list_unresolved_pr_human_needed_prs` must filter out human:solved."""

    def test_filters_out_pr_with_human_solved(self):
        resolved = {
            "number": 1, "title": "t", "body": "",
            "labels": [
                {"name": "auto-improve:pr-human-needed"},
                {"name": "human:solved"},
            ],
            "comments": [],
        }
        unresolved = {
            "number": 2, "title": "t", "body": "",
            "labels": [{"name": "auto-improve:pr-human-needed"}],
            "comments": [],
        }
        with mock.patch.object(R, "_gh_json", return_value=[resolved, unresolved]):
            out = R._list_unresolved_pr_human_needed_prs()
        self.assertEqual([p["number"] for p in out], [2])


class TestInProgressHumanNeededPreservesOpusAttempted(unittest.TestCase):
    """Regression (#1146): the ``in_progress_to_human_needed`` park must
    never strip ``LABEL_OPUS_ATTEMPTED``.

    ``cai rescue``'s one-shot escalation guard
    (``_issue_has_opus_attempted`` in ``cai_lib/cmd_rescue.py``) relies
    on that label surviving every re-park so subsequent rescue passes
    detect the burned one-shot and refuse a second escalation. If a
    future refactor adds ``LABEL_OPUS_ATTEMPTED`` to ``labels_remove``
    on the transition or to a caller's ``extra_remove`` tuple, the
    guard silently becomes a no-op and Opus would be re-invoked on
    every park — these tests fail fast on that class of regression.
    """

    def _install_fakes(self):
        calls = {"set_labels": []}

        def _fake_set_labels(issue_number, *, add=(), remove=(), log_prefix="cai"):
            calls["set_labels"].append({
                "issue": issue_number,
                "add": list(add),
                "remove": list(remove),
            })
            return True

        def _fake_post_comment(issue_number, body, *, log_prefix="cai"):
            return True

        p1 = mock.patch("cai_lib.github._set_labels", _fake_set_labels)
        p2 = mock.patch("cai_lib.github._post_issue_comment", _fake_post_comment)
        p1.start()
        p2.start()
        self.addCleanup(p1.stop)
        self.addCleanup(p2.stop)
        return calls

    def test_transition_labels_remove_excludes_opus_attempted(self):
        """Structural assertion on the transition definition itself."""
        t = next(tt for tt in ISSUE_TRANSITIONS if tt.name == "in_progress_to_human_needed")
        self.assertNotIn(LABEL_OPUS_ATTEMPTED, t.labels_remove)

    def test_park_does_not_strip_opus_attempted(self):
        """End-to-end through ``_park_in_progress_at_human_needed`` →
        ``fire_trigger`` → the patched ``_set_labels`` shim."""
        calls = self._install_fakes()
        ok = impl_mod._park_in_progress_at_human_needed(
            1141, reason="## Test park\n\nregression #1146",
        )
        self.assertTrue(ok)
        self.assertEqual(len(calls["set_labels"]), 1)
        self.assertNotIn(
            LABEL_OPUS_ATTEMPTED, calls["set_labels"][0]["remove"]
        )

    def test_spike_extra_remove_still_preserves_opus_attempted(self):
        """The subagent-no-change spike path in ``cai_lib/actions/implement.py``
        passes ``extra_remove=(LABEL_PLAN_APPROVED,)`` (~line 1389); that
        code path must still NOT strip ``LABEL_OPUS_ATTEMPTED``."""
        from cai_lib.config import LABEL_PLAN_APPROVED
        calls = self._install_fakes()
        ok = impl_mod._park_in_progress_at_human_needed(
            1141,
            reason="## Spike\n\nneeds research",
            extra_remove=(LABEL_PLAN_APPROVED,),
        )
        self.assertTrue(ok)
        self.assertEqual(len(calls["set_labels"]), 1)
        remove = calls["set_labels"][0]["remove"]
        # Plan-approved is intentionally stripped on the spike path.
        self.assertIn(LABEL_PLAN_APPROVED, remove)
        # Opus-attempted must NOT be stripped — that is the invariant
        # this test class exists to pin.
        self.assertNotIn(LABEL_OPUS_ATTEMPTED, remove)


if __name__ == "__main__":
    unittest.main()
