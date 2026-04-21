"""Tests for the _build_pipeline_coedits_exemption_block helper in
cai_lib.actions.merge.

The helper drives the wrapper-side pipeline co-edits scope
exemption — see the ``Pipeline co-edits scope exemption`` comment
block in ``cai_lib/actions/merge.py`` and the matching
``Exemption: wrapper-injected pre-authorized pipeline co-edits``
section in ``.claude/agents/review/cai-merge.md``.
"""
import os
import sys
import unittest

sys.path.insert(
    0, os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)

from cai_lib.actions.merge import (
    _build_pipeline_coedits_exemption_block,
    _is_pipeline_coedit_comment_body,
    _extract_paths_from_files_line,
)


def _comment(body: str) -> dict:
    """Wrap a body string in a minimal comment-shaped dict."""
    return {"body": body}


_DOCS_APPLIED_BODY = (
    "## cai docs review (applied) \u2014 deadbeefcafebabe\n"
    "\n"
    "Summary line.\n"
    "\n"
    "### Fixed: stale_docs\n"
    "\n"
    "**File(s):** README.md, docs/cli.md, cai.py\n"
    "\n"
    "**Description:** doc drift fixed.\n"
    "\n"
    "**What was changed:** updated references.\n"
    "\n"
    "---\n"
    "_Documentation updated automatically by `cai review-docs`._\n"
)

_PRE_MERGE_FINDINGS_BODY = (
    "## cai pre-merge review \u2014 abc123def456\n"
    "\n"
    "### Finding: missing_co_change\n"
    "\n"
    "**File(s):** docs/modules.yaml, docs/agents.md\n"
    "\n"
    "**Description:** missing references.\n"
    "\n"
    "**Suggested fix:** add them.\n"
)

_PRE_MERGE_CLEAN_BODY = (
    "## cai pre-merge review (clean) \u2014 0123456789abcdef\n"
    "\n"
    "No ripple effects found.\n"
)

_DOCS_CLEAN_BODY = (
    "## cai docs review (clean) \u2014 0123456789abcdef\n"
    "\n"
    "No documentation updates needed.\n"
)


class TestIsPipelineCoeditCommentBody(unittest.TestCase):

    def test_empty_body_is_false(self):
        self.assertFalse(_is_pipeline_coedit_comment_body(""))

    def test_docs_applied_is_true(self):
        self.assertTrue(
            _is_pipeline_coedit_comment_body(_DOCS_APPLIED_BODY)
        )

    def test_docs_clean_is_false(self):
        self.assertFalse(
            _is_pipeline_coedit_comment_body(_DOCS_CLEAN_BODY)
        )

    def test_pre_merge_findings_is_true(self):
        self.assertTrue(
            _is_pipeline_coedit_comment_body(_PRE_MERGE_FINDINGS_BODY)
        )

    def test_pre_merge_clean_is_false(self):
        self.assertFalse(
            _is_pipeline_coedit_comment_body(_PRE_MERGE_CLEAN_BODY)
        )

    def test_unrelated_comment_is_false(self):
        self.assertFalse(
            _is_pipeline_coedit_comment_body(
                "Just a plain comment from a human reviewer."
            )
        )

    def test_leading_whitespace_tolerated(self):
        self.assertTrue(
            _is_pipeline_coedit_comment_body(
                "\n\n" + _DOCS_APPLIED_BODY
            )
        )


class TestExtractPathsFromFilesLine(unittest.TestCase):

    def test_simple_comma_split(self):
        self.assertEqual(
            _extract_paths_from_files_line("README.md, docs/cli.md, cai.py"),
            ["README.md", "docs/cli.md", "cai.py"],
        )

    def test_strips_parenthetical_line_decorations(self):
        self.assertEqual(
            _extract_paths_from_files_line(
                "CLAUDE.md (lines 35-37), docs/agents.md (lines 58-65)"
            ),
            ["CLAUDE.md", "docs/agents.md"],
        )

    def test_strips_surrounding_backticks(self):
        self.assertEqual(
            _extract_paths_from_files_line(
                "`.claude/agents/audit/cai-agent-audit.md`"
            ),
            [".claude/agents/audit/cai-agent-audit.md"],
        )

    def test_skips_empty_tokens(self):
        self.assertEqual(
            _extract_paths_from_files_line("README.md, , , cai.py,"),
            ["README.md", "cai.py"],
        )


class TestBuildPipelineCoeditsExemptionBlock(unittest.TestCase):

    def test_empty_comments_returns_empty(self):
        self.assertEqual(
            _build_pipeline_coedits_exemption_block([]), ""
        )

    def test_no_pipeline_comments_returns_empty(self):
        self.assertEqual(
            _build_pipeline_coedits_exemption_block(
                [_comment("Plain human comment."),
                 _comment("## Implement subagent: did things")]
            ),
            "",
        )

    def test_only_clean_pipeline_comments_returns_empty(self):
        self.assertEqual(
            _build_pipeline_coedits_exemption_block(
                [_comment(_PRE_MERGE_CLEAN_BODY),
                 _comment(_DOCS_CLEAN_BODY)]
            ),
            "",
        )

    def test_docs_applied_emits_block_with_paths(self):
        result = _build_pipeline_coedits_exemption_block(
            [_comment(_DOCS_APPLIED_BODY)]
        )
        self.assertTrue(
            result.startswith("## Pre-authorized pipeline co-edits")
        )
        self.assertIn("- `README.md`", result)
        self.assertIn("- `docs/cli.md`", result)
        self.assertIn("- `cai.py`", result)
        self.assertIn(
            "**Treat every file in this list as in-scope", result
        )
        self.assertIn("`.github/workflows/`", result)
        # Block must end with a blank line so the subsequent
        # "## PR changes" header is separated from the exemption
        # body when concatenated into the user message.
        self.assertTrue(result.endswith("\n\n"))

    def test_pre_merge_findings_emits_block_with_paths(self):
        result = _build_pipeline_coedits_exemption_block(
            [_comment(_PRE_MERGE_FINDINGS_BODY)]
        )
        self.assertTrue(
            result.startswith("## Pre-authorized pipeline co-edits")
        )
        self.assertIn("- `docs/modules.yaml`", result)
        self.assertIn("- `docs/agents.md`", result)

    def test_dedupe_preserves_first_seen_order(self):
        # Same file cited in two comments — should appear once,
        # at the position of its first appearance.
        comments = [
            _comment(_DOCS_APPLIED_BODY),  # README.md, docs/cli.md, cai.py
            _comment(_PRE_MERGE_FINDINGS_BODY),  # docs/modules.yaml, docs/agents.md
            _comment(
                "## cai docs review (applied) \u2014 newersha\n"
                "\n"
                "### Fixed: stale_docs\n"
                "\n"
                "**File(s):** README.md, scripts/check-modules-coverage.py\n"
            ),
        ]
        result = _build_pipeline_coedits_exemption_block(comments)
        # README.md appears exactly once.
        self.assertEqual(result.count("- `README.md`"), 1)
        # Order: README.md (from first comment) before
        # docs/modules.yaml (from second), before
        # scripts/check-modules-coverage.py (from third).
        readme_pos = result.find("- `README.md`")
        modules_pos = result.find("- `docs/modules.yaml`")
        new_pos = result.find("- `scripts/check-modules-coverage.py`")
        self.assertLess(readme_pos, modules_pos)
        self.assertLess(modules_pos, new_pos)

    def test_block_concatenates_cleanly_with_pr_changes_header(self):
        """End-to-end: simulate user_message concatenation to verify
        spacing.

        Ensures the helper's output sits correctly between the
        issue body (which ends with ``\\n\\n``) and the
        ``## PR changes\\n\\n`` header without collapsing or
        multiplying blank lines.
        """
        block = _build_pipeline_coedits_exemption_block(
            [_comment(_DOCS_APPLIED_BODY)]
        )
        # Mirrors the user_message pattern in handle_merge.
        message = (
            "## Linked issue\n\n"
            "some body\n\n"
            ""  # requeue_block (absent for this PR)
            f"{block}"
            "## PR changes\n\n"
            "```diff\n(diff)\n```\n"
        )
        # Exactly one blank line between the exemption block end
        # and the PR changes header.
        self.assertIn(
            "rules still apply in full.\n\n## PR changes\n\n",
            message,
        )


if __name__ == "__main__":
    unittest.main()
