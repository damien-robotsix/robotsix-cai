"""Tests for the agent-deletion tombstone mechanism in
cai_lib.cmd_helpers_git._apply_agent_edit_staging."""
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cai_lib.cmd_helpers_git import (  # noqa: E402
    _apply_agent_edit_staging,
    _setup_agent_edit_staging,
    _work_directory_block,
)
import cai_lib.cmd_helpers_git as cmd_helpers_git  # noqa: E402


class TestAgentDeletionTombstones(unittest.TestCase):

    def _make_tmp(self) -> Path:
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: shutil.rmtree(tmp, ignore_errors=True))
        return tmp

    def _seed_agent(self, tmp: Path, rel: str) -> Path:
        p = tmp / ".claude" / "agents" / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("stub")
        return p

    def _drop_tombstone(self, tmp: Path, rel: str) -> None:
        t = tmp / ".cai-staging" / "agents-delete" / rel
        t.parent.mkdir(parents=True, exist_ok=True)
        t.write_text("")

    def test_tombstone_deletes_flat_agent(self):
        tmp = self._make_tmp()
        _setup_agent_edit_staging(tmp)
        self._seed_agent(tmp, "cai-triage.md")
        self._drop_tombstone(tmp, "cai-triage.md")
        applied = _apply_agent_edit_staging(tmp)
        self.assertGreaterEqual(applied, 1)
        self.assertFalse((tmp / ".claude/agents/cai-triage.md").exists())
        self.assertFalse((tmp / ".cai-staging").exists(),
                         "staging dir must be cleaned up")

    def test_tombstone_deletes_subfolder_agent(self):
        tmp = self._make_tmp()
        _setup_agent_edit_staging(tmp)
        self._seed_agent(tmp, "lifecycle/cai-refine.md")
        self._drop_tombstone(tmp, "lifecycle/cai-refine.md")
        applied = _apply_agent_edit_staging(tmp)
        self.assertGreaterEqual(applied, 1)
        self.assertFalse(
            (tmp / ".claude/agents/lifecycle/cai-refine.md").exists())

    def test_missing_target_is_silently_skipped(self):
        tmp = self._make_tmp()
        _setup_agent_edit_staging(tmp)
        self._drop_tombstone(tmp, "does-not-exist.md")
        # Must not raise; nothing should be created under agents/.
        _apply_agent_edit_staging(tmp)
        agents_dir = tmp / ".claude" / "agents"
        if agents_dir.exists():
            self.assertFalse(any(agents_dir.rglob("*")))

    def test_unrelated_agents_untouched(self):
        tmp = self._make_tmp()
        _setup_agent_edit_staging(tmp)
        self._seed_agent(tmp, "cai-triage.md")
        self._seed_agent(tmp, "cai-keep.md")
        self._drop_tombstone(tmp, "cai-triage.md")
        _apply_agent_edit_staging(tmp)
        self.assertFalse((tmp / ".claude/agents/cai-triage.md").exists())
        self.assertTrue((tmp / ".claude/agents/cai-keep.md").exists())

    def test_non_md_tombstones_ignored(self):
        tmp = self._make_tmp()
        _setup_agent_edit_staging(tmp)
        self._seed_agent(tmp, "cai-triage.md")
        stray = tmp / ".cai-staging" / "agents-delete" / "cai-triage.txt"
        stray.parent.mkdir(parents=True, exist_ok=True)
        stray.write_text("")
        _apply_agent_edit_staging(tmp)
        self.assertTrue((tmp / ".claude/agents/cai-triage.md").exists())

    def test_write_plus_tombstone_atomic_migration(self):
        """Canonical migration: write new subfolder copy + tombstone
        the flat copy, both applied in one staging pass."""
        tmp = self._make_tmp()
        _setup_agent_edit_staging(tmp)
        self._seed_agent(tmp, "cai-triage.md")
        staged = tmp / ".cai-staging" / "agents" / "lifecycle" / "cai-triage.md"
        staged.parent.mkdir(parents=True, exist_ok=True)
        staged.write_text("NEW CONTENT")
        self._drop_tombstone(tmp, "cai-triage.md")
        applied = _apply_agent_edit_staging(tmp)
        self.assertGreaterEqual(applied, 2)
        self.assertFalse((tmp / ".claude/agents/cai-triage.md").exists())
        self.assertEqual(
            (tmp / ".claude/agents/lifecycle/cai-triage.md").read_text(),
            "NEW CONTENT",
        )

    def test_empty_tombstone_dir_is_noop(self):
        tmp = self._make_tmp()
        _setup_agent_edit_staging(tmp)
        self._seed_agent(tmp, "cai-triage.md")
        _apply_agent_edit_staging(tmp)
        self.assertTrue((tmp / ".claude/agents/cai-triage.md").exists())


class TestStagingMatrix(unittest.TestCase):
    """Matrix tests exercising all four .cai-staging/ subdirectories."""

    def _fresh(self) -> Path:
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: shutil.rmtree(tmp, ignore_errors=True))
        return tmp

    # ------------------------------------------------------------------
    # agents/ subdir
    # ------------------------------------------------------------------

    def test_agent_write_creates_file(self):
        tmp = self._fresh()
        _setup_agent_edit_staging(tmp)
        staged = tmp / ".cai-staging" / "agents" / "foo" / "bar.md"
        staged.parent.mkdir(parents=True, exist_ok=True)
        staged.write_text("A")
        count = _apply_agent_edit_staging(tmp)
        target = tmp / ".claude" / "agents" / "foo" / "bar.md"
        self.assertTrue(target.exists())
        self.assertEqual(target.read_text(), "A")
        self.assertGreaterEqual(count, 1)

    def test_agent_write_overwrites_existing(self):
        tmp = self._fresh()
        _setup_agent_edit_staging(tmp)
        target = tmp / ".claude" / "agents" / "x.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("old")
        staged = tmp / ".cai-staging" / "agents" / "x.md"
        staged.parent.mkdir(parents=True, exist_ok=True)
        staged.write_text("new")
        _apply_agent_edit_staging(tmp)
        self.assertEqual(target.read_text(), "new")

    # ------------------------------------------------------------------
    # plugins/ subdir
    # ------------------------------------------------------------------

    def test_plugin_copytree_merges(self):
        tmp = self._fresh()
        _setup_agent_edit_staging(tmp)
        staged = tmp / ".cai-staging" / "plugins" / "myplug" / "skills" / "foo" / "SKILL.md"
        staged.parent.mkdir(parents=True, exist_ok=True)
        staged.write_text("S")
        count = _apply_agent_edit_staging(tmp)
        target = tmp / ".claude" / "plugins" / "myplug" / "skills" / "foo" / "SKILL.md"
        self.assertTrue(target.exists())
        self.assertEqual(target.read_text(), "S")
        self.assertGreaterEqual(count, 1)

    def test_plugin_copytree_preserves_existing_siblings(self):
        tmp = self._fresh()
        _setup_agent_edit_staging(tmp)
        # Pre-create a sibling plugin file that should NOT be touched
        keep = tmp / ".claude" / "plugins" / "other" / "keep.md"
        keep.parent.mkdir(parents=True, exist_ok=True)
        keep.write_text("keep")
        # Stage a different plugin
        staged = tmp / ".cai-staging" / "plugins" / "newplug" / "x.md"
        staged.parent.mkdir(parents=True, exist_ok=True)
        staged.write_text("x")
        _apply_agent_edit_staging(tmp)
        self.assertTrue(keep.exists())
        self.assertEqual(keep.read_text(), "keep")

    # ------------------------------------------------------------------
    # claudemd/ subdir
    # ------------------------------------------------------------------

    def test_claudemd_root_write(self):
        tmp = self._fresh()
        _setup_agent_edit_staging(tmp)
        staged = tmp / ".cai-staging" / "claudemd" / "CLAUDE.md"
        staged.parent.mkdir(parents=True, exist_ok=True)
        staged.write_text("root")
        _apply_agent_edit_staging(tmp)
        self.assertEqual((tmp / "CLAUDE.md").read_text(), "root")

    def test_claudemd_subdir_write(self):
        tmp = self._fresh()
        _setup_agent_edit_staging(tmp)
        staged = tmp / ".cai-staging" / "claudemd" / "subdir" / "CLAUDE.md"
        staged.parent.mkdir(parents=True, exist_ok=True)
        staged.write_text("sub")
        _apply_agent_edit_staging(tmp)
        self.assertEqual((tmp / "subdir" / "CLAUDE.md").read_text(), "sub")

    def test_claudemd_non_claudemd_files_ignored(self):
        tmp = self._fresh()
        _setup_agent_edit_staging(tmp)
        staged_dir = tmp / ".cai-staging" / "claudemd"
        staged_dir.mkdir(parents=True, exist_ok=True)
        (staged_dir / "README.md").write_text("readme")
        (staged_dir / "CLAUDE.md").write_text("real")
        _apply_agent_edit_staging(tmp)
        self.assertFalse((tmp / "README.md").exists())
        self.assertEqual((tmp / "CLAUDE.md").read_text(), "real")

    # ------------------------------------------------------------------
    # Combined / multi-subdir
    # ------------------------------------------------------------------

    def test_combined_apply_matrix(self):
        """One setup+apply pass exercising all four staging subdirs."""
        tmp = self._fresh()
        _setup_agent_edit_staging(tmp)

        # Agent write
        agent_staged = tmp / ".cai-staging" / "agents" / "a.md"
        agent_staged.parent.mkdir(parents=True, exist_ok=True)
        agent_staged.write_text("agent-content")

        # Plugin file
        plugin_staged = tmp / ".cai-staging" / "plugins" / "p" / "x"
        plugin_staged.parent.mkdir(parents=True, exist_ok=True)
        plugin_staged.write_text("plugin-content")

        # CLAUDE.md
        claude_staged = tmp / ".cai-staging" / "claudemd" / "CLAUDE.md"
        claude_staged.parent.mkdir(parents=True, exist_ok=True)
        claude_staged.write_text("claude-content")

        # Tombstone — pre-seed the target first
        gone = tmp / ".claude" / "agents" / "gone.md"
        gone.parent.mkdir(parents=True, exist_ok=True)
        gone.write_text("gone")
        tombstone = tmp / ".cai-staging" / "agents-delete" / "gone.md"
        tombstone.parent.mkdir(parents=True, exist_ok=True)
        tombstone.write_text("")

        count = _apply_agent_edit_staging(tmp)

        self.assertTrue((tmp / ".claude" / "agents" / "a.md").exists())
        self.assertTrue((tmp / ".claude" / "plugins" / "p" / "x").exists())
        self.assertEqual((tmp / "CLAUDE.md").read_text(), "claude-content")
        self.assertFalse(gone.exists())
        self.assertGreaterEqual(count, 4)

    def test_staging_dir_cleaned_up_after_success(self):
        tmp = self._fresh()
        _setup_agent_edit_staging(tmp)
        staged = tmp / ".cai-staging" / "agents" / "z.md"
        staged.parent.mkdir(parents=True, exist_ok=True)
        staged.write_text("z")
        _apply_agent_edit_staging(tmp)
        self.assertFalse((tmp / ".cai-staging").exists())

    def test_plugin_failure_preserves_staging_and_returns_early(self):
        """If shutil.copytree fails, staging is preserved and the
        function returns early — CLAUDE.md writes and tombstone
        deletions do NOT execute.

        Uses mock.patch to force the failure deterministically (avoids
        platform-specific behaviour of writing a regular file to the
        plugins target path)."""
        tmp = self._fresh()
        _setup_agent_edit_staging(tmp)

        # Agent write (should succeed before the plugin step)
        agent_staged = tmp / ".cai-staging" / "agents" / "ok.md"
        agent_staged.parent.mkdir(parents=True, exist_ok=True)
        agent_staged.write_text("ok")

        # Stage a plugin write
        plugin_staged = tmp / ".cai-staging" / "plugins" / "fail.md"
        plugin_staged.parent.mkdir(parents=True, exist_ok=True)
        plugin_staged.write_text("fail")

        # Stage a CLAUDE.md write (should NOT happen — early return)
        claude_staged = tmp / ".cai-staging" / "claudemd" / "CLAUDE.md"
        claude_staged.parent.mkdir(parents=True, exist_ok=True)
        claude_staged.write_text("should-not-appear")

        # Pre-seed a tombstone target (should NOT be deleted — early return)
        keep_agent = tmp / ".claude" / "agents" / "keep.md"
        keep_agent.parent.mkdir(parents=True, exist_ok=True)
        keep_agent.write_text("keep")
        tombstone = tmp / ".cai-staging" / "agents-delete" / "keep.md"
        tombstone.parent.mkdir(parents=True, exist_ok=True)
        tombstone.write_text("")

        with mock.patch.object(cmd_helpers_git.shutil, "copytree",
                               side_effect=OSError("forced")):
            count = _apply_agent_edit_staging(tmp)

        # .cai-staging preserved (not cleaned up)
        self.assertTrue((tmp / ".cai-staging").exists())
        # CLAUDE.md NOT written
        self.assertFalse((tmp / "CLAUDE.md").exists())
        # Tombstone target NOT deleted
        self.assertTrue(keep_agent.exists())
        # Agent write ran (step 1) but plugin failed (step 2) — count >= 1
        self.assertGreaterEqual(count, 1)

    def test_return_count_sums_all_operations(self):
        """Return value = 2 agent writes + 1 plugin tree + 1 CLAUDE.md
        + 1 tombstone = 5."""
        tmp = self._fresh()
        _setup_agent_edit_staging(tmp)

        # 2 agent writes
        for name in ("a1.md", "a2.md"):
            f = tmp / ".cai-staging" / "agents" / name
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(name)

        # 1 plugin tree (counts as 1 total, not per-file)
        pf = tmp / ".cai-staging" / "plugins" / "mypkg" / "f.md"
        pf.parent.mkdir(parents=True, exist_ok=True)
        pf.write_text("p")

        # 1 CLAUDE.md
        cf = tmp / ".cai-staging" / "claudemd" / "CLAUDE.md"
        cf.parent.mkdir(parents=True, exist_ok=True)
        cf.write_text("c")

        # 1 tombstone with pre-seeded target
        target = tmp / ".claude" / "agents" / "del.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("del")
        ts = tmp / ".cai-staging" / "agents-delete" / "del.md"
        ts.parent.mkdir(parents=True, exist_ok=True)
        ts.write_text("")

        count = _apply_agent_edit_staging(tmp)
        self.assertGreaterEqual(count, 5)

    def test_apply_no_staging_is_noop(self):
        """Calling apply without any .cai-staging/ dir must return 0."""
        tmp = self._fresh()
        # Deliberately do NOT call _setup_agent_edit_staging
        self.assertFalse((tmp / ".cai-staging").exists())
        count = _apply_agent_edit_staging(tmp)
        self.assertEqual(count, 0)

    # ------------------------------------------------------------------
    # Drift-prevention
    # ------------------------------------------------------------------

    def test_protocol_doc_exists(self):
        """docs/cai-staging.md must exist in the repo root."""
        repo_root = Path(__file__).resolve().parent.parent
        self.assertTrue(
            (repo_root / "docs" / "cai-staging.md").is_file(),
            "docs/cai-staging.md is missing — do not delete the protocol reference",
        )

    def test_work_directory_block_documents_agents_delete(self):
        """_work_directory_block must mention agents-delete and the other
        staging subdirs it describes (plugins/ is documented in root CLAUDE.md
        rather than in the dynamic block, so it is not checked here)."""
        block = _work_directory_block(Path("/tmp/xyz"))
        for marker in ("agents-delete", "agents/", "claudemd/"):
            self.assertIn(
                marker, block,
                f"_work_directory_block is missing staging marker: {marker!r}",
            )


if __name__ == "__main__":
    unittest.main()
