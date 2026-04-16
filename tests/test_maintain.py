"""Tests for handle_maintain — the dispatcher handler for :applying issues."""
import sys
import os
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cai_lib.config import (
    LABEL_APPLYING,
    LABEL_APPLIED,
    LABEL_HUMAN_NEEDED,
    REPO,
)


def _make_applying_issue(number=501):
    """Minimal fake issue in the :applying state."""
    return {
        "number": number,
        "title": f"Test maintenance issue {number}",
        "body": "Ops:\n- label add 100 some-label\n",
        "labels": [{"name": LABEL_APPLYING}],
        "createdAt": "2026-01-01T00:00:00Z",
    }


def _make_clone_result(rc=0):
    r = MagicMock()
    r.returncode = rc
    r.stderr = ""
    r.stdout = ""
    return r


def _make_agent_result(confidence="HIGH", rc=0, reason=None):
    r = MagicMock()
    reason_line = ""
    if confidence in ("MEDIUM", "LOW"):
        default_reason = "Some operations could not be verified." if reason is None else reason
        reason_line = f"Confidence reason: {default_reason}\n"
    r.returncode = rc
    r.stdout = f"## Maintenance Summary\n\nConfidence: {confidence}\n{reason_line}"
    r.stderr = ""
    return r


class TestHandleMaintainHappyPath(unittest.TestCase):
    """HIGH confidence → :applied transition fires."""

    def test_handle_maintain_happy_path(self):
        issue = _make_applying_issue()
        label_calls = []

        def fake_set_labels(issue_number, *, add=(), remove=(), log_prefix="cai"):
            label_calls.append({"issue_number": issue_number,
                                 "add": list(add), "remove": list(remove)})
            return True

        with patch("cai_lib.actions.maintain._run", return_value=_make_clone_result()), \
             patch("cai_lib.actions.maintain._run_claude_p",
                   return_value=_make_agent_result("HIGH")), \
             patch("cai_lib.actions.maintain.shutil.rmtree"), \
             patch("cai_lib.actions.maintain.log_run"), \
             patch("cai_lib.github._set_labels", side_effect=fake_set_labels), \
             patch("cai_lib.github._post_issue_comment", return_value=True):
            from cai_lib.actions.maintain import handle_maintain
            rc = handle_maintain(issue)

        self.assertEqual(rc, 0)
        applied_calls = [c for c in label_calls
                         if LABEL_APPLIED in c["add"] and LABEL_APPLYING in c["remove"]]
        self.assertTrue(
            applied_calls,
            f"Expected _set_labels(add=[LABEL_APPLIED], remove=[LABEL_APPLYING]); "
            f"got calls: {label_calls}"
        )


class TestHandleMaintainLowConfidence(unittest.TestCase):
    """LOW confidence → :human-needed diversion."""

    def test_low_confidence_diverts_to_human(self):
        issue = _make_applying_issue()
        label_calls = []

        def fake_set_labels(issue_number, *, add=(), remove=(), log_prefix="cai"):
            label_calls.append({"issue_number": issue_number,
                                 "add": list(add), "remove": list(remove)})
            return True

        with patch("cai_lib.actions.maintain._run", return_value=_make_clone_result()), \
             patch("cai_lib.actions.maintain._run_claude_p",
                   return_value=_make_agent_result("LOW")), \
             patch("cai_lib.actions.maintain.shutil.rmtree"), \
             patch("cai_lib.actions.maintain.log_run"), \
             patch("cai_lib.github._set_labels", side_effect=fake_set_labels), \
             patch("cai_lib.github._post_issue_comment", return_value=True):
            from cai_lib.actions.maintain import handle_maintain
            rc = handle_maintain(issue)

        self.assertEqual(rc, 0)
        human_calls = [c for c in label_calls
                       if LABEL_HUMAN_NEEDED in c["add"] and LABEL_APPLYING in c["remove"]]
        self.assertTrue(
            human_calls,
            f"Expected divert to HUMAN_NEEDED; got calls: {label_calls}"
        )

    def test_medium_confidence_diverts_to_human(self):
        issue = _make_applying_issue()
        label_calls = []

        def fake_set_labels(issue_number, *, add=(), remove=(), log_prefix="cai"):
            label_calls.append({"issue_number": issue_number,
                                 "add": list(add), "remove": list(remove)})
            return True

        with patch("cai_lib.actions.maintain._run", return_value=_make_clone_result()), \
             patch("cai_lib.actions.maintain._run_claude_p",
                   return_value=_make_agent_result("MEDIUM")), \
             patch("cai_lib.actions.maintain.shutil.rmtree"), \
             patch("cai_lib.actions.maintain.log_run"), \
             patch("cai_lib.github._set_labels", side_effect=fake_set_labels), \
             patch("cai_lib.github._post_issue_comment", return_value=True):
            from cai_lib.actions.maintain import handle_maintain
            rc = handle_maintain(issue)

        self.assertEqual(rc, 0)
        human_calls = [c for c in label_calls
                       if LABEL_HUMAN_NEEDED in c["add"] and LABEL_APPLYING in c["remove"]]
        self.assertTrue(
            human_calls,
            f"Expected divert to HUMAN_NEEDED; got calls: {label_calls}"
        )


if __name__ == "__main__":
    unittest.main()
