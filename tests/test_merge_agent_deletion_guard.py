"""Tests for the unauthorized agent-file deletion guard in
cai_lib.actions.merge (issue #1024).

The guard catches pre-merge scope creep when ``cai-review-docs`` co-edits
tombstone an ``.claude/agents/*.md`` file that the stored plan never
authorized. See the ``Unauthorized agent-file deletion guard`` comment
block in ``cai_lib/actions/merge.py``.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cai_lib.actions.merge import (
    _canonicalize_agent_plan_path,
    _detect_unauthorized_agent_deletions,
    _parse_deleted_agent_files_from_diff,
)


# ---------------------------------------------------------------------------
# Diff fixtures
# ---------------------------------------------------------------------------

_DIFF_DELETES_CAI_SELECT = """\
diff --git a/.claude/agents/implementation/cai-select.md b/.claude/agents/implementation/cai-select.md
deleted file mode 100644
index abc1234..0000000
--- a/.claude/agents/implementation/cai-select.md
+++ /dev/null
@@ -1,3 +0,0 @@
-old
-content
-here
diff --git a/.claude/agents/lifecycle/cai-rescue.md b/.claude/agents/lifecycle/cai-rescue.md
index def4567..fed7654 100644
--- a/.claude/agents/lifecycle/cai-rescue.md
+++ b/.claude/agents/lifecycle/cai-rescue.md
@@ -1,2 +1,2 @@
-old line
+new line
"""

_DIFF_NO_DELETIONS = """\
diff --git a/cai_lib/actions/merge.py b/cai_lib/actions/merge.py
index abc..def 100644
--- a/cai_lib/actions/merge.py
+++ b/cai_lib/actions/merge.py
@@ -1,1 +1,1 @@
-old
+new
"""

_DIFF_DELETES_NON_AGENT = """\
diff --git a/cai_lib/legacy_helper.py b/cai_lib/legacy_helper.py
deleted file mode 100644
index abc..000
--- a/cai_lib/legacy_helper.py
+++ /dev/null
@@ -1 +0,0 @@
-pass
"""


# ---------------------------------------------------------------------------
# Issue body fixtures
# ---------------------------------------------------------------------------

def _issue_with_plan(files_to_change_bullets: str) -> str:
    return (
        "# Issue title\n\n"
        "Description here.\n\n"
        "<!-- cai-plan-start -->\n"
        "## Selected Implementation Plan\n\n"
        "### Summary\n\n"
        "Do the thing.\n\n"
        "### Files to change\n\n"
        f"{files_to_change_bullets}\n"
        "### Detailed steps\n\n"
        "...\n"
        "<!-- cai-plan-end -->\n"
    )


class TestParseDeletedAgentFilesFromDiff(unittest.TestCase):

    def test_empty_diff(self):
        self.assertEqual(_parse_deleted_agent_files_from_diff(""), [])

    def test_no_deletions(self):
        self.assertEqual(
            _parse_deleted_agent_files_from_diff(_DIFF_NO_DELETIONS), [],
        )

    def test_agent_deletion_detected(self):
        self.assertEqual(
            _parse_deleted_agent_files_from_diff(_DIFF_DELETES_CAI_SELECT),
            [".claude/agents/implementation/cai-select.md"],
        )

    def test_non_agent_deletion_ignored(self):
        self.assertEqual(
            _parse_deleted_agent_files_from_diff(_DIFF_DELETES_NON_AGENT),
            [],
        )

    def test_deduplication(self):
        dup = _DIFF_DELETES_CAI_SELECT + _DIFF_DELETES_CAI_SELECT
        self.assertEqual(
            _parse_deleted_agent_files_from_diff(dup),
            [".claude/agents/implementation/cai-select.md"],
        )


class TestCanonicalizeAgentPlanPath(unittest.TestCase):

    def test_tombstone_path_canonicalized(self):
        self.assertEqual(
            _canonicalize_agent_plan_path(
                ".cai-staging/agents-delete/implementation/cai-select.md"
            ),
            ".claude/agents/implementation/cai-select.md",
        )

    def test_direct_agent_path_kept(self):
        self.assertEqual(
            _canonicalize_agent_plan_path(
                ".claude/agents/implementation/cai-select.md"
            ),
            ".claude/agents/implementation/cai-select.md",
        )

    def test_edit_intent_rejected(self):
        self.assertIsNone(
            _canonicalize_agent_plan_path(
                ".cai-staging/agents/implementation/cai-select.md"
            )
        )

    def test_unrelated_path_rejected(self):
        self.assertIsNone(_canonicalize_agent_plan_path("cai_lib/foo.py"))

    def test_empty_path_rejected(self):
        self.assertIsNone(_canonicalize_agent_plan_path(""))


class TestDetectUnauthorizedAgentDeletions(unittest.TestCase):

    def test_no_deletions_returns_empty(self):
        body = _issue_with_plan(
            "- **`cai_lib/actions/merge.py`**: add helper\n"
        )
        self.assertEqual(
            _detect_unauthorized_agent_deletions(_DIFF_NO_DELETIONS, body),
            [],
        )

    def test_unauthorized_deletion_flagged(self):
        body = _issue_with_plan(
            "- **`.cai-staging/agents/lifecycle/cai-rescue.md`**: edit\n"
            "- **`cai_lib/cmd_rescue.py`**: comment update\n"
        )
        self.assertEqual(
            _detect_unauthorized_agent_deletions(
                _DIFF_DELETES_CAI_SELECT, body
            ),
            [".claude/agents/implementation/cai-select.md"],
        )

    def test_deletion_authorized_via_tombstone(self):
        body = _issue_with_plan(
            "- **`.cai-staging/agents-delete/implementation/cai-select.md`**: delete\n"
        )
        self.assertEqual(
            _detect_unauthorized_agent_deletions(
                _DIFF_DELETES_CAI_SELECT, body
            ),
            [],
        )

    def test_deletion_authorized_via_direct_live_path(self):
        body = _issue_with_plan(
            "- **`.claude/agents/implementation/cai-select.md`**: remove entirely\n"
        )
        self.assertEqual(
            _detect_unauthorized_agent_deletions(
                _DIFF_DELETES_CAI_SELECT, body
            ),
            [],
        )

    def test_edit_intent_does_not_authorize_deletion(self):
        body = _issue_with_plan(
            "- **`.cai-staging/agents/implementation/cai-select.md`**: edit\n"
        )
        self.assertEqual(
            _detect_unauthorized_agent_deletions(
                _DIFF_DELETES_CAI_SELECT, body
            ),
            [".claude/agents/implementation/cai-select.md"],
        )

    def test_no_stored_plan_flags_all_deletions(self):
        body = "# Issue title\n\nNo plan block here.\n"
        self.assertEqual(
            _detect_unauthorized_agent_deletions(
                _DIFF_DELETES_CAI_SELECT, body
            ),
            [".claude/agents/implementation/cai-select.md"],
        )

    def test_missing_files_section_flags_all_deletions(self):
        body = (
            "# Issue title\n\n"
            "<!-- cai-plan-start -->\n"
            "## Selected Implementation Plan\n\n"
            "### Summary\n\nDo it.\n\n"
            "### Detailed steps\n\n...\n"
            "<!-- cai-plan-end -->\n"
        )
        self.assertEqual(
            _detect_unauthorized_agent_deletions(
                _DIFF_DELETES_CAI_SELECT, body
            ),
            [".claude/agents/implementation/cai-select.md"],
        )


if __name__ == "__main__":
    unittest.main()
