"""Tests for conflict_list / conflict_resolve tools."""
from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from cai.agents.conflict_tools import _load_file_conflicts, _parse_conflicts, conflict_cleanup, conflict_list, conflict_resolve


# ---------------------------------------------------------------------------
# _parse_conflicts
# ---------------------------------------------------------------------------

SIMPLE = """\
before
<<<<<<< HEAD
ours line
=======
theirs line
>>>>>>> abc123
after
"""

MULTI = """\
<<<<<<< HEAD
a
=======
b
>>>>>>> sha1
middle
<<<<<<< HEAD
c
d
=======
e
>>>>>>> sha2
end
"""


def test_parse_single_block():
    lines = SIMPLE.splitlines(keepends=True)
    blocks = _parse_conflicts(lines)
    assert len(blocks) == 1
    b = blocks[0]
    assert b["index"] == 0
    assert b["ours"] == "ours line\n"
    assert b["theirs"] == "theirs line\n"


def test_parse_multiple_blocks():
    lines = MULTI.splitlines(keepends=True)
    blocks = _parse_conflicts(lines)
    assert len(blocks) == 2
    assert blocks[0]["ours"] == "a\n"
    assert blocks[0]["theirs"] == "b\n"
    assert blocks[1]["ours"] == "c\nd\n"
    assert blocks[1]["theirs"] == "e\n"


def test_parse_no_conflicts():
    lines = "clean file\n".splitlines(keepends=True)
    assert _parse_conflicts(lines) == []


# ---------------------------------------------------------------------------
# _load_file_conflicts
# ---------------------------------------------------------------------------


def test_load_file_conflicts_success(tmp_path):
    (tmp_path / "a.py").write_text(SIMPLE)
    ctx = _make_ctx(tmp_path)
    err, full, lines, blocks = _load_file_conflicts(ctx, "a.py")
    assert err is None
    assert full == (tmp_path / "a.py").resolve()
    assert isinstance(lines, list)
    assert len(blocks) == 1


def test_load_file_conflicts_escape(tmp_path):
    ctx = _make_ctx(tmp_path)
    err, full, lines, blocks = _load_file_conflicts(ctx, "../outside.py")
    assert "Permission denied" in err
    assert full is None
    assert lines is None
    assert blocks is None


def test_load_file_conflicts_missing(tmp_path):
    ctx = _make_ctx(tmp_path)
    err, full, lines, blocks = _load_file_conflicts(ctx, "nope.py")
    assert "not found" in err.lower()
    assert full is None
    assert lines is None
    assert blocks is None


def test_load_file_conflicts_no_markers(tmp_path):
    (tmp_path / "clean.py").write_text("x = 1\n")
    ctx = _make_ctx(tmp_path)
    err, full, lines, blocks = _load_file_conflicts(ctx, "clean.py")
    assert "No conflict" in err
    assert full is None
    assert lines is None
    assert blocks is None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_ctx(tmp_path: Path) -> MagicMock:
    ctx = MagicMock()
    ctx.deps.backend.root_dir = str(tmp_path)
    return ctx


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# conflict_list
# ---------------------------------------------------------------------------

def test_conflict_list_shows_blocks(tmp_path):
    (tmp_path / "a.py").write_text(SIMPLE)
    out = run(conflict_list(_make_ctx(tmp_path), "a.py"))
    assert "1 conflict" in out
    assert "ours line" in out
    assert "theirs line" in out


def test_conflict_list_no_conflicts(tmp_path):
    (tmp_path / "clean.py").write_text("x = 1\n")
    out = run(conflict_list(_make_ctx(tmp_path), "clean.py"))
    assert "No conflict" in out


def test_conflict_list_missing_file(tmp_path):
    out = run(conflict_list(_make_ctx(tmp_path), "missing.py"))
    assert "not found" in out.lower()


# ---------------------------------------------------------------------------
# conflict_resolve — ours / theirs / custom
# ---------------------------------------------------------------------------

def test_resolve_ours(tmp_path):
    f = tmp_path / "a.py"
    f.write_text(SIMPLE)
    out = run(conflict_resolve(_make_ctx(tmp_path), "a.py", 0, "ours"))
    assert "ours" in out
    result = f.read_text()
    assert "ours line" in result
    assert "theirs line" not in result
    assert "<<<<<<<" not in result


def test_resolve_theirs(tmp_path):
    f = tmp_path / "a.py"
    f.write_text(SIMPLE)
    out = run(conflict_resolve(_make_ctx(tmp_path), "a.py", 0, "theirs"))
    assert "theirs" in out
    result = f.read_text()
    assert "theirs line" in result
    assert "ours line" not in result
    assert "<<<<<<<" not in result


def test_resolve_custom(tmp_path):
    f = tmp_path / "a.py"
    f.write_text(SIMPLE)
    out = run(conflict_resolve(_make_ctx(tmp_path), "a.py", 0, "merged line\n"))
    assert "custom" in out
    result = f.read_text()
    assert "merged line" in result
    assert "<<<<<<<" not in result
    assert "before" in result
    assert "after" in result


def test_resolve_preserves_surrounding_content(tmp_path):
    f = tmp_path / "a.py"
    f.write_text(SIMPLE)
    run(conflict_resolve(_make_ctx(tmp_path), "a.py", 0, "ours"))
    result = f.read_text()
    assert result.startswith("before\n")
    assert result.endswith("after\n")


def test_resolve_multi_second_block(tmp_path):
    f = tmp_path / "a.py"
    f.write_text(MULTI)
    run(conflict_resolve(_make_ctx(tmp_path), "a.py", 1, "theirs"))
    result = f.read_text()
    assert "<<<<<<<" in result       # first block still unresolved
    assert "e\n" in result           # theirs from block 1
    assert "c\n" not in result       # ours from block 1 gone


def test_resolve_reports_remaining_conflicts(tmp_path):
    f = tmp_path / "a.py"
    f.write_text(MULTI)
    out = run(conflict_resolve(_make_ctx(tmp_path), "a.py", 0, "ours"))
    assert "1 conflict(s) remaining" in out


def test_resolve_reports_clean_when_done(tmp_path):
    f = tmp_path / "a.py"
    f.write_text(SIMPLE)
    out = run(conflict_resolve(_make_ctx(tmp_path), "a.py", 0, "ours"))
    assert "clean" in out


def test_resolve_out_of_range(tmp_path):
    f = tmp_path / "a.py"
    f.write_text(SIMPLE)
    out = run(conflict_resolve(_make_ctx(tmp_path), "a.py", 5, "ours"))
    assert "out of range" in out


# ---------------------------------------------------------------------------
# conflict_cleanup
# ---------------------------------------------------------------------------

DEBRIS_FILE = """\
def foo():
    x = 1
    matched_prior_args = prior_args
    y = 2
    return x + y
"""


def test_conflict_cleanup_removes_single_range(tmp_path):
    f = tmp_path / "a.py"
    f.write_text(DEBRIS_FILE)
    out = run(conflict_cleanup(_make_ctx(tmp_path), "a.py", [(3, 3)]))
    assert "Removed 1 line" in out
    result = f.read_text()
    assert "matched_prior_args" not in result
    assert "def foo():" in result
    assert "y = 2" in result


def test_conflict_cleanup_removes_multiple_ranges(tmp_path):
    f = tmp_path / "a.py"
    f.write_text(DEBRIS_FILE)
    out = run(conflict_cleanup(_make_ctx(tmp_path), "a.py", [(3, 3), (5, 5)]))
    assert "Removed 2 line" in out
    result = f.read_text()
    assert "matched_prior_args" not in result
    assert "return x + y" not in result
    assert "def foo():" in result
    assert "x = 1" in result
    assert "y = 2" in result


def test_conflict_cleanup_preserves_surrounding_content(tmp_path):
    f = tmp_path / "a.py"
    original = "line1\nline2\nline3\nline4\nline5\n"
    f.write_text(original)
    run(conflict_cleanup(_make_ctx(tmp_path), "a.py", [(2, 3)]))
    result = f.read_text()
    assert result == "line1\nline4\nline5\n"


def test_conflict_cleanup_reports_lines_removed(tmp_path):
    f = tmp_path / "a.py"
    f.write_text(DEBRIS_FILE)
    out = run(conflict_cleanup(_make_ctx(tmp_path), "a.py", [(1, 2), (4, 5)]))
    assert "Removed 4 line" in out
    assert "2 range" in out
    assert "a.py" in out


def test_conflict_cleanup_out_of_range_is_error(tmp_path):
    f = tmp_path / "a.py"
    f.write_text(DEBRIS_FILE)
    out = run(conflict_cleanup(_make_ctx(tmp_path), "a.py", [(10, 20)]))
    assert "Invalid range" in out
    # File must not be modified.
    assert f.read_text() == DEBRIS_FILE


def test_conflict_cleanup_path_escape_is_denied(tmp_path):
    ctx = _make_ctx(tmp_path)
    out = run(conflict_cleanup(ctx, "../outside.py", [(1, 1)]))
    assert "Permission denied" in out


def test_conflict_cleanup_empty_ranges_is_noop(tmp_path):
    """An empty remove_lines list must not modify the file and should report 0."""
    f = tmp_path / "a.py"
    f.write_text(DEBRIS_FILE)
    out = run(conflict_cleanup(_make_ctx(tmp_path), "a.py", []))
    assert "Removed 0 line" in out
    assert "0 range" in out
    assert f.read_text() == DEBRIS_FILE


def test_conflict_cleanup_reverse_processing(tmp_path):
    """Ranges are processed in reverse order so earlier line numbers stay valid.

    File (5 lines)::

      1: a
      2: b
      3: c
      4: d
      5: e

    Remove ranges (4, 5) then (2, 3). If processed in order, removing (2,3)
    first would shift lines 4-5 up and break the second range.  Reverse
    processing deletes lines 4-5 first, then lines 2-3 in the *original*
    numbering — both ranges still match their intended lines.
    """
    f = tmp_path / "a.py"
    f.write_text("a\nb\nc\nd\ne\n")
    out = run(conflict_cleanup(_make_ctx(tmp_path), "a.py", [(2, 3), (4, 5)]))
    assert "Removed 4 line" in out
    assert "2 range" in out
    result = f.read_text()
    assert result == "a\n"


def test_conflict_cleanup_remove_entire_file(tmp_path):
    """Removing every line in the file produces an empty file."""
    f = tmp_path / "a.py"
    f.write_text("line1\nline2\nline3\n")
    out = run(conflict_cleanup(_make_ctx(tmp_path), "a.py", [(1, 3)]))
    assert "Removed 3 line" in out
    assert f.read_text() == ""


def test_conflict_cleanup_remove_first_line(tmp_path):
    """Removing the very first line (range 1,1) is a valid boundary case."""
    f = tmp_path / "a.py"
    f.write_text("first\nsecond\nthird\n")
    out = run(conflict_cleanup(_make_ctx(tmp_path), "a.py", [(1, 1)]))
    assert "Removed 1 line" in out
    assert "first" not in f.read_text()
    assert f.read_text() == "second\nthird\n"


def test_conflict_cleanup_remove_last_line(tmp_path):
    """Removing the very last line (range N,N) is a valid boundary case."""
    f = tmp_path / "a.py"
    f.write_text("first\nsecond\nthird\n")
    out = run(conflict_cleanup(_make_ctx(tmp_path), "a.py", [(3, 3)]))
    assert "Removed 1 line" in out
    assert "third" not in f.read_text()
    assert f.read_text() == "first\nsecond\n"


def test_conflict_cleanup_non_existent_file(tmp_path):
    """A non-existent file must produce a 'not found' error."""
    ctx = _make_ctx(tmp_path)
    out = run(conflict_cleanup(ctx, "missing.py", [(1, 1)]))
    assert "not found" in out.lower()