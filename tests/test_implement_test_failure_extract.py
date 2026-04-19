"""Tests for cai_lib.actions.implement._extract_test_failures."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cai_lib.actions.implement import _extract_test_failures


_VERBOSE_OUTPUT = """test_foo (tests.test_foo.TestFoo.test_foo) ... ok
test_bar (tests.test_bar.TestBar.test_bar) ... FAIL
test_baz (tests.test_baz.TestBaz.test_baz) ... ERROR

======================================================================
FAIL: test_bar (tests.test_bar.TestBar.test_bar)
----------------------------------------------------------------------
Traceback (most recent call last):
  File "tests/test_bar.py", line 42, in test_bar
    self.assertEqual(1, 2)
AssertionError: 1 != 2

======================================================================
ERROR: test_baz (tests.test_baz.TestBaz.test_baz)
----------------------------------------------------------------------
Traceback (most recent call last):
  File "tests/test_baz.py", line 7, in test_baz
    raise RuntimeError("oh no")
RuntimeError: oh no

----------------------------------------------------------------------
Ran 3 tests in 0.002s

FAILED (failures=1, errors=1)
"""


class TestExtractTestFailures(unittest.TestCase):
    def test_lists_failing_test_names(self):
        result = _extract_test_failures(_VERBOSE_OUTPUT)
        self.assertIn("FAIL: tests.test_bar.TestBar.test_bar", result)
        self.assertIn("ERROR: tests.test_baz.TestBaz.test_baz", result)

    def test_includes_traceback_blocks(self):
        result = _extract_test_failures(_VERBOSE_OUTPUT)
        self.assertIn("AssertionError: 1 != 2", result)
        self.assertIn("RuntimeError: oh no", result)

    def test_includes_final_summary(self):
        result = _extract_test_failures(_VERBOSE_OUTPUT)
        self.assertIn("FAILED (failures=1, errors=1)", result)

    def test_omits_passing_test_lines(self):
        result = _extract_test_failures(_VERBOSE_OUTPUT)
        self.assertNotIn("test_foo (", result)
        self.assertNotIn("... ok", result)

    def test_omits_ran_summary_divider(self):
        result = _extract_test_failures(_VERBOSE_OUTPUT)
        self.assertNotIn("Ran 3 tests in", result)

    def test_respects_max_chars(self):
        result = _extract_test_failures(_VERBOSE_OUTPUT, max_chars=80)
        self.assertTrue(result.endswith("... (truncated)"))
        # Body is clipped to max_chars then the truncation marker is appended.
        self.assertLessEqual(len(result), 80 + len("\n\n... (truncated)"))

    def test_falls_back_to_raw_when_no_markers(self):
        raw = "some crash output with no FAIL or ERROR markers"
        result = _extract_test_failures(raw)
        self.assertEqual(result, raw)

    def test_falls_back_truncates_raw_when_no_markers(self):
        raw = "x" * 5000
        result = _extract_test_failures(raw, max_chars=1000)
        self.assertTrue(result.endswith("... (truncated)"))
        self.assertLessEqual(len(result), 1000 + len("\n\n... (truncated)"))


if __name__ == "__main__":
    unittest.main()
