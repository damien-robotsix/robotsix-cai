"""Tests for the _build_requeue_exemption_block helper in cai_lib.actions.merge.

The helper drives the wrapper-side confirm re-queue scope-expansion
exemption — see the ``Re-queue scope-expansion exemption`` comment
block in ``cai_lib/actions/merge.py`` and the matching
``Exemption: wrapper-injected pre-authorized scope`` section in
``.claude/agents/review/cai-merge.md``.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cai_lib.actions.merge import _build_requeue_exemption_block


def _issue_body(marker: bool, plan: str | None) -> str:
    """Assemble a synthetic issue body for the test fixtures."""
    parts = ["# Issue title", "", "Some description."]
    if plan is not None:
        parts.extend([
            "",
            "<!-- cai-plan-start -->",
            "## Selected Implementation Plan",
            plan,
            "<!-- cai-plan-end -->",
        ])
    if marker:
        parts.extend([
            "",
            "## Confirm re-queue (attempt 1)",
            "",
            "Fix confirmed unsolved. Re-queued for another attempt.",
        ])
    return "\n".join(parts)


_PLAN_WITH_FILES = """### Summary

Do the thing.

### Files to change

- **`cai_lib/actions/merge.py`**: add helper
- **`tests/test_merge_requeue_exemption.py`**: new tests

### Detailed steps

Step 1 — ...
"""


class TestBuildRequeueExemptionBlock(unittest.TestCase):

    def test_empty_body_returns_empty(self):
        self.assertEqual(_build_requeue_exemption_block(""), "")

    def test_no_requeue_marker_returns_empty(self):
        body = _issue_body(marker=False, plan=_PLAN_WITH_FILES)
        self.assertEqual(_build_requeue_exemption_block(body), "")

    def test_marker_without_plan_returns_empty(self):
        body = _issue_body(marker=True, plan=None)
        self.assertEqual(_build_requeue_exemption_block(body), "")

    def test_marker_with_plan_but_no_files_section_returns_empty(self):
        plan_no_files = "### Summary\n\nDo it.\n\n### Detailed steps\n\n..."
        body = _issue_body(marker=True, plan=plan_no_files)
        self.assertEqual(_build_requeue_exemption_block(body), "")

    def test_marker_with_files_section_but_no_paths_returns_empty(self):
        plan_prose_only = (
            "### Files to change\n\n"
            "- Update the relevant helpers.\n"
            "- Add a new test.\n\n"
            "### Detailed steps\n\n..."
        )
        body = _issue_body(marker=True, plan=plan_prose_only)
        self.assertEqual(_build_requeue_exemption_block(body), "")

    def test_happy_path_emits_block_with_all_paths(self):
        body = _issue_body(marker=True, plan=_PLAN_WITH_FILES)
        result = _build_requeue_exemption_block(body)
        self.assertTrue(result.startswith("## Pre-authorized scope expansion"))
        self.assertIn("- `cai_lib/actions/merge.py`", result)
        self.assertIn("- `tests/test_merge_requeue_exemption.py`", result)
        self.assertIn("**Treat every file in this list as in-scope", result)
        self.assertIn("`.github/workflows/`", result)
        # Block must end with a blank line so the subsequent
        # "## PR changes" header is separated from the exemption body
        # when concatenated into the user message.
        self.assertTrue(result.endswith("\n\n"))

    def test_paths_deduplicated_preserving_order(self):
        plan_with_dupes = (
            "### Files to change\n\n"
            "- **`cai_lib/actions/merge.py`**: step A\n"
            "- **`tests/test_merge_requeue_exemption.py`**: step B\n"
            "- **`cai_lib/actions/merge.py`**: step C (same file, later step)\n\n"
            "### Detailed steps\n\n..."
        )
        body = _issue_body(marker=True, plan=plan_with_dupes)
        result = _build_requeue_exemption_block(body)
        # Only one bullet per file.
        self.assertEqual(result.count("- `cai_lib/actions/merge.py`"), 1)
        # First-seen order preserved: merge.py appears before the test.
        merge_pos = result.find("- `cai_lib/actions/merge.py`")
        test_pos = result.find(
            "- `tests/test_merge_requeue_exemption.py`"
        )
        self.assertLess(merge_pos, test_pos)

    def test_multiple_requeue_attempts_also_match(self):
        """Body containing attempt 2 (not just attempt 1) still qualifies."""
        body = _issue_body(marker=False, plan=_PLAN_WITH_FILES)
        body += "\n\n## Confirm re-queue (attempt 2)\n\nSecond retry."
        result = _build_requeue_exemption_block(body)
        self.assertTrue(result.startswith("## Pre-authorized scope expansion"))

    def test_block_concatenates_cleanly_with_pr_changes_header(self):
        """End-to-end: simulate user_message concatenation to verify spacing.

        Ensures the helper's output sits correctly between the issue body
        (which ends with ``\\n\\n``) and the ``## PR changes\\n\\n`` header
        without collapsing or multiplying blank lines.
        """
        body = _issue_body(marker=True, plan=_PLAN_WITH_FILES)
        block = _build_requeue_exemption_block(body)
        # Mirrors the user_message pattern in handle_merge.
        message = (
            "## Linked issue\n\n"
            "some body\n\n"
            f"{block}"
            "## PR changes\n\n"
            "```diff\n(diff)\n```\n"
        )
        # Exactly one blank line between the exemption block end and
        # the PR changes header.
        self.assertIn(
            "rules still apply in full.\n\n## PR changes\n\n", message
        )


if __name__ == "__main__":
    unittest.main()
