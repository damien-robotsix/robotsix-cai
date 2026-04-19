"""Tests for the agent-deletion tombstone mechanism in
cai_lib.cmd_helpers_git._apply_agent_edit_staging."""
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

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


class TestFilesDeleteTombstones(unittest.TestCase):

    def _make_tmp(self) -> Path:
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: shutil.rmtree(tmp, ignore_errors=True))
        return tmp

    def _seed_file(self, tmp: Path, rel: str, content: str = "x") -> Path:
        p = tmp / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
        return p

    def _drop_files_tombstone(self, tmp: Path, rel: str) -> None:
        t = tmp / ".cai-staging" / "files-delete" / rel
        t.parent.mkdir(parents=True, exist_ok=True)
        t.write_text("")

    def _git_ok(self, *a, **kw):
        m = MagicMock()
        m.returncode = 0
        m.stdout = ""
        m.stderr = ""
        return m

    def _git_fail(self, *a, **kw):
        m = MagicMock()
        m.returncode = 1
        m.stdout = ""
        m.stderr = "not in tracked tree"
        return m

    def test_tombstone_deletes_tracked_file(self):
        tmp = self._make_tmp()
        _setup_agent_edit_staging(tmp)
        self._seed_file(tmp, "cai_lib/cmd_agents.py")
        self._drop_files_tombstone(tmp, "cai_lib/cmd_agents.py")
        with patch("cai_lib.cmd_helpers_git._git",
                   side_effect=self._git_ok):
            applied = _apply_agent_edit_staging(tmp)
        self.assertGreaterEqual(applied, 1)
        self.assertFalse((tmp / "cai_lib/cmd_agents.py").exists())
        self.assertFalse((tmp / ".cai-staging").exists())

    def test_missing_target_is_silently_skipped(self):
        tmp = self._make_tmp()
        _setup_agent_edit_staging(tmp)
        self._drop_files_tombstone(tmp, "does/not/exist.py")
        with patch("cai_lib.cmd_helpers_git._git",
                   side_effect=self._git_ok):
            _apply_agent_edit_staging(tmp)  # must not raise

    def test_unrelated_files_untouched(self):
        tmp = self._make_tmp()
        _setup_agent_edit_staging(tmp)
        self._seed_file(tmp, "a/doomed.py")
        self._seed_file(tmp, "a/keep.py", "KEEP")
        self._drop_files_tombstone(tmp, "a/doomed.py")
        with patch("cai_lib.cmd_helpers_git._git",
                   side_effect=self._git_ok):
            _apply_agent_edit_staging(tmp)
        self.assertFalse((tmp / "a/doomed.py").exists())
        self.assertEqual((tmp / "a/keep.py").read_text(), "KEEP")

    def test_untracked_file_skipped(self):
        tmp = self._make_tmp()
        _setup_agent_edit_staging(tmp)
        self._seed_file(tmp, "untracked.py")
        self._drop_files_tombstone(tmp, "untracked.py")
        with patch("cai_lib.cmd_helpers_git._git",
                   side_effect=self._git_fail):
            _apply_agent_edit_staging(tmp)
        # git ls-files failed, so file must remain.
        self.assertTrue((tmp / "untracked.py").exists())

    def test_protected_git_dir_refused(self):
        tmp = self._make_tmp()
        _setup_agent_edit_staging(tmp)
        self._seed_file(tmp, ".git/HEAD", "ref: refs/heads/main")
        self._drop_files_tombstone(tmp, ".git/HEAD")
        with patch("cai_lib.cmd_helpers_git._git",
                   side_effect=self._git_ok):
            _apply_agent_edit_staging(tmp)
        self.assertTrue((tmp / ".git/HEAD").exists())


if __name__ == "__main__":
    unittest.main()
