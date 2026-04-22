"""Regression tests for issue #1055 — LOW-confidence merge verdicts
that cite concrete code bugs are routed to REVISION_PENDING via
``approved_to_revision_pending`` instead of parking at
PR_HUMAN_NEEDED via ``approved_to_human``.

The companion helper ``_verdict_cites_concrete_code_bug`` must fire
only on mechanically-fixable reasoning (AttributeError, NameError,
wrong field/method name, typo, undefined name, ...). Design/scope
concerns must keep the old ``approved_to_human`` routing.
"""
import json
import os
import sys
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cai_lib.actions import merge as merge_mod
from cai_lib.actions.merge import _verdict_cites_concrete_code_bug
from cai_lib.dispatcher import HandlerResult
from cai_lib.fsm_transitions import PR_TRANSITIONS


class TestVerdictCitesConcreteCodeBug(unittest.TestCase):
    """The detector fires only on concrete, mechanically-fixable bugs."""

    def test_empty_reasoning_is_false(self):
        self.assertFalse(_verdict_cites_concrete_code_bug(""))
        self.assertFalse(_verdict_cites_concrete_code_bug(None))  # type: ignore[arg-type]

    def test_attribute_error_matches(self):
        self.assertTrue(_verdict_cites_concrete_code_bug(
            "runner.py:38 accesses entry.file_globs, but ModuleEntry "
            "defines a `globs` field — any real invocation will crash "
            "with AttributeError inside _build_module_message."
        ))

    def test_name_error_matches(self):
        self.assertTrue(_verdict_cites_concrete_code_bug(
            "The helper references `results` but the enclosing scope "
            "only defines `result`; calling this path raises NameError."
        ))

    def test_type_and_key_errors_match(self):
        self.assertTrue(_verdict_cites_concrete_code_bug(
            "calling .split() on None raises TypeError"))
        self.assertTrue(_verdict_cites_concrete_code_bug(
            "lookup without .get() raises KeyError on missing label"))

    def test_import_and_module_not_found_match(self):
        self.assertTrue(_verdict_cites_concrete_code_bug(
            "new import fails with ImportError under Python 3.12"))
        self.assertTrue(_verdict_cites_concrete_code_bug(
            "the helper imports cai_lib.foo which raises "
            "ModuleNotFoundError at runtime"))

    def test_wrong_field_name_matches(self):
        self.assertTrue(_verdict_cites_concrete_code_bug(
            "Minor issue: wrong field name `file_globs` — the dataclass "
            "calls it `globs`."
        ))

    def test_incorrect_method_name_matches(self):
        self.assertTrue(_verdict_cites_concrete_code_bug(
            "incorrect method name — the call site uses `.set_label()` "
            "but the class defines `.set_labels()`."
        ))

    def test_typo_matches(self):
        self.assertTrue(_verdict_cites_concrete_code_bug(
            "There is a typo in the variable name that will cause the "
            "lookup to miss."
        ))

    def test_undefined_name_matches(self):
        self.assertTrue(_verdict_cites_concrete_code_bug(
            "Reference to undefined variable `ctx` in the new branch."
        ))

    def test_design_concern_does_not_match(self):
        self.assertFalse(_verdict_cites_concrete_code_bug(
            "The new helper duplicates logic already present in "
            "cai_lib/foo.py; consider consolidating before merge."
        ))

    def test_scope_concern_does_not_match(self):
        self.assertFalse(_verdict_cites_concrete_code_bug(
            "PR scope is broader than the issue requested; extra file "
            "edits under scripts/ are not mentioned in the remediation."
        ))

    def test_case_insensitive_phrase_matches(self):
        self.assertTrue(_verdict_cites_concrete_code_bug(
            "The call site uses a WRONG METHOD NAME — typo-like bug."
        ))


class TestApprovedToRevisionPendingTransition(unittest.TestCase):
    """The new FSM transition must exist with the expected shape."""

    def test_transition_registered(self):
        names = {t.name for t in PR_TRANSITIONS}
        self.assertIn("approved_to_revision_pending", names)

    def test_transition_label_shape(self):
        from cai_lib.fsm_states import PRState
        from cai_lib.config import (
            LABEL_PR_APPROVED, LABEL_PR_REVISION_PENDING,
        )
        t = next(
            t for t in PR_TRANSITIONS
            if t.name == "approved_to_revision_pending"
        )
        self.assertEqual(t.from_state, PRState.APPROVED)
        self.assertEqual(t.to_state, PRState.REVISION_PENDING)
        self.assertIn(LABEL_PR_APPROVED, t.labels_remove)
        self.assertIn(LABEL_PR_REVISION_PENDING, t.labels_add)


def _pr_fixture(number: int = 1234) -> dict:
    return {
        "number": number,
        "title": "auto-improve: example",
        "headRefName": f"auto-improve/{number}-example",
        "headRefOid": "d7becb043dfd84c2796f35b7deb1353881435930",
        "labels": [{"name": "pr:approved"}],
        "state": "OPEN",
        "mergeable": "MERGEABLE",
        "mergeStateStatus": "CLEAN",
        "mergedAt": None,
        "comments": [],
        "reviews": [],
        "createdAt": "2026-04-20T00:00:00Z",
    }


class TestHandleMergeLowHoldRouting(unittest.TestCase):
    """handle_merge must pick the right transition for a held verdict."""

    def _invoke(self, reasoning: str, confidence: str = "low",
                action: str = "hold") -> tuple[HandlerResult, MagicMock]:
        pr = _pr_fixture()

        # Mock gh + git subprocess calls used by filters + verdict post.
        run_mock = MagicMock()
        run_mock.return_value.returncode = 0
        run_mock.return_value.stdout = ""
        run_mock.return_value.stderr = ""

        # Mock the model call: return a JSON verdict matching
        # _MERGE_JSON_SCHEMA.
        claude_mock = MagicMock()
        claude_mock.return_value.returncode = 0
        claude_mock.return_value.stdout = json.dumps({
            "confidence": confidence,
            "action": action,
            "reasoning": reasoning,
        })
        claude_mock.return_value.stderr = ""

        # Mock GitHub JSON: issue carries :pr-open so the pre-gate passes.
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
        filter_mock = MagicMock(return_value=[])  # no unaddressed comments
        fetch_review_mock = MagicMock(return_value=[])
        has_label_mock = MagicMock(return_value=False)
        set_labels_mock = MagicMock(return_value=True)
        transition_mock = MagicMock(return_value=True)
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
             patch.object(merge_mod, "_issue_has_label", has_label_mock), \
             patch.object(merge_mod, "_set_labels", set_labels_mock), \
             patch.object(merge_mod, "fire_trigger",
                          transition_mock), \
             patch.object(merge_mod, "log_run", log_mock):
            result = merge_mod.handle_merge(pr)

        self.assertIsInstance(result, HandlerResult)
        return result, run_mock

    def test_low_hold_with_attribute_error_routes_to_revision(self):
        reasoning = (
            "runner.py:38 accesses entry.file_globs, but ModuleEntry "
            "defines a `globs` field. Any invocation crashes with "
            "AttributeError inside _build_module_message."
        )
        result, run_mock = self._invoke(reasoning)
        self.assertEqual(result.trigger, "approved_to_revision_pending")
        # A follow-up comment with the new heading must be posted
        # so cai-comment-filter does NOT silently resolve it.
        comment_bodies = [
            call.args[0][call.args[0].index("--body") + 1]
            for call in run_mock.call_args_list
            if call.args and call.args[0][:3] == ["gh", "pr", "comment"]
        ]
        self.assertTrue(any(
            b.startswith("## cai merge: fixable code bug")
            for b in comment_bodies
        ), f"no fixable-bug comment posted; got: {comment_bodies!r}")

    def test_low_hold_design_concern_still_parks_as_human(self):
        reasoning = (
            "PR scope is broader than the issue requested; extra file "
            "edits under scripts/ are not mentioned in the remediation."
        )
        result, _run_mock = self._invoke(reasoning)
        self.assertEqual(result.trigger, "approved_to_human")

    def test_medium_hold_with_attribute_error_still_parks_as_human(self):
        """The redirect is gated on confidence=='low' only — MEDIUM
        holds continue to park at human-needed so the narrower fix is
        conservative (issue #1055)."""
        reasoning = (
            "Possible AttributeError on the new helper's return path, "
            "but unclear whether the branch is reachable."
        )
        result, _run_mock = self._invoke(
            reasoning, confidence="medium"
        )
        self.assertEqual(result.trigger, "approved_to_human")


if __name__ == "__main__":
    unittest.main()
