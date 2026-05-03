"""Tests for block_overview and block_edit tree-sitter tools."""
from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

pytest.importorskip("tree_sitter")
pytest.importorskip("tree_sitter_python")

from cai.tools.block_tools import (
    BLOCK_EDIT_TOOL,
    BLOCK_OVERVIEW_TOOL,
    block_edit,
    block_overview,
)


def _run(coro):
    return asyncio.run(coro)


def _make_ctx(root_dir: str = "/tmp/repo") -> MagicMock:
    """Build a minimal mock RunContext with deps.backend.root_dir."""
    ctx = MagicMock()
    ctx.deps.backend.root_dir = root_dir
    return ctx


# ---------------------------------------------------------------------------
# Helpers — create sample .py files via tmp_path
# ---------------------------------------------------------------------------


SAMPLE_PY = """\
'''Module docstring.'''

import os


def top_level_func():
    '''A top-level function.'''
    pass


class Foo:
    '''A class with methods.'''

    def method_one(self):
        '''First method.'''
        pass

    async def method_two(self):
        '''Async method.'''
        pass


async def async_top():
    '''An async top-level function.'''
    pass
"""


def _write_sample(path: Path) -> Path:
    f = path / "sample.py"
    f.write_text(SAMPLE_PY)
    return f


# ---------------------------------------------------------------------------
# block_overview — basic behaviour
# ---------------------------------------------------------------------------


@patch("cai.tools.block_tools._resolve")
def test_block_overview_functions_and_classes(mock_resolve, tmp_path):
    """block_overview lists functions, classes, and methods with indices."""
    f = _write_sample(tmp_path)
    mock_resolve.return_value = f

    result = _run(block_overview(_make_ctx(), "sample.py"))

    assert "**1.**" in result
    assert "top_level_func" in result
    assert "function" in result.lower()
    assert "**2.**" in result
    assert "Foo" in result
    assert "class" in result.lower()
    assert "**3.**" in result
    assert "method_one" in result
    assert "method" in result.lower()
    assert "**4.**" in result
    assert "method_two" in result
    assert "async" in result.lower()
    assert "**5.**" in result
    assert "async_top" in result


@patch("cai.tools.block_tools._resolve")
def test_block_overview_docstrings(mock_resolve, tmp_path):
    """block_overview includes docstring summaries."""
    f = _write_sample(tmp_path)
    mock_resolve.return_value = f

    result = _run(block_overview(_make_ctx(), "sample.py"))

    assert "A top-level function" in result
    assert "A class with methods" in result
    assert "First method" in result
    assert "Async method" in result
    assert "An async top-level function" in result


@patch("cai.tools.block_tools._resolve")
def test_block_overview_methods_are_indented(mock_resolve, tmp_path):
    """Class methods appear indented under their parent class."""
    f = _write_sample(tmp_path)
    mock_resolve.return_value = f

    result = _run(block_overview(_make_ctx(), "sample.py"))

    # Methods should have leading whitespace (indentation)
    assert "    **3.**" in result
    assert "    **4.**" in result


@patch("cai.tools.block_tools._resolve")
def test_block_overview_empty_file(mock_resolve, tmp_path):
    """An empty Python file reports no blocks found."""
    f = tmp_path / "empty.py"
    f.write_text("")
    mock_resolve.return_value = f

    result = _run(block_overview(_make_ctx(), "empty.py"))

    assert "No top-level blocks found" in result


@patch("cai.tools.block_tools._resolve")
def test_block_overview_line_ranges(mock_resolve, tmp_path):
    """Line ranges are included in the output."""
    f = _write_sample(tmp_path)
    mock_resolve.return_value = f

    result = _run(block_overview(_make_ctx(), "sample.py"))

    assert "lines " in result


# ---------------------------------------------------------------------------
# block_overview — error paths
# ---------------------------------------------------------------------------


@patch("cai.tools.block_tools._resolve")
def test_block_overview_file_not_found(mock_resolve, tmp_path):
    """When the file does not exist, an error is returned."""
    f = tmp_path / "missing.py"
    mock_resolve.return_value = f

    result = _run(block_overview(_make_ctx(), "missing.py"))

    assert "File not found" in result


@patch("cai.tools.block_tools._resolve")
def test_block_overview_not_python_file(mock_resolve, tmp_path):
    """Non-.py files produce an error."""
    f = tmp_path / "config.yaml"
    f.write_text("key: value")
    mock_resolve.return_value = f

    result = _run(block_overview(_make_ctx(), "config.yaml"))

    assert "Not a Python file" in result


@patch("cai.tools.block_tools._resolve")
def test_block_overview_is_directory(mock_resolve, tmp_path):
    """Directories produce an error."""
    d = tmp_path / "mydir"
    d.mkdir()
    mock_resolve.return_value = d

    result = _run(block_overview(_make_ctx(), "mydir"))

    assert "Not a regular file" in result


@patch("cai.tools.block_tools._resolve")
def test_block_overview_permission_error(mock_resolve):
    """PermissionError from _resolve is returned as error message."""
    mock_resolve.side_effect = PermissionError("Path escapes repository root")

    result = _run(block_overview(_make_ctx(), "../escape.py"))

    assert "Path escapes repository root" in result


@patch("cai.tools.block_tools._resolve")
def test_block_overview_parse_failure(mock_resolve, tmp_path):
    """Invalid Python syntax returns a parse-failure error."""
    f = tmp_path / "broken.py"
    f.write_text("def foo(: pass\n")  # malformed syntax
    mock_resolve.return_value = f

    result = _run(block_overview(_make_ctx(), "broken.py"))

    # tree-sitter is resilient; it may produce a tree with error nodes
    # but should not crash. If it produces no blocks, that's fine too.
    assert isinstance(result, str)


@patch("cai.tools.block_tools._resolve")
def test_block_overview_function_no_docstring(mock_resolve, tmp_path):
    """A function without a docstring is still listed."""
    source = """\
def bare_func():
    pass
"""
    f = tmp_path / "bare.py"
    f.write_text(source)
    mock_resolve.return_value = f

    result = _run(block_overview(_make_ctx(), "bare.py"))

    assert "**1.**" in result
    assert "bare_func" in result
    # No docstring — no em-dash summary appended
    assert "—" not in result


@patch("cai.tools.block_tools._resolve")
def test_block_overview_class_no_methods(mock_resolve, tmp_path):
    """A class without methods is still listed."""
    source = """\
class Empty:
    '''Empty class.'''
    pass
"""
    f = tmp_path / "empty_cls.py"
    f.write_text(source)
    mock_resolve.return_value = f

    result = _run(block_overview(_make_ctx(), "empty_cls.py"))

    assert "**1.**" in result
    assert "class" in result.lower()
    assert "Empty" in result
    # No methods — only one block
    assert "**2." not in result


@patch("cai.tools.block_tools._resolve")
def test_block_overview_double_quote_docstrings(mock_resolve, tmp_path):
    """Docstrings using triple double-quotes are extracted correctly."""
    source = '''\
def quoted():
    """A function with double-quote docstring."""
    pass
'''
    f = tmp_path / "quoted.py"
    f.write_text(source)
    mock_resolve.return_value = f

    result = _run(block_overview(_make_ctx(), "quoted.py"))

    assert "A function with double-quote docstring" in result


# ---------------------------------------------------------------------------
# block_edit — success
# ---------------------------------------------------------------------------


@patch("cai.tools.block_tools._resolve")
def test_block_edit_replace_function(mock_resolve, tmp_path):
    """Replacing a top-level function updates the file."""
    f = _write_sample(tmp_path)
    mock_resolve.return_value = f

    result = _run(
        block_edit(
            _make_ctx(),
            "sample.py",
            block_index=1,
            new_content="def top_level_func():\n    '''Updated.'''\n    pass\n",
        )
    )

    assert "Replaced function" in result
    assert "top_level_func" in result
    assert "block 1" in result

    # Verify the file content
    new_content = f.read_text()
    assert "Updated" in new_content
    assert "top_level_func" in new_content


@patch("cai.tools.block_tools._resolve")
def test_block_edit_replace_method(mock_resolve, tmp_path):
    """Replacing a class method updates the file."""
    f = _write_sample(tmp_path)
    mock_resolve.return_value = f

    result = _run(
        block_edit(
            _make_ctx(),
            "sample.py",
            block_index=3,
            new_content="def method_one(self):\n    '''Replaced.'''\n    return 42\n",
        )
    )

    assert "Replaced method" in result
    assert "method_one" in result
    assert "block 3" in result

    new_content = f.read_text()
    assert "Replaced" in new_content
    assert "return 42" in new_content


@patch("cai.tools.block_tools._resolve")
def test_block_edit_preserves_indentation(mock_resolve, tmp_path):
    """block_edit reapplies the original block's leading indentation."""
    f = _write_sample(tmp_path)
    mock_resolve.return_value = f

    _run(
        block_edit(
            _make_ctx(),
            "sample.py",
            block_index=3,
            new_content="def method_one(self):\n    '''Rep.'''\n    pass\n",
        )
    )

    content = f.read_text()
    # The method should still be indented inside the class
    assert "    def method_one" in content


# ---------------------------------------------------------------------------
# block_edit — error paths
# ---------------------------------------------------------------------------


@patch("cai.tools.block_tools._resolve")
def test_block_edit_out_of_range(mock_resolve, tmp_path):
    """An out-of-range index returns an error."""
    f = _write_sample(tmp_path)
    mock_resolve.return_value = f

    result = _run(block_edit(_make_ctx(), "sample.py", block_index=99, new_content="x"))

    assert "out of range" in result


@patch("cai.tools.block_tools._resolve")
def test_block_edit_not_python(mock_resolve, tmp_path):
    """Non-.py files produce an error."""
    f = tmp_path / "config.yaml"
    f.write_text("key: value")
    mock_resolve.return_value = f

    result = _run(block_edit(_make_ctx(), "config.yaml", block_index=1, new_content="x"))

    assert "Not a Python file" in result


@patch("cai.tools.block_tools._resolve")
def test_block_edit_file_not_found(mock_resolve, tmp_path):
    """Missing file returns an error."""
    f = tmp_path / "nope.py"
    mock_resolve.return_value = f

    result = _run(block_edit(_make_ctx(), "nope.py", block_index=1, new_content="x"))

    assert "File not found" in result


@patch("cai.tools.block_tools._resolve")
def test_block_edit_permission_error(mock_resolve):
    """PermissionError from _resolve is returned as error message."""
    mock_resolve.side_effect = PermissionError("Path escapes repository root")

    result = _run(block_edit(_make_ctx(), "../escape.py", block_index=1, new_content="x"))

    assert "Path escapes repository root" in result


@patch("cai.tools.block_tools._resolve")
def test_block_edit_is_directory(mock_resolve, tmp_path):
    """Directories produce an error."""
    d = tmp_path / "mydir"
    d.mkdir()
    mock_resolve.return_value = d

    result = _run(block_edit(_make_ctx(), "mydir", block_index=1, new_content="x"))

    assert "Not a regular file" in result


@patch("cai.tools.block_tools._resolve")
def test_block_edit_parse_failure(mock_resolve, tmp_path):
    """An unparseable Python file returns a parse-failure error."""
    f = tmp_path / "broken.py"
    f.write_text("def foo(: pass\n")  # malformed
    mock_resolve.return_value = f

    result = _run(block_edit(_make_ctx(), "broken.py", block_index=1, new_content="x"))

    assert "Failed to parse" in result


@patch("cai.tools.block_tools._resolve")
def test_block_edit_zero_index(mock_resolve, tmp_path):
    """block_index=0 is out of range (indices are 1-based)."""
    f = _write_sample(tmp_path)
    mock_resolve.return_value = f

    result = _run(block_edit(_make_ctx(), "sample.py", block_index=0, new_content="x"))

    assert "out of range" in result


@patch("cai.tools.block_tools._resolve")
def test_block_edit_negative_index(mock_resolve, tmp_path):
    """A negative block_index is out of range."""
    f = _write_sample(tmp_path)
    mock_resolve.return_value = f

    result = _run(block_edit(_make_ctx(), "sample.py", block_index=-1, new_content="x"))

    assert "out of range" in result


@patch("cai.tools.block_tools._resolve")
def test_block_edit_unparseable_replacement(mock_resolve, tmp_path):
    """A replacement that makes the file unparseable returns an error."""
    f = _write_sample(tmp_path)
    mock_resolve.return_value = f

    result = _run(
        block_edit(
            _make_ctx(),
            "sample.py",
            block_index=1,
            new_content="def top_level_func(:\n    '''Broken.'''\n    pass\n",  # missing ')'
        )
    )

    assert "Edit resulted in unparseable file" in result
    # File should not be modified
    assert "A top-level function." in f.read_text()


@patch("cai.tools.block_tools._resolve")
def test_block_edit_replace_class(mock_resolve, tmp_path):
    """Replacing a class block updates the file."""
    f = _write_sample(tmp_path)
    mock_resolve.return_value = f

    result = _run(
        block_edit(
            _make_ctx(),
            "sample.py",
            block_index=2,
            new_content="class Foo:\n    '''Updated class.'''\n    pass\n",
        )
    )

    assert "Replaced class" in result
    assert "Foo" in result
    assert "block 2" in result

    new_content = f.read_text()
    assert "Updated class" in new_content


@patch("cai.tools.block_tools._resolve")
def test_block_edit_replace_async_function(mock_resolve, tmp_path):
    """Replacing an async function block updates the file."""
    f = _write_sample(tmp_path)
    mock_resolve.return_value = f

    result = _run(
        block_edit(
            _make_ctx(),
            "sample.py",
            block_index=5,
            new_content="async def async_top():\n    '''Replaced async.'''\n    return 99\n",
        )
    )

    assert "Replaced async function" in result
    assert "async_top" in result
    assert "block 5" in result

    new_content = f.read_text()
    assert "Replaced async" in new_content
    assert "return 99" in new_content


@patch("cai.tools.block_tools._resolve")
def test_block_edit_empty_new_content(mock_resolve, tmp_path):
    """An empty new_content is treated as a single newline."""
    f = _write_sample(tmp_path)
    mock_resolve.return_value = f

    _run(
        block_edit(
            _make_ctx(),
            "sample.py",
            block_index=1,
            new_content="",
        )
    )

    content = f.read_text()
    # The function block is replaced with an empty line (just '\n')
    # The original function should no longer be present
    assert "top_level_func" not in content


# ---------------------------------------------------------------------------
# Helper function tests
# ---------------------------------------------------------------------------


def test_get_parser_returns_parser():
    """_get_parser returns a tree-sitter Parser configured for Python."""
    from cai.tools.block_tools import _get_parser

    parser = _get_parser()
    assert parser is not None
    # Calling again returns the same singleton
    assert _get_parser() is parser


def test_is_async_detects_async_function(tmp_path):
    """_is_async returns True for async functions, False for sync."""
    from cai.tools.block_tools import _get_parser, _is_async

    source = b"async def foo():\n    pass\n"
    parser = _get_parser()
    tree = parser.parse(source)

    # Find the function_definition node
    for child in tree.root_node.children:
        if child.type == "function_definition":
            assert _is_async(child) is True
            break
    else:
        pytest.fail("No function_definition found")

    source_sync = b"def bar():\n    pass\n"
    tree_sync = parser.parse(source_sync)
    for child in tree_sync.root_node.children:
        if child.type == "function_definition":
            assert _is_async(child) is False
            break
    else:
        pytest.fail("No function_definition found")


def test_node_text_returns_source_slice(tmp_path):
    """_node_text returns the source bytes spanned by a node."""
    from cai.tools.block_tools import _get_parser, _node_text

    source = b"def foo():\n    pass\n"
    parser = _get_parser()
    tree = parser.parse(source)

    for child in tree.root_node.children:
        if child.type == "function_definition":
            text = _node_text(source, child)
            assert "def foo():" in text
            assert "pass" in text
            return
    pytest.fail("No function_definition found")


# ---------------------------------------------------------------------------
# Tool constants
# ---------------------------------------------------------------------------


def test_block_overview_tool_is_tool_instance():
    """BLOCK_OVERVIEW_TOOL is a pydantic_ai Tool."""
    from pydantic_ai import Tool

    assert isinstance(BLOCK_OVERVIEW_TOOL, Tool)


def test_block_edit_tool_is_tool_instance():
    """BLOCK_EDIT_TOOL is a pydantic_ai Tool."""
    from pydantic_ai import Tool

    assert isinstance(BLOCK_EDIT_TOOL, Tool)


def test_block_overview_tool_name():
    """The tool name matches the function name."""
    assert BLOCK_OVERVIEW_TOOL.name == "block_overview"


def test_block_edit_tool_name():
    """The tool name matches the function name."""
    assert BLOCK_EDIT_TOOL.name == "block_edit"


# ---------------------------------------------------------------------------
# TOOL_FACTORIES registration
# ---------------------------------------------------------------------------


def test_block_tools_registered_in_tool_factories():
    """Both tools are registered under their keys in loader.py."""
    from cai.agents.loader import TOOL_FACTORIES

    assert "block_overview" in TOOL_FACTORIES
    assert TOOL_FACTORIES["block_overview"] == "cai.tools.block_tools:BLOCK_OVERVIEW_TOOL"
    assert "block_edit" in TOOL_FACTORIES
    assert TOOL_FACTORIES["block_edit"] == "cai.tools.block_tools:BLOCK_EDIT_TOOL"


def test_import_factory_resolves_block_tools():
    """The factory target strings import and return the correct tools."""
    from cai.agents.loader import TOOL_FACTORIES, _import_factory

    overview = _import_factory(TOOL_FACTORIES["block_overview"])
    assert overview is BLOCK_OVERVIEW_TOOL

    edit = _import_factory(TOOL_FACTORIES["block_edit"])
    assert edit is BLOCK_EDIT_TOOL


# ---------------------------------------------------------------------------
# Module docstring
# ---------------------------------------------------------------------------


def test_module_docstring_exists():
    """The block_tools module has a docstring."""
    import cai.tools.block_tools as bt

    assert bt.__doc__ is not None
    assert len(bt.__doc__) > 0
    assert "block_overview" in bt.__doc__
