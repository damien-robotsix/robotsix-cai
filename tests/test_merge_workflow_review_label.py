"""Regression tests for issue #1064 — PRs held on a workflow-file
concern get the `needs-workflow-review` PR label added by the merge
wrapper so admins can filter workflow-review-required holds out of
the generic `pr:human-needed` queue.

The detector `_pr_touches_workflow_files` must fire only on diffs
that modify `.github/workflows/` files. The integrated handler
path must gate the label on `medium + hold` (workflow-file rule
caps at medium) AND a workflow-touching diff.
"""
import json
import os
import sys
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cai_lib.actions import merge as merge_mod
from cai_lib.actions.merge import _pr_touches_workflow_files
from cai_lib.config import LABEL_PR_NEEDS_WORKFLOW_REVIEW

from tests._helpers import _pr_fixture


class TestPrTouchesWorkflowFiles(unittest.TestCase):
    """The detector matches only `.github/workflows/` diff headers."""

    def test_empty_diff_is_false(self):
        self.assertFalse(_pr_touches_workflow_files(""))
        self.assertFalse(_pr_touches_workflow_files(None))  # type: ignore[arg-type]

    def test_workflow_file_modified_matches(self):
        diff = (
            "diff --git a/.github/workflows/regenerate-docs.yml "
            "b/.github/workflows/regenerate-docs.yml\n"
            "index 1111111..2222222 100644\n"
            "--- a/.github/workflows/regenerate-docs.yml\n"
            "+++ b/.github/workflows/regenerate-docs.yml\n"
            "@@ -1,3 +1,4 @@\n"
            " name: regenerate-docs\n"
            "+# new comment\n"
        )
        self.assertTrue(_pr_touches_workflow_files(diff))

    def test_workflow_file_added_matches(self):
        diff = (
            "diff --git a/.github/workflows/new.yml b/.github/workflows/new.yml\n"
            "new file mode 100644\n"
            "index 0000000..abcdef0\n"
            "--- /dev/null\n"
            "+++ b/.github/workflows/new.yml\n"
        )
        self.assertTrue(_pr_touches_workflow_files(diff))

    def test_non_workflow_file_does_not_match(self):
        diff = (
            "diff --git a/cai_lib/actions/merge.py b/cai_lib/actions/merge.py\n"
            "index 1111111..2222222 100644\n"
        )
        self.assertFalse(_pr_touches_workflow_files(diff))

    def test_near_miss_path_does_not_match(self):
        # `.github/NOT-workflows/` must not be treated as workflow.
        diff = (
            "diff --git a/.github/NOT-workflows/foo.yml "
            "b/.github/NOT-workflows/foo.yml\n"
        )
        self.assertFalse(_pr_touches_workflow_files(diff))

    def test_mixed_diff_with_workflow_matches(self):
        diff = (
            "diff --git a/cai_lib/actions/merge.py b/cai_lib/actions/merge.py\n"
            "index 1111111..2222222 100644\n"
            "diff --git a/.github/workflows/ci.yml b/.github/workflows/ci.yml\n"
            "index 3333333..4444444 100644\n"
        )
        self.assertTrue(_pr_touches_workflow_files(diff))


class TestHandleMergeWorkflowReviewLabel(unittest.TestCase):
    """handle_merge must attach `needs-workflow-review` only on
    `medium + hold` verdicts for workflow-touching PRs."""

    def _invoke(self, confidence: str, action: str, reasoning: str,
                diff_stdout: str) -> tuple[MagicMock, MagicMock]:
        pr = _pr_fixture(2000)

        def run_side_effect(cmd, *args, **kwargs):
            result = MagicMock()
            result.returncode = 0
            result.stderr = ""
            if (isinstance(cmd, list) and len(cmd) >= 3
                    and cmd[0] == "gh" and cmd[1] == "pr"
                    and cmd[2] == "diff"):
                result.stdout = diff_stdout
            else:
                result.stdout = ""
            return result

        run_mock = MagicMock(side_effect=run_side_effect)

        claude_mock = MagicMock()
        claude_mock.return_value.returncode = 0
        claude_mock.return_value.stdout = json.dumps({
            "confidence": confidence,
            "action": action,
            "reasoning": reasoning,
        })
        claude_mock.return_value.stderr = ""

        def gh_json_side_effect(args):
            if "issue" in args and "view" in args:
                return {
                    "number": 2000,
                    "title": "auto-improve: example",
                    "labels": [{"name": "auto-improve:pr-open"}],
                    "state": "OPEN",
                    "body": "",
                }
            if "pr" in args and "view" in args:
                return {"statusCheckRollup": []}
            return {}

        log_mock = MagicMock()

        with patch.object(merge_mod, "_run", run_mock), \
             patch.object(merge_mod, "_run_claude_p", claude_mock), \
             patch.object(merge_mod, "_gh_json",
                          MagicMock(side_effect=gh_json_side_effect)), \
             patch.object(merge_mod, "_filter_comments_with_haiku",
                          MagicMock(return_value=[])), \
             patch.object(merge_mod, "_fetch_review_comments",
                          MagicMock(return_value=[])), \
             patch.object(merge_mod, "_issue_has_label",
                          MagicMock(return_value=False)), \
             patch.object(merge_mod, "_set_labels",
                          MagicMock(return_value=True)), \
             patch.object(merge_mod, "fire_trigger",
                          MagicMock(return_value=True)), \
             patch.object(merge_mod, "_git",
                          MagicMock(return_value=MagicMock(returncode=0, stdout="", stderr=""))), \
             patch.object(merge_mod, "log_run", log_mock):
            result = merge_mod.handle_merge(pr)

        from cai_lib.dispatcher import HandlerResult
        self.assertIsInstance(result, HandlerResult)
        return run_mock, log_mock

    def _label_add_calls(self, run_mock: MagicMock) -> list:
        return [
            call for call in run_mock.call_args_list
            if call.args
            and isinstance(call.args[0], list)
            and call.args[0][:3] == ["gh", "pr", "edit"]
            and "--add-label" in call.args[0]
            and LABEL_PR_NEEDS_WORKFLOW_REVIEW in call.args[0]
        ]

    def test_medium_hold_workflow_file_adds_label(self):
        diff = (
            "diff --git a/.github/workflows/regenerate-docs.yml "
            "b/.github/workflows/regenerate-docs.yml\n"
            "index 1111111..2222222 100644\n"
        )
        run_mock, log_mock = self._invoke(
            "medium", "hold",
            "Workflow-file rule caps at medium.",
            diff,
        )
        self.assertEqual(len(self._label_add_calls(run_mock)), 1)
        log_kwargs = log_mock.call_args.kwargs
        self.assertEqual(log_kwargs.get("result"), "held_workflow_review")

    def test_medium_hold_no_workflow_file_no_label(self):
        diff = (
            "diff --git a/cai_lib/actions/merge.py "
            "b/cai_lib/actions/merge.py\n"
            "index 1111111..2222222 100644\n"
        )
        run_mock, log_mock = self._invoke(
            "medium", "hold",
            "Unrelated scope concern.",
            diff,
        )
        self.assertEqual(len(self._label_add_calls(run_mock)), 0)
        log_kwargs = log_mock.call_args.kwargs
        self.assertEqual(log_kwargs.get("result"), "held")

    def test_low_hold_workflow_file_no_label(self):
        """LOW + hold is not caused by the workflow rule (which caps at
        MEDIUM) — leave it to the fixable-bug path or the default park.
        """
        diff = (
            "diff --git a/.github/workflows/regenerate-docs.yml "
            "b/.github/workflows/regenerate-docs.yml\n"
        )
        run_mock, _log_mock = self._invoke(
            "low", "hold",
            "Some unrelated concern.",
            diff,
        )
        self.assertEqual(len(self._label_add_calls(run_mock)), 0)


if __name__ == "__main__":
    unittest.main()
