"""Tests for cai_lib.actions.implement._extract_referenced_helpers
and _enclosing_function_source (issue #987)."""
import os
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cai_lib.actions.implement import (
    _enclosing_function_source,
    _extract_referenced_helpers,
)


_HELPER_SOURCE = textwrap.dedent(
    """\
    def outer(x):
        return x + 1


    class C:
        def method(self, y):
            return y * 2

        def other(self):
            def nested(z):
                return z - 1
            return nested(self.method(0))
    """
)


class TestEnclosingFunctionSource(unittest.TestCase):
    def test_returns_top_level_function(self):
        res = _enclosing_function_source(_HELPER_SOURCE, 2)
        self.assertIsNotNone(res)
        name, src = res
        self.assertEqual(name, "outer")
        self.assertIn("return x + 1", src)

    def test_returns_method_in_class(self):
        res = _enclosing_function_source(_HELPER_SOURCE, 7)
        self.assertIsNotNone(res)
        name, src = res
        self.assertEqual(name, "method")
        self.assertIn("return y * 2", src)

    def test_returns_innermost_nested_function(self):
        res = _enclosing_function_source(_HELPER_SOURCE, 11)
        self.assertIsNotNone(res)
        name, src = res
        self.assertEqual(name, "nested")
        self.assertIn("return z - 1", src)

    def test_returns_none_outside_any_function(self):
        # Line 5 is the blank line between `outer` and `class C`.
        res = _enclosing_function_source(_HELPER_SOURCE, 5)
        self.assertIsNone(res)

    def test_syntax_error_returns_none(self):
        res = _enclosing_function_source("def bad(:\n  pass\n", 1)
        self.assertIsNone(res)


class TestExtractReferencedHelpers(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.work_dir = Path(self.tmp.name)
        (self.work_dir / "cai_lib").mkdir()
        (self.work_dir / "cai_lib" / "foo.py").write_text(_HELPER_SOURCE)
        (self.work_dir / "tests").mkdir()
        (self.work_dir / "tests" / "test_foo.py").write_text(
            "def test_foo():\n    assert False\n"
        )

    def _traceback(self, frames: list[tuple[str, int, str]]) -> str:
        lines = ["Traceback (most recent call last):"]
        for path, line, func in frames:
            lines.append(f'  File "{path}", line {line}, in {func}')
            lines.append("    <body>")
        lines.append("AssertionError: boom")
        return "\n".join(lines)

    def test_extracts_helper_source(self):
        tb = self._traceback([
            ("tests/test_foo.py", 2, "test_foo"),
            ("cai_lib/foo.py", 2, "outer"),
        ])
        out = _extract_referenced_helpers(tb, self.work_dir)
        self.assertIn("`cai_lib/foo.py`", out)
        self.assertIn("`outer`", out)
        self.assertIn("return x + 1", out)

    def test_skips_test_paths(self):
        tb = self._traceback([("tests/test_foo.py", 2, "test_foo")])
        out = _extract_referenced_helpers(tb, self.work_dir)
        self.assertEqual(out, "")

    def test_skips_absolute_paths(self):
        tb = self._traceback([("/usr/lib/python3.12/unittest/case.py", 10, "run")])
        out = _extract_referenced_helpers(tb, self.work_dir)
        self.assertEqual(out, "")

    def test_skips_dotdot_paths(self):
        tb = self._traceback([("../etc/passwd", 1, "root")])
        out = _extract_referenced_helpers(tb, self.work_dir)
        self.assertEqual(out, "")

    def test_skips_site_packages(self):
        tb = self._traceback([(".venv/lib/site-packages/requests/api.py", 1, "get")])
        out = _extract_referenced_helpers(tb, self.work_dir)
        self.assertEqual(out, "")

    def test_dedupes_same_path_and_func(self):
        tb = self._traceback([
            ("cai_lib/foo.py", 2, "outer"),
            ("cai_lib/foo.py", 2, "outer"),
        ])
        out = _extract_referenced_helpers(tb, self.work_dir)
        # A single occurrence of the fenced block header.
        self.assertEqual(out.count("```python"), 1)

    def test_missing_file_is_ignored(self):
        tb = self._traceback([("cai_lib/does_not_exist.py", 1, "ghost")])
        out = _extract_referenced_helpers(tb, self.work_dir)
        self.assertEqual(out, "")

    def test_empty_output_when_no_frames(self):
        self.assertEqual(_extract_referenced_helpers("", self.work_dir), "")

    def test_respects_max_chars(self):
        tb = self._traceback([("cai_lib/foo.py", 2, "outer")])
        out = _extract_referenced_helpers(tb, self.work_dir, max_chars=40)
        self.assertTrue(out.endswith("... (truncated)"))
        self.assertLessEqual(len(out), 40 + len("\n\n... (truncated)"))


if __name__ == "__main__":
    unittest.main()
