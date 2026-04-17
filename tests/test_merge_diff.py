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

    def test_natural_order_preserved(self):
        """Files are packed in their original diff order, not reshuffled."""
        budget = 10_000
        a = _make_file_chunk("cai_lib/a_first.py", 2_000)
        b = _make_file_chunk("cai_lib/b_second.py", 2_000)
        c = _make_file_chunk("tests/test_c.py", 2_000)
        raw = a + b + c
        self.assertLess(len(raw), budget)

        result = _assemble_diff(raw, budget)

        self.assertEqual(result, raw)
        pos_a = result.find("cai_lib/a_first.py")
        pos_b = result.find("cai_lib/b_second.py")
        pos_c = result.find("tests/test_c.py")
        self.assertLess(pos_a, pos_b)
        self.assertLess(pos_b, pos_c)

    def test_tail_files_omitted_when_budget_exhausted(self):
        """When total exceeds budget, trailing chunks are dropped and noted."""
        budget = 5_000
        head = _make_file_chunk("cai_lib/head.py", budget - 200)
        tail = _make_file_chunk("cai_lib/tail.py", 1_000)
        raw = head + tail
        self.assertGreater(len(raw), budget)

        result = _assemble_diff(raw, budget)

        self.assertIn("cai_lib/head.py", result)
        self.assertIn("file(s) omitted", result)
        self.assertIn("cai_lib/tail.py", result.split("file(s) omitted")[1])

    def test_test_files_not_privileged(self):
        """A test-file chunk at the end is dropped if it does not fit."""
        budget = 5_000
        source = _make_file_chunk("cai_lib/impl.py", budget - 200)
        test = _make_file_chunk("tests/test_feature.py", 1_000)
        raw = source + test
        self.assertGreater(len(raw), budget)

        result = _assemble_diff(raw, budget)

        self.assertIn("cai_lib/impl.py", result)
        self.assertIn("file(s) omitted", result)
        self.assertIn("tests/test_feature.py",
                      result.split("file(s) omitted")[1])

    def test_single_oversized_file_omitted(self):
        """A single file larger than the whole budget is listed as omitted."""
        budget = 1_000
        chunk = _make_file_chunk("cai_lib/huge.py", budget + 500)
        raw = chunk
        self.assertGreater(len(raw), budget)

        result = _assemble_diff(raw, budget)

        self.assertIn("file(s) omitted", result)
        self.assertIn("cai_lib/huge.py", result)


if __name__ == "__main__":
    unittest.main()
