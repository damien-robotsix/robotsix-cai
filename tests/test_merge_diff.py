"""Tests for the _assemble_diff helper in cai_lib.actions.merge."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cai_lib.actions.merge import _assemble_diff


def _make_file_chunk(path: str, size: int) -> str:
    """Return a synthetic diff --git chunk of at least *size* bytes.

    The body always ends with a newline so that concatenated chunks produce
    valid line boundaries (required for the ``^diff --git`` split to work).
    """
    header = f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n"
    body_size = max(0, size - len(header))
    # Fill with '+' lines of 80 chars each so the diff looks plausible.
    # Do NOT truncate mid-line — keep whole lines so the chunk ends with \n.
    lines = []
    while sum(len(l) for l in lines) < body_size:
        lines.append("+" + "x" * 79 + "\n")
    body = "".join(lines)
    return header + body


class TestAssembleDiff(unittest.TestCase):

    def test_under_budget_returned_unchanged(self):
        """Diffs within the budget should be returned verbatim."""
        raw = _make_file_chunk("src/foo.py", 1_000)
        result = _assemble_diff(raw, 40_000)
        self.assertEqual(result, raw)

    def test_test_files_prioritised_over_source_files(self):
        """When total diff exceeds budget, test-file chunks appear before source chunks."""
        # Create a large source chunk that nearly fills the budget, and a
        # smaller test chunk appended after it.  Without prioritisation the
        # test chunk would be cut off; with prioritisation it should appear
        # first and be retained.
        budget = 5_000
        # Use sizes that are multiples of 81 (line length) + header size so
        # the chunk lengths are predictable and total reliably exceeds budget.
        source_chunk = _make_file_chunk("src/big_source.py", budget - 200)
        test_chunk = _make_file_chunk("tests/test_feature.py", 400)
        raw = source_chunk + test_chunk  # test chunk at end, total > budget
        self.assertGreater(len(raw), budget)

        result = _assemble_diff(raw, budget)

        # The test chunk should be present (it was prioritised).
        self.assertIn("tests/test_feature.py", result)
        # An omission note should appear because source was dropped.
        self.assertIn("file(s) omitted", result)
        # Test chunk should appear before source chunk (if source was included at all).
        test_pos = result.find("tests/test_feature.py")
        source_pos = result.find("src/big_source.py")
        if source_pos != -1:
            self.assertLess(test_pos, source_pos)

    def test_source_file_omitted_when_test_fills_budget(self):
        """Source files are omitted when test files consume the budget."""
        budget = 3_000
        test_chunk = _make_file_chunk("tests/test_x.py", budget - 200)
        source_chunk = _make_file_chunk("src/impl.py", 600)
        raw = source_chunk + test_chunk
        self.assertGreater(len(raw), budget)

        result = _assemble_diff(raw, budget)

        self.assertIn("tests/test_x.py", result)
        self.assertIn("file(s) omitted", result)
        self.assertIn("src/impl.py", result.split("file(s) omitted")[1])

    def test_test_file_alone_exceeds_budget(self):
        """When even the test file alone exceeds budget, include what fits and note omission."""
        budget = 1_000
        # A test chunk larger than the whole budget.
        test_chunk = _make_file_chunk("tests/test_huge.py", budget + 500)
        raw = test_chunk
        self.assertGreater(len(raw), budget)

        result = _assemble_diff(raw, budget)

        # The result should be truncated (preamble empty, no chunk fits).
        # The omission note should list the test file as omitted.
        self.assertIn("file(s) omitted", result)
        self.assertIn("tests/test_huge.py", result)


if __name__ == "__main__":
    unittest.main()
