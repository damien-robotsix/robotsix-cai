"""Tests for the plan-scope enforcer in cai_lib.actions.implement
(issue #1074).

The enforcer parses the stored plan's `### Files to change` section
and `#### Step N — Edit/Write` headers to decide which files the
cai-implement subagent is allowed to write. Out-of-scope files are
reverted before the commit step so they cannot sink the PR via
unrelated regression failures (e.g. issue #1065 wrote an unrelated
test module referencing real git operations and triggered two
consecutive `tests_failed` diverts).
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cai_lib.actions.implement import (
    _ALWAYS_IN_SCOPE,
    _canonical_staging_aliases,
    _list_changed_paths,
    _normalize_plan_path,
    _parse_plan_scope,
)


_PLAN_WITH_FILES_AND_STEPS = """\
## Plan

### Summary
Do the thing.

### Files to change
- **`/tmp/cai-plan-1065-a5338f84/cai_lib/fsm_transitions.py`**: edit X.
- **`/tmp/cai-plan-1065-a5338f84/cai_lib/actions/open_pr.py`**: rewrite.
- **`tests/test_open_pr_non_bot_branch.py`**: new file.

### Detailed steps

#### Step 1 — Edit `/tmp/cai-plan-1065-a5338f84/cai_lib/fsm_transitions.py`

**old_string:**

#### Step 2 — Write `/tmp/cai-plan-1065-a5338f84/cai_lib/actions/open_pr.py`

**Intent:** rewrite.

#### Step 3 — Write `/tmp/cai-plan-1065-a5338f84/tests/test_open_pr_non_bot_branch.py`

**Intent:** new test.
"""


_PLAN_WITH_STAGING = """\
## Plan

### Files to change
- **`.cai-staging/agents/implementation/cai-implement.md`**: extend rule 2.
- **`.cai-staging/agents-delete/lifecycle/cai-old.md`**: tombstone.
- **`cai_lib/actions/implement.py`**: add helpers.

### Detailed steps
"""


_PLAN_NO_FILES_SECTION = """\
## Plan

### Summary
Some prose.

### Detailed steps

#### Step 1 — Edit `cai_lib/foo.py`
"""


class TestNormalizePlanPath(unittest.TestCase):
    def test_strips_implement_work_dir_prefix(self):
        self.assertEqual(
            _normalize_plan_path(
                "/tmp/cai-implement-123-deadbeef/cai_lib/foo.py"
            ),
            "cai_lib/foo.py",
        )

    def test_strips_plan_work_dir_prefix(self):
        self.assertEqual(
            _normalize_plan_path(
                "/tmp/cai-plan-1065-a5338f84/tests/test_x.py"
            ),
            "tests/test_x.py",
        )

    def test_relative_path_passes_through(self):
        self.assertEqual(
            _normalize_plan_path("cai_lib/foo.py"),
            "cai_lib/foo.py",
        )

    def test_empty_input_returns_empty_string(self):
        self.assertEqual(_normalize_plan_path(""), "")
        self.assertEqual(_normalize_plan_path(None), "")  # type: ignore[arg-type]

    def test_non_cai_absolute_passes_through_without_leading_slash(self):
        self.assertEqual(
            _normalize_plan_path("/tmp/other/foo.py"),
            "tmp/other/foo.py",
        )


class TestCanonicalStagingAliases(unittest.TestCase):
    def test_agents_staging_maps_to_live_path(self):
        self.assertEqual(
            _canonical_staging_aliases(
                ".cai-staging/agents/implementation/cai-implement.md"
            ),
            [".claude/agents/implementation/cai-implement.md"],
        )

    def test_agents_delete_maps_to_live_path(self):
        self.assertEqual(
            _canonical_staging_aliases(
                ".cai-staging/agents-delete/lifecycle/cai-old.md"
            ),
            [".claude/agents/lifecycle/cai-old.md"],
        )

    def test_plugins_staging_maps_to_live_path(self):
        self.assertEqual(
            _canonical_staging_aliases(
                ".cai-staging/plugins/cai-skills/skills/foo/SKILL.md"
            ),
            [".claude/plugins/cai-skills/skills/foo/SKILL.md"],
        )

    def test_claudemd_staging_maps_to_live_path(self):
        self.assertEqual(
            _canonical_staging_aliases(
                ".cai-staging/claudemd/subdir/CLAUDE.md"
            ),
            ["subdir/CLAUDE.md"],
        )

    def test_files_delete_maps_to_live_path(self):
        self.assertEqual(
            _canonical_staging_aliases(
                ".cai-staging/files-delete/cai_lib/dead.py"
            ),
            ["cai_lib/dead.py"],
        )

    def test_non_staging_returns_empty(self):
        self.assertEqual(
            _canonical_staging_aliases("cai_lib/foo.py"),
            [],
        )


class TestParsePlanScope(unittest.TestCase):
    def test_always_in_scope_entries_present(self):
        scope = _parse_plan_scope("")
        self.assertTrue(_ALWAYS_IN_SCOPE.issubset(scope))
        self.assertIn(".cai/pr-context.md", scope)

    def test_none_input_returns_always_in_scope_only(self):
        self.assertEqual(_parse_plan_scope(None), set(_ALWAYS_IN_SCOPE))

    def test_files_to_change_section_parsed(self):
        scope = _parse_plan_scope(_PLAN_WITH_FILES_AND_STEPS)
        self.assertIn("cai_lib/fsm_transitions.py", scope)
        self.assertIn("cai_lib/actions/open_pr.py", scope)
        self.assertIn("tests/test_open_pr_non_bot_branch.py", scope)

    def test_step_headers_parsed(self):
        scope = _parse_plan_scope(_PLAN_WITH_FILES_AND_STEPS)
        # Step 3's write target is in step headers even though the
        # Files-to-change bullet already lists it — both sources must
        # converge on the same relative path.
        self.assertIn("tests/test_open_pr_non_bot_branch.py", scope)

    def test_staging_paths_expand_to_live_aliases(self):
        scope = _parse_plan_scope(_PLAN_WITH_STAGING)
        # Staging form preserved
        self.assertIn(
            ".cai-staging/agents/implementation/cai-implement.md", scope,
        )
        # Live alias added
        self.assertIn(
            ".claude/agents/implementation/cai-implement.md", scope,
        )
        # Delete tombstone canonicalised to live agent path
        self.assertIn(".claude/agents/lifecycle/cai-old.md", scope)

    def test_out_of_scope_path_not_in_scope(self):
        scope = _parse_plan_scope(_PLAN_WITH_FILES_AND_STEPS)
        self.assertNotIn(
            "tests/test_merge_workflow_review_label.py", scope,
        )


class TestListChangedPaths(unittest.TestCase):
    """Integration test: initialise a throwaway git repo and verify
    the helper collates tracked modifications, staged additions, and
    untracked files."""

    def setUp(self):
        import subprocess
        self.tmp = tempfile.mkdtemp(prefix="cai-scope-test-")
        self.work = Path(self.tmp)
        subprocess.run(
            ["git", "init", "-q", str(self.work)], check=True,
        )
        subprocess.run(
            ["git", "-C", str(self.work), "config", "user.email", "t@example.com"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(self.work), "config", "user.name", "Tester"],
            check=True,
        )
        (self.work / "a.py").write_text("x = 1\n")
        subprocess.run(
            ["git", "-C", str(self.work), "add", "a.py"], check=True,
        )
        subprocess.run(
            ["git", "-C", str(self.work), "commit", "-q", "-m", "init"],
            check=True,
        )

    def tearDown(self):
        import shutil as _shutil
        _shutil.rmtree(self.tmp, ignore_errors=True)

    def test_no_changes_returns_empty(self):
        self.assertEqual(_list_changed_paths(self.work), [])

    def test_untracked_file_listed(self):
        (self.work / "b.py").write_text("y = 2\n")
        paths = _list_changed_paths(self.work)
        self.assertIn("b.py", paths)

    def test_modified_and_untracked_both_listed(self):
        (self.work / "a.py").write_text("x = 99\n")
        (self.work / "c.py").write_text("z = 3\n")
        paths = _list_changed_paths(self.work)
        self.assertIn("a.py", paths)
        self.assertIn("c.py", paths)

    def test_no_duplicate_entries(self):
        (self.work / "a.py").write_text("x = 99\n")
        (self.work / "b.py").write_text("y = 2\n")
        paths = _list_changed_paths(self.work)
        self.assertEqual(len(paths), len(set(paths)))


class TestParsePlanScopeWithoutFilesToChange(unittest.TestCase):
    """When the plan omits `### Files to change`, the parser still
    returns `_ALWAYS_IN_SCOPE` plus any Step-header paths. The
    enforcer layer (tested separately) refuses to enforce in that
    case — we only validate the parser here."""

    def test_step_headers_still_parsed(self):
        scope = _parse_plan_scope(_PLAN_NO_FILES_SECTION)
        self.assertIn("cai_lib/foo.py", scope)


if __name__ == "__main__":
    unittest.main()
