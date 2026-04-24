"""Tests for the _read_prefetched_files helper and the
issue_body extension to _work_directory_block in
cai_lib.cmd_helpers_git."""
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cai_lib.cmd_helpers_git import (  # noqa: E402
    _read_prefetched_files,
    _work_directory_block,
)
from cai_lib.cmd_helpers_issues import _parse_files_to_change  # noqa: E402


class TestReadPrefetchedFiles(unittest.TestCase):

    def _make_tmp(self) -> Path:
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: shutil.rmtree(tmp, ignore_errors=True))
        return tmp

    def test_empty_paths_returns_empty_string(self):
        tmp = self._make_tmp()
        result = _read_prefetched_files(tmp, [])
        self.assertEqual(result, "")

    def test_missing_file_skipped_with_log(self):
        tmp = self._make_tmp()
        # Create one real file and reference one missing file.
        real = tmp / "real.py"
        real.write_text("# real content\n")
        stderr_lines = []
        with patch("sys.stderr") as mock_err:
            mock_err.write = lambda s: stderr_lines.append(s)
            result = _read_prefetched_files(tmp, ["missing.py", "real.py"])
        # The real file's content must appear.
        self.assertIn("### real.py", result)
        self.assertIn("# real content", result)
        # The preload section header must appear.
        self.assertIn("## Pre-loaded file contents", result)

    def test_per_file_cap_skips_large_file(self):
        tmp = self._make_tmp()
        # A file whose estimated token count (len//4) exceeds per_file_token_cap.
        big = tmp / "big.py"
        big.write_text("x" * 1000)  # 1000 chars → ~250 tokens
        small = tmp / "small.py"
        small.write_text("# small\n")

        stderr_output = []
        original_stderr = sys.stderr
        class CapturingStderr:
            def write(self, s):
                stderr_output.append(s)
            def flush(self):
                pass
        sys.stderr = CapturingStderr()
        try:
            result = _read_prefetched_files(
                tmp, ["big.py", "small.py"], per_file_token_cap=100
            )
        finally:
            sys.stderr = original_stderr

        # big.py should be skipped; small.py should appear.
        self.assertNotIn("big.py", result)
        self.assertIn("### small.py", result)
        combined = "".join(stderr_output)
        self.assertIn("too large", combined)

    def test_total_cap_stops_accumulation(self):
        tmp = self._make_tmp()
        # Two files each ~100 tokens; total cap of 150 should include first
        # but skip the second.
        f1 = tmp / "file1.py"
        f1.write_text("a" * 400)  # ~100 tokens
        f2 = tmp / "file2.py"
        f2.write_text("b" * 400)  # ~100 tokens

        stderr_output = []
        original_stderr = sys.stderr
        class CapturingStderr:
            def write(self, s):
                stderr_output.append(s)
            def flush(self):
                pass
        sys.stderr = CapturingStderr()
        try:
            result = _read_prefetched_files(
                tmp, ["file1.py", "file2.py"],
                per_file_token_cap=200, total_token_cap=150,
            )
        finally:
            sys.stderr = original_stderr

        self.assertIn("### file1.py", result)
        self.assertNotIn("### file2.py", result)
        combined = "".join(stderr_output)
        self.assertIn("total cap exceeded", combined)

    def test_issue_body_with_files_to_change_section(self):
        tmp = self._make_tmp()
        target = tmp / "cai_lib" / "foo.py"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("def foo(): pass\n")

        issue_body = (
            "### Files to change\n\n"
            "- `cai_lib/foo.py`: add bar function\n"
        )
        result = _work_directory_block(tmp, issue_body)
        self.assertIn("## Pre-loaded file contents", result)
        self.assertIn("### cai_lib/foo.py", result)
        self.assertIn("def foo(): pass", result)

    def test_issue_body_without_files_to_change_section(self):
        tmp = self._make_tmp()
        issue_body = "Some issue body without any files section."
        # Suppress shared-memory injection (implement-plan-scope-gate.md
        # contains the literal phrase "## Pre-loaded file contents" which
        # would cause false-positive assertNotIn failures — same pattern
        # as PR#1204 / PR#1226).
        with patch("cai_lib.cmd_helpers_git._read_shared_memory", return_value=""):
            result = _work_directory_block(tmp, issue_body)
        # Should fall back to current behavior — no preload section.
        self.assertNotIn("## Pre-loaded file contents", result)

    def test_no_issue_body_no_preload(self):
        tmp = self._make_tmp()
        # Same shared-memory isolation as test_issue_body_without_files_to_change_section.
        with patch("cai_lib.cmd_helpers_git._read_shared_memory", return_value=""):
            result = _work_directory_block(tmp)
        self.assertNotIn("## Pre-loaded file contents", result)


class TestParseFilesToChange(unittest.TestCase):

    def test_parses_paths_from_section(self):
        body = (
            "### Files to change\n\n"
            "- `cai_lib/foo.py`: add thing\n"
            "- `cai_lib/bar.md`: update docs\n"
        )
        paths = _parse_files_to_change(body)
        self.assertIn("cai_lib/foo.py", paths)
        self.assertIn("cai_lib/bar.md", paths)

    def test_returns_empty_for_missing_section(self):
        self.assertEqual(_parse_files_to_change("No section here"), [])

    def test_returns_empty_for_empty_input(self):
        self.assertEqual(_parse_files_to_change(""), [])


if __name__ == "__main__":
    unittest.main()
