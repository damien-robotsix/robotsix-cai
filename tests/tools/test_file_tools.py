"""Tests for the file_info tool (line count, byte size, mtime)."""
from __future__ import annotations

import asyncio
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from cai.tools.file_tools import FILE_INFO_TOOL, file_info


def _run(coro):
    return asyncio.run(coro)


def _make_ctx(root_dir: str = "/tmp/repo") -> MagicMock:
    """Build a minimal mock RunContext with deps.backend.root_dir."""
    ctx = MagicMock()
    ctx.deps.backend.root_dir = root_dir
    return ctx


# ---------------------------------------------------------------------------
# file_info — basic behaviour
# ---------------------------------------------------------------------------


@patch("cai.tools.file_tools._resolve")
def test_file_info_returns_line_count_and_size(mock_resolve, tmp_path):
    """file_info returns line count, byte size, and mtime for a regular file."""
    f = tmp_path / "hello.py"
    f.write_text("line1\nline2\nline3\n")
    mock_resolve.return_value = f

    result = _run(file_info(_make_ctx(), "hello.py"))

    assert "hello.py" in result
    assert "3 lines" in result
    assert str(f.stat().st_size) in result
    assert "modified " in result
    mock_resolve.assert_called_once()


@patch("cai.tools.file_tools._resolve")
def test_file_info_empty_file(mock_resolve, tmp_path):
    """An empty file reports 0 lines."""
    f = tmp_path / "empty.txt"
    f.write_text("")
    mock_resolve.return_value = f

    result = _run(file_info(_make_ctx(), "empty.txt"))

    assert "0 lines" in result


@patch("cai.tools.file_tools._resolve")
def test_file_info_single_line_no_trailing_newline(mock_resolve, tmp_path):
    """A file with one line and no trailing newline reports 1 line."""
    f = tmp_path / "one_liner.py"
    f.write_text("print('hello')")
    mock_resolve.return_value = f

    result = _run(file_info(_make_ctx(), "one_liner.py"))

    assert "1 lines" in result or "1 line" in result


@patch("cai.tools.file_tools._resolve")
def test_file_info_multi_line_no_trailing_newline(mock_resolve, tmp_path):
    """Lines are counted correctly when the file lacks a trailing newline."""
    f = tmp_path / "trailing.txt"
    f.write_text("a\nb\nc")
    mock_resolve.return_value = f

    result = _run(file_info(_make_ctx(), "trailing.txt"))

    assert "3 lines" in result


@patch("cai.tools.file_tools._resolve")
def test_file_info_binary_content(mock_resolve, tmp_path):
    """Binary content with no newline characters reports 1 line (single chunk)."""
    f = tmp_path / "binary.bin"
    f.write_bytes(b"\x00\x01\x02\xff")
    mock_resolve.return_value = f

    result = _run(file_info(_make_ctx(), "binary.bin"))

    assert "1 lines" in result or "1 line" in result
    assert "4 bytes" in result


@patch("cai.tools.file_tools._resolve")
def test_file_info_with_blank_lines(mock_resolve, tmp_path):
    """Blank lines at the end are counted (trailing newline = final empty line)."""
    f = tmp_path / "blanks.py"
    f.write_text("a\n\n\nb\n\n")
    mock_resolve.return_value = f

    result = _run(file_info(_make_ctx(), "blanks.py"))

    assert "5 lines" in result


# ---------------------------------------------------------------------------
# file_info — error paths
# ---------------------------------------------------------------------------


@patch("cai.tools.file_tools._resolve")
def test_file_info_file_not_found(mock_resolve, tmp_path):
    """When the resolved path does not exist, file_info returns an error string."""
    f = tmp_path / "missing.py"  # path exists but file doesn't
    mock_resolve.return_value = f

    result = _run(file_info(_make_ctx(), "missing.py"))

    assert "File not found" in result
    assert "missing.py" in result


@patch("cai.tools.file_tools._resolve")
def test_file_info_is_directory(mock_resolve, tmp_path):
    """When the path resolves to a directory, file_info returns an error string."""
    d = tmp_path / "somedir"
    d.mkdir()
    mock_resolve.return_value = d

    result = _run(file_info(_make_ctx(), "somedir"))

    assert "Not a regular file" in result
    assert "somedir" in result


@patch("cai.tools.file_tools._resolve")
def test_file_info_permission_error(mock_resolve):
    """When _resolve raises PermissionError, the error message is returned."""
    mock_resolve.side_effect = PermissionError("Path escapes repository root")

    result = _run(file_info(_make_ctx(), "../escape.txt"))

    assert "Path escapes repository root" in result
    assert "../escape.txt" not in result  # The PermissionError message is returned directly


# ---------------------------------------------------------------------------
# file_info — byte size precision
# ---------------------------------------------------------------------------


@patch("cai.tools.file_tools._resolve")
def test_file_info_byte_size_accuracy(mock_resolve, tmp_path):
    """The reported byte size matches the actual file size on disk."""
    content = "x" * 1024 * 10  # 10 KiB
    f = tmp_path / "ten_kb.txt"
    f.write_text(content)
    mock_resolve.return_value = f

    result = _run(file_info(_make_ctx(), "ten_kb.txt"))

    assert "10240 bytes" in result or str(len(content)) in result


# ---------------------------------------------------------------------------
# FILE_INFO_TOOL constant
# ---------------------------------------------------------------------------


def test_file_info_tool_is_tool_instance():
    """FILE_INFO_TOOL is a pydantic_ai Tool."""
    from pydantic_ai import Tool

    assert isinstance(FILE_INFO_TOOL, Tool)


def test_file_info_tool_name():
    """The tool name matches the function name."""
    assert FILE_INFO_TOOL.name == "file_info"


# ---------------------------------------------------------------------------
# TOOL_FACTORIES registration
# ---------------------------------------------------------------------------


def test_file_info_registered_in_tool_factories():
    """file_info is registered under its key in loader.py."""
    from cai.agents.loader import TOOL_FACTORIES

    assert "file_info" in TOOL_FACTORIES
    assert TOOL_FACTORIES["file_info"] == "cai.tools.file_tools:FILE_INFO_TOOL"


def test_import_factory_resolves_file_info_tool():
    """The factory target string imports and returns the correct tool."""
    from cai.agents.loader import TOOL_FACTORIES, _import_factory

    tool = _import_factory(TOOL_FACTORIES["file_info"])
    assert tool is FILE_INFO_TOOL


# ---------------------------------------------------------------------------
# Module docstring
# ---------------------------------------------------------------------------


def test_module_docstring_exists():
    """The file_tools module has a docstring describing the tool's purpose."""
    import cai.tools.file_tools as ft

    assert ft.__doc__ is not None
    assert len(ft.__doc__) > 0
    assert "file_info" in ft.__doc__
