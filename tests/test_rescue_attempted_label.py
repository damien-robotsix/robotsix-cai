"""Tests for the ``LABEL_RESCUE_ATTEMPTED`` skip-marker plumbing.

Covers:
  - ``_list_unresolved_*`` skip targets carrying the label.
  - ``cmd_rescue`` main loop applies the label after non-resuming
    verdicts and leaves the label off after resumed / agent_failed.
  - Every ``human_to_*`` and ``pr_human_to_*`` transition strips the
    label so a future park can be re-evaluated.
"""
from __future__ import annotations

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cai_lib import cmd_rescue as R  # noqa: E402
from cai_lib.config import (  # noqa: E402
    LABEL_HUMAN_NEEDED,
    LABEL_PR_HUMAN_NEEDED,
    LABEL_RESCUE_ATTEMPTED,
)
from cai_lib.fsm_transitions import (  # noqa: E402
    ISSUE_TRANSITIONS,
    PR_TRANSITIONS,
)


class TestListersSkipRescueAttempted(unittest.TestCase):

    def test_issue_lister_skips_label(self):
        attempted = {
            "number": 1, "title": "t", "body": "",
            "labels": [
                {"name": LABEL_HUMAN_NEEDED},
                {"name": LABEL_RESCUE_ATTEMPTED},
            ],
            "comments": [],
        }
        fresh = {
            "number": 2, "title": "t", "body": "",
            "labels": [{"name": LABEL_HUMAN_NEEDED}],
            "comments": [],
        }
        with mock.patch.object(R, "_gh_json", return_value=[attempted, fresh]):
            out = R._list_unresolved_human_needed_issues()
        self.assertEqual([i["number"] for i in out], [2])

    def test_pr_lister_skips_label(self):
        attempted = {
            "number": 11, "title": "t", "body": "",
            "labels": [
                {"name": LABEL_PR_HUMAN_NEEDED},
                {"name": LABEL_RESCUE_ATTEMPTED},
            ],
            "comments": [],
        }
        fresh = {
            "number": 12, "title": "t", "body": "",
            "labels": [{"name": LABEL_PR_HUMAN_NEEDED}],
            "comments": [],
        }
        with mock.patch.object(R, "_gh_json", return_value=[attempted, fresh]):
            out = R._list_unresolved_pr_human_needed_prs()
        self.assertEqual([p["number"] for p in out], [12])


class TestCmdRescueAppliesLabelAfterNonResumingVerdicts(unittest.TestCase):
    """The dispatcher loop in ``cmd_rescue`` is responsible for stamping
    ``LABEL_RESCUE_ATTEMPTED`` on any target the per-issue / per-PR
    helper did not actually resume. Resumed targets must not get the
    label (their FSM transition would clear it anyway, but stamping
    still wastes an API call). Transient ``agent_failed`` results must
    also stay un-labelled so the next tick retries.
    """

    def _run_with_tags(self, issue_tags, pr_tags):
        issues = [
            {"number": 100 + i, "title": "t", "body": "",
             "labels": [{"name": LABEL_HUMAN_NEEDED}], "comments": []}
            for i, _ in enumerate(issue_tags)
        ]
        prs = [
            {"number": 200 + i, "title": "t", "body": "",
             "labels": [{"name": LABEL_PR_HUMAN_NEEDED}], "comments": []}
            for i, _ in enumerate(pr_tags)
        ]
        marked: list[tuple[int, bool]] = []

        def _fake_mark(target_number, *, is_pr):
            marked.append((target_number, is_pr))

        i_iter = iter(issue_tags)
        p_iter = iter(pr_tags)
        with mock.patch.object(
            R, "_list_unresolved_human_needed_issues", return_value=issues,
        ), mock.patch.object(
            R, "_list_unresolved_pr_human_needed_prs", return_value=prs,
        ), mock.patch.object(
            R, "_try_rescue_issue", side_effect=lambda *a, **k: next(i_iter),
        ), mock.patch.object(
            R, "_try_rescue_pr", side_effect=lambda *a, **k: next(p_iter),
        ), mock.patch.object(
            R, "_publish_prevention_findings",
        ), mock.patch.object(
            R, "_mark_rescue_attempted", side_effect=_fake_mark,
        ):
            R.cmd_rescue(args=None)
        return marked

    def test_marks_truly_human_needed_and_low_confidence(self):
        marked = self._run_with_tags(
            issue_tags=["truly_human_needed", "low_confidence", "no_target",
                        "opus_already_attempted", "opus_no_plan"],
            pr_tags=["truly_human_needed", "low_confidence", "no_target"],
        )
        marked_issues = sorted(n for n, is_pr in marked if not is_pr)
        marked_prs = sorted(n for n, is_pr in marked if is_pr)
        self.assertEqual(marked_issues, [100, 101, 102, 103, 104])
        self.assertEqual(marked_prs, [200, 201, 202])

    def test_does_not_mark_resumed_or_agent_failed(self):
        marked = self._run_with_tags(
            issue_tags=["resumed", "agent_failed", "opus_attempt_scheduled"],
            pr_tags=["resumed", "agent_failed"],
        )
        self.assertEqual(marked, [])


class TestMarkRescueAttemptedHelper(unittest.TestCase):
    """The helper routes issues through ``_set_labels`` and PRs through
    ``_set_pr_labels`` (PRs share the issue-number namespace but the
    PR-specific helper exists for symmetry)."""

    def test_issue_path_uses_set_labels(self):
        with mock.patch.object(R, "_set_labels", return_value=True) as si, \
             mock.patch.object(R, "_set_pr_labels") as sp:
            R._mark_rescue_attempted(42, is_pr=False)
        si.assert_called_once()
        self.assertEqual(si.call_args.kwargs["add"], [LABEL_RESCUE_ATTEMPTED])
        sp.assert_not_called()

    def test_pr_path_uses_set_pr_labels(self):
        with mock.patch.object(R, "_set_pr_labels", return_value=True) as sp, \
             mock.patch.object(R, "_set_labels") as si:
            R._mark_rescue_attempted(77, is_pr=True)
        sp.assert_called_once()
        self.assertEqual(sp.call_args.kwargs["add"], [LABEL_RESCUE_ATTEMPTED])
        si.assert_not_called()

    def test_failure_does_not_raise(self):
        with mock.patch.object(R, "_set_labels", return_value=False):
            R._mark_rescue_attempted(42, is_pr=False)  # must not raise


class TestResumeTransitionsStripLabel(unittest.TestCase):
    """Structural pin: every ``human_to_*`` issue transition and every
    ``pr_human_to_*`` PR transition must declare
    ``LABEL_RESCUE_ATTEMPTED`` in ``labels_remove``. Without this, a
    target that exits HUMAN_NEEDED (e.g. via admin ``human:solved``)
    would carry the marker into its next park and never get re-rescued.
    """

    def test_all_human_to_transitions_strip_label(self):
        for t in ISSUE_TRANSITIONS:
            if t.name.startswith("human_to_"):
                self.assertIn(
                    LABEL_RESCUE_ATTEMPTED, t.labels_remove,
                    f"{t.name} must strip {LABEL_RESCUE_ATTEMPTED}",
                )

    def test_all_pr_human_to_transitions_strip_label(self):
        for t in PR_TRANSITIONS:
            if t.name.startswith("pr_human_to_"):
                self.assertIn(
                    LABEL_RESCUE_ATTEMPTED, t.labels_remove,
                    f"{t.name} must strip {LABEL_RESCUE_ATTEMPTED}",
                )


if __name__ == "__main__":
    unittest.main()
