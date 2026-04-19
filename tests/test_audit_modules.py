"""Tests for cai_lib.audit.modules — load_modules + coverage_check."""
import os
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cai_lib.audit.modules import (
    ModuleEntry,
    coverage_check,
    load_modules,
)


def _write(tmpdir: Path, body: str) -> Path:
    p = tmpdir / "modules.yaml"
    p.write_text(textwrap.dedent(body))
    return p


class TestLoadModules(unittest.TestCase):

    def test_valid_roundtrip(self):
        with tempfile.TemporaryDirectory() as td:
            p = _write(Path(td), """\
                modules:
                  - name: core
                    summary: Core.
                    doc: modules/core.md
                    globs: ["cai_lib/*.py"]
                  - name: tests
                    summary: Tests.
                    doc: modules/tests.md
                    globs: ["tests/*.py"]
            """)
            mods = load_modules(p)
            self.assertEqual(len(mods), 2)
            self.assertIsInstance(mods[0], ModuleEntry)
            self.assertEqual(mods[0].name, "core")
            self.assertEqual(mods[1].globs, ["tests/*.py"])

    def test_duplicate_name_raises(self):
        with tempfile.TemporaryDirectory() as td:
            p = _write(Path(td), """\
                modules:
                  - name: dup
                    summary: a
                    doc: d.md
                    globs: ["a/*.py"]
                  - name: dup
                    summary: b
                    doc: d.md
                    globs: ["b/*.py"]
            """)
            with self.assertRaises(ValueError) as ctx:
                load_modules(p)
            self.assertIn("duplicate", str(ctx.exception).lower())

    def test_missing_required_field_raises(self):
        with tempfile.TemporaryDirectory() as td:
            p = _write(Path(td), """\
                modules:
                  - name: no-globs
                    summary: missing globs
                    doc: d.md
            """)
            with self.assertRaises(ValueError) as ctx:
                load_modules(p)
            self.assertIn("globs", str(ctx.exception))

    def test_check_doc_exists_flag(self):
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            (tdp / "modules").mkdir()
            (tdp / "modules" / "core.md").write_text("# core")
            good = _write(tdp, """\
                modules:
                  - name: core
                    summary: ok
                    doc: modules/core.md
                    globs: ["cai_lib/*.py"]
            """)
            # default: no existence check
            load_modules(good)
            # with flag + existing doc: still passes
            load_modules(good, check_doc_exists=True)
            # with flag + missing doc: raises
            bad = tdp / "bad.yaml"
            bad.write_text(textwrap.dedent("""\
                modules:
                  - name: bad
                    summary: missing doc
                    doc: modules/does-not-exist.md
                    globs: ["x/*.py"]
            """))
            with self.assertRaises(ValueError):
                load_modules(bad, check_doc_exists=True)


class TestCoverageCheck(unittest.TestCase):

    def _mods(self, spec):
        return [
            ModuleEntry(name=n, summary="", doc="d.md", globs=g)
            for n, g in spec
        ]

    def test_full_coverage_returns_empty(self):
        mods = self._mods([("a", ["a/*.py"]), ("b", ["b/*.py"])])
        self.assertEqual(
            coverage_check(mods, ["a/x.py", "b/y.py"]),
            [],
        )

    def test_zero_match_flagged(self):
        mods = self._mods([("a", ["a/*.py"])])
        errs = coverage_check(mods, ["c/x.py"])
        self.assertEqual(len(errs), 1)
        self.assertIn("no module", errs[0])

    def test_multi_match_flagged(self):
        mods = self._mods([
            ("a", ["*.py"]),
            ("b", ["x.py"]),
        ])
        errs = coverage_check(mods, ["x.py"])
        self.assertEqual(len(errs), 1)
        self.assertIn("multiple", errs[0])


if __name__ == "__main__":
    unittest.main()
