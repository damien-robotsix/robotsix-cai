"""Tests for ``cai_lib.actions.review_docs._build_deletion_manifest_block``.

Established by issue #960: the wrapper must pass the agent a
deterministic list of deleted files, not let the agent guess from
``git diff --stat``. These tests lock down the manifest rendering
contract.
"""
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cai_lib.actions import review_docs  # noqa: E402


def _fake_git_result(stdout: str) -> SimpleNamespace:
    return SimpleNamespace(stdout=stdout, stderr="", returncode=0)


class TestDeletionManifestBlock(unittest.TestCase):

    def _make_tmp(self) -> Path:
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: shutil.rmtree(tmp, ignore_errors=True))
        return tmp

    def test_empty_output_renders_no_deletions_block(self):
        tmp = self._make_tmp()
        with patch.object(review_docs, "_git",
                          return_value=_fake_git_result("")):
            block = review_docs._build_deletion_manifest_block(tmp)
        self.assertIn("## Authoritative deletion manifest", block)
        self.assertIn("This PR deletes no files", block)

    def test_deleted_paths_render_as_bullets(self):
        tmp = self._make_tmp()
        # Note: paths intentionally NOT created on disk, so the
        # existence-cross-check keeps them in the manifest.
        stdout = "cai_lib/dead.py\nscripts/gone.sh\n"
        with patch.object(review_docs, "_git",
                          return_value=_fake_git_result(stdout)):
            block = review_docs._build_deletion_manifest_block(tmp)
        self.assertIn("- `cai_lib/dead.py`", block)
        self.assertIn("- `scripts/gone.sh`", block)
        self.assertIn("single source of truth", block)

    def test_still_present_files_are_filtered_out(self):
        tmp = self._make_tmp()
        # Seed a file that git claims is deleted but which is actually
        # present in the work dir — the pathological case the defensive
        # cross-check exists to guard against.
        present = tmp / "cai_lib" / "still_here.py"
        present.parent.mkdir(parents=True, exist_ok=True)
        present.write_text("# real\n")
        stdout = "cai_lib/still_here.py\nscripts/gone.sh\n"
        with patch.object(review_docs, "_git",
                          return_value=_fake_git_result(stdout)):
            block = review_docs._build_deletion_manifest_block(tmp)
        self.assertNotIn("cai_lib/still_here.py", block)
        self.assertIn("- `scripts/gone.sh`", block)

    def test_blank_lines_in_git_output_are_ignored(self):
        tmp = self._make_tmp()
        stdout = "\ncai_lib/dead.py\n\n"
        with patch.object(review_docs, "_git",
                          return_value=_fake_git_result(stdout)):
            block = review_docs._build_deletion_manifest_block(tmp)
        self.assertIn("- `cai_lib/dead.py`", block)
        # No empty bullets.
        self.assertNotIn("- ``", block)


if __name__ == "__main__":
    unittest.main()
