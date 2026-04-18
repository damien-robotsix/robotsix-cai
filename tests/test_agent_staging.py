"""Tests for the agent-deletion tombstone mechanism in
cai_lib.cmd_helpers_git._apply_agent_edit_staging."""
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cai_lib.cmd_helpers_git import (  # noqa: E402
    _apply_agent_edit_staging,
    _setup_agent_edit_staging,
)


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


if __name__ == "__main__":
    unittest.main()
