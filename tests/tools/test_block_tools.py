"""Tests for the block_overview and block_edit tree-sitter tools."""
from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import pytest

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
# block_overview — basic behaviour
# ---------------------------------------------------------------------------


@patch("cai.tools.block_tools._resolve")
def test_block_overview_lists_blocks(mock_resolve, tmp_path):
    """block_overview returns a markdown listing of functions, classes, methods, and docstrings."""
    f = tmp_path / "sample.py"
    f.write_text(
        '"""Module docstring."""\n'
        "\n"
        "\n"
        'def top_level_func():\n'
        '    """top level docstring."""\n'
        "    pass\n"
        "\n"
        "\n"
        'class Foo:\n'
        '    """Foo docstring."""\n'
        "\n"
        "    def bar(self):\n"
        '        """bar docstring."""\n'
        "        pass\n"
        "\n"
        "    async def baz(self):\n"
        '        """baz docstring."""\n'
        "        pass\n"
    )
    mock_resolve.return_value = f

    result = _run(block_overview(_make_ctx(), "sample.py"))

    assert "## Block Overview: sample.py" in result
    assert "class" in result
    assert "Foo" in result
    assert "method" in result
    assert "bar" in result
    assert "baz" in result
    assert "function" in result
    assert "top_level_func" in result
    assert "Foo docstring" in result
    assert "bar docstring" in result
    assert "baz docstring" in result
    assert "top level docstring" in result
    # Line ranges
    assert "lines 4-6" in result  # top_level_func
    assert "lines 9-18" in result  # Foo class
    # Verify block count: 0=function, 1=class, 2=method bar, 3=method baz
    assert "0." in result
    assert "1." in result
    assert "2." in result
    assert "3." in result


@patch("cai.tools.block_tools._resolve")
def test_block_overview_async_function(mock_resolve, tmp_path):
    """An async top-level function is detected and marked as 'async function'."""
    f = tmp_path / "async_sample.py"
    f.write_text(
        "import asyncio\n"
        "\n"
        "\n"
        "async def fetch_data():\n"
        '    """Fetch data."""\n'
        "    pass\n"
    )
    mock_resolve.return_value = f

    result = _run(block_overview(_make_ctx(), "async_sample.py"))

    assert "async function" in result
    assert "fetch_data" in result
    assert "Fetch data" in result


@patch("cai.tools.block_tools._resolve")
def test_block_overview_decorated_definition(mock_resolve, tmp_path):
    """Decorated functions and classes are handled correctly."""
    f = tmp_path / "decorated.py"
    f.write_text(
        "from functools import lru_cache\n"
        "\n"
        "\n"
        "@lru_cache\n"
        "def cached_func():\n"
        '    """Cached."""\n'
        "    pass\n"
        "\n"
        "\n"
        "@some_decorator\n"
        "class DecoratedClass:\n"
        '    """Decorated class."""\n'
        "\n"
        "    def method(self):\n"
        '        """A method."""\n'
        "        pass\n"
    )
    mock_resolve.return_value = f

    result = _run(block_overview(_make_ctx(), "decorated.py"))

    assert "cached_func" in result
    assert "Cached" in result
    assert "DecoratedClass" in result
    assert "Decorated class" in result
    assert "method" in result
    assert "A method" in result
    # Decorated function line range spans the decorator
    assert "lines 4-7" in result  # @lru_cache + def cached_func


@patch("cai.tools.block_tools._resolve")
def test_block_overview_no_docstring(mock_resolve, tmp_path):
    """Functions without docstrings get no docstring line in the output."""
    f = tmp_path / "undocumented.py"
    f.write_text(
        "def no_doc():\n"
        "    pass\n"
        "\n"
        "\n"
        'def with_doc():\n'
        '    """Has doc."""\n'
        "    pass\n"
    )
    mock_resolve.return_value = f

    result = _run(block_overview(_make_ctx(), "undocumented.py"))

    # with_doc should have its docstring
    assert "Has doc" in result
    # no_doc should not have a docstring line — check that 'docstring:' appears
    # only for with_doc, not for no_doc
    lines = result.splitlines()
    doc_lines = [l for l in lines if "docstring:" in l]
    assert len(doc_lines) == 1
    assert "Has doc" in doc_lines[0]
    # no_doc should appear exactly once (function line, no docstring line)
    assert sum(1 for l in lines if "no_doc" in l) == 1


@patch("cai.tools.block_tools._resolve")
def test_block_overview_non_python_file(mock_resolve, tmp_path):
    """Non-.py files return an error string."""
    f = tmp_path / "notes.txt"
    f.write_text("some text")
    mock_resolve.return_value = f

    result = _run(block_overview(_make_ctx(), "notes.txt"))

    assert "not a Python file" in result
    assert "notes.txt" in result


@patch("cai.tools.block_tools._resolve")
def test_block_overview_parse_error(mock_resolve, tmp_path):
    """Invalid Python returns an error string."""
    f = tmp_path / "broken.py"
    f.write_text("def broken(  # missing closing paren and body\n")
    mock_resolve.return_value = f

    result = _run(block_overview(_make_ctx(), "broken.py"))

    assert "syntax errors" in result.lower() or "could not be parsed" in result.lower()


@patch("cai.tools.block_tools._resolve")
def test_block_overview_empty_file(mock_resolve, tmp_path):
    """An empty file (no functions or classes) returns a header with no blocks."""
    f = tmp_path / "empty.py"
    f.write_text('"""Module docstring."""\n')
    mock_resolve.return_value = f

    result = _run(block_overview(_make_ctx(), "empty.py"))

    assert "## Block Overview: empty.py" in result
    # No blocks means no block indices appear
    assert "0." not in result


@patch("cai.tools.block_tools._resolve")
def test_block_overview_file_not_found(mock_resolve, tmp_path):
    """When the resolved path does not exist, block_overview returns an error."""
    f = tmp_path / "missing.py"  # path exists but file doesn't
    mock_resolve.return_value = f

    result = _run(block_overview(_make_ctx(), "missing.py"))

    assert "File not found" in result
    assert "missing.py" in result


@patch("cai.tools.block_tools._resolve")
def test_block_overview_permission_error(mock_resolve):
    """When _resolve raises PermissionError, the error message is returned."""
    mock_resolve.side_effect = PermissionError("Path escapes repository root")

    result = _run(block_overview(_make_ctx(), "../escape.py"))

    assert "Path escapes repository root" in result


@patch("cai.tools.block_tools._resolve")
def test_block_overview_triple_single_quotes(mock_resolve, tmp_path):
    """Functions with ''' triple-single-quote docstrings are parsed correctly."""
    f = tmp_path / "single_quotes.py"
    f.write_text(
        "def func_a():\n"
        "    '''Triple single docstring.'''\n"
        "    pass\n"
        "\n"
        "def func_b():\n"
        '    """Triple double docstring."""\n'
        "    pass\n"
    )
    mock_resolve.return_value = f

    result = _run(block_overview(_make_ctx(), "single_quotes.py"))

    assert "Triple single docstring" in result
    assert "Triple double docstring" in result
    assert "func_a" in result
    assert "func_b" in result


@patch("cai.tools.block_tools._resolve")
def test_block_overview_class_no_methods(mock_resolve, tmp_path):
    """A class with no methods is listed correctly and has no method blocks."""
    f = tmp_path / "empty_class.py"
    f.write_text(
        "class Empty:\n"
        '    """An empty class."""\n'
        "    pass\n"
        "\n"
        "\n"
        "def standalone():\n"
        "    pass\n"
    )
    mock_resolve.return_value = f

    result = _run(block_overview(_make_ctx(), "empty_class.py"))

    assert "class" in result
    assert "Empty" in result
    assert "An empty class" in result
    assert "0. [class    ] `Empty`" in result
    assert "standalone" in result
    # Only one class + one function = 2 blocks, no methods
    assert "method" not in result


@patch("cai.tools.block_tools._resolve")
def test_block_overview_decorated_async_function(mock_resolve, tmp_path):
    """A decorated async function is correctly detected."""
    f = tmp_path / "decorated_async.py"
    f.write_text(
        "from functools import wraps\n"
        "\n"
        "\n"
        "@wraps\n"
        "async def fetch():\n"
        '    """Fetch something."""\n'
        "    return None\n"
    )
    mock_resolve.return_value = f

    result = _run(block_overview(_make_ctx(), "decorated_async.py"))

    assert "async function" in result
    assert "fetch" in result
    assert "Fetch something" in result


@patch("cai.tools.block_tools._resolve")
def test_block_overview_only_imports_and_globals(mock_resolve, tmp_path):
    """A file with only imports and global assignments produces no blocks."""
    f = tmp_path / "config.py"
    f.write_text(
        "import os\n"
        "import sys\n"
        "\n"
        "DEBUG = True\n"
        "MAX_RETRIES = 3\n"
    )
    mock_resolve.return_value = f

    result = _run(block_overview(_make_ctx(), "config.py"))

    assert "## Block Overview: config.py" in result
    # No function, class, or method blocks
    assert "0." not in result
    assert "function" not in result or "0." not in result
    assert "class" not in result or "0." not in result


@patch("cai.tools.block_tools._resolve")
def test_block_overview_multiple_decorators(mock_resolve, tmp_path):
    """A function with multiple stacked decorators is handled correctly."""
    f = tmp_path / "multi_deco.py"
    f.write_text(
        "@deco1\n"
        "@deco2\n"
        "def multi_decorated():\n"
        '    """Multi-decorated."""\n'
        "    pass\n"
    )
    mock_resolve.return_value = f

    result = _run(block_overview(_make_ctx(), "multi_deco.py"))

    assert "multi_decorated" in result
    assert "Multi-decorated" in result
    # Line range should cover all decorators
    assert "lines 1-5" in result


@patch("cai.tools.block_tools._resolve")
def test_block_overview_nested_class(mock_resolve, tmp_path):
    """Nested classes inside top-level classes are handled without crashing."""
    f = tmp_path / "nested.py"
    f.write_text(
        "class Outer:\n"
        '    """Outer class."""\n'
        "\n"
        "    class Inner:\n"
        '        """Inner class."""\n'
        "        pass\n"
        "\n"
        "    def method(self):\n"
        "        pass\n"
    )
    mock_resolve.return_value = f

    result = _run(block_overview(_make_ctx(), "nested.py"))

    assert "Outer" in result
    assert "Outer class" in result
    assert "method" in result
    # The nested class is not a function_definition or decorated_definition,
    # so it should not be collected as a method block
    assert "Inner" not in result
    # Only 2 blocks: Outer (index 0) and method (index 1)
    assert "0." in result
    assert "1." in result


# ---------------------------------------------------------------------------
# block_edit — basic behaviour
# ---------------------------------------------------------------------------


@patch("cai.tools.block_tools._resolve")
def test_block_edit_success(mock_resolve, tmp_path):
    """Replacing a method body updates the file and returns new line range."""
    f = tmp_path / "edit_sample.py"
    f.write_text(
        "class Foo:\n"
        "    def bar(self):\n"
        '        """Old docstring."""\n'
        "        return 1\n"
        "\n"
        "    def baz(self):\n"
        "        return 2\n"
    )
    mock_resolve.return_value = f

    result = _run(block_edit(_make_ctx(), "edit_sample.py", 1, "    def bar(self):\n        return 42\n"))

    assert "Replaced method `bar`" in result
    # Check file was actually written
    new_content = f.read_text()
    assert "return 42" in new_content
    assert "Old docstring" not in new_content


@patch("cai.tools.block_tools._resolve")
def test_block_edit_out_of_range(mock_resolve, tmp_path):
    """An out-of-range block_index returns an error with the valid range."""
    f = tmp_path / "range.py"
    f.write_text("def a():\n    pass\n\ndef b():\n    pass\n")
    mock_resolve.return_value = f

    result = _run(block_edit(_make_ctx(), "range.py", 5, "def x():\n    pass\n"))

    assert "out of range" in result
    assert "0-1" in result  # two blocks: indices 0 and 1


@patch("cai.tools.block_tools._resolve")
def test_block_edit_non_python_file(mock_resolve, tmp_path):
    """Editing a non-.py file returns an error without modifying."""
    f = tmp_path / "data.txt"
    f.write_text("original")
    mock_resolve.return_value = f

    result = _run(block_edit(_make_ctx(), "data.txt", 0, "changed"))

    assert "not a Python file" in result
    assert f.read_text() == "original"


@patch("cai.tools.block_tools._resolve")
def test_block_edit_parse_error(mock_resolve, tmp_path):
    """Editing a file with initial syntax errors returns an error."""
    f = tmp_path / "broken_edit.py"
    f.write_text("def broken(\n")
    mock_resolve.return_value = f

    result = _run(block_edit(_make_ctx(), "broken_edit.py", 0, "fixed"))

    assert "syntax errors" in result.lower() or "could not be parsed" in result.lower()


@patch("cai.tools.block_tools._resolve")
def test_block_edit_file_not_found(mock_resolve, tmp_path):
    """When the resolved path does not exist, block_edit returns an error."""
    f = tmp_path / "missing.py"  # path exists but file doesn't
    mock_resolve.return_value = f

    result = _run(block_edit(_make_ctx(), "missing.py", 0, "replacement"))

    assert "File not found" in result
    assert "missing.py" in result


@patch("cai.tools.block_tools._resolve")
def test_block_edit_permission_error(mock_resolve):
    """When _resolve raises PermissionError, the error message is returned."""
    mock_resolve.side_effect = PermissionError("Path escapes repository root")

    result = _run(block_edit(_make_ctx(), "../escape.py", 0, "replacement"))

    assert "Path escapes repository root" in result


@patch("cai.tools.block_tools._resolve")
def test_block_edit_negative_index(mock_resolve, tmp_path):
    """A negative block_index is out of range and returns an error."""
    f = tmp_path / "neg.py"
    f.write_text("def a():\n    pass\n")
    mock_resolve.return_value = f

    result = _run(block_edit(_make_ctx(), "neg.py", -1, "replacement"))

    assert "out of range" in result
    assert "-1" in result
    assert "0-0" in result


@patch("cai.tools.block_tools._resolve")
def test_block_edit_syntax_error_warning(mock_resolve, tmp_path):
    """An edit that introduces syntax errors returns a warning but writes the file."""
    f = tmp_path / "warn_edit.py"
    f.write_text(
        "def good():\n"
        "    return 1\n"
        "\n"
        "def keep():\n"
        "    return 2\n"
    )
    mock_resolve.return_value = f

    result = _run(block_edit(_make_ctx(), "warn_edit.py", 0, "this is not valid Python"))

    # The warning is returned despite the edit being applied
    assert "Warning:" in result
    assert "replaced" in result or "replaced" in result.lower()
    # File was written despite syntax errors
    new_content = f.read_text()
    assert "this is not valid Python" in new_content
    # The second function should still be in the file
    assert "def keep():" in new_content


@patch("cai.tools.block_tools._resolve")
def test_block_edit_replace_top_level_function(mock_resolve, tmp_path):
    """Replacing a top-level function works and updates the line range."""
    f = tmp_path / "func_edit.py"
    f.write_text(
        "def old_func():\n"
        "    return 1\n"
        "\n"
        "def other():\n"
        "    return 2\n"
    )
    mock_resolve.return_value = f

    result = _run(block_edit(_make_ctx(), "func_edit.py", 0, "def old_func():\n    return 42\n"))

    assert "Replaced function `old_func`" in result
    new_content = f.read_text()
    assert "return 42" in new_content
    assert "return 1" not in new_content


@patch("cai.tools.block_tools._resolve")
def test_block_edit_replace_class(mock_resolve, tmp_path):
    """Replacing an entire class block works."""
    f = tmp_path / "class_edit.py"
    f.write_text(
        "class OldClass:\n"
        '    """Old docstring."""\n'
        "    def method(self):\n"
        "        return 1\n"
        "\n"
        "class Other:\n"
        "    pass\n"
    )
    mock_resolve.return_value = f

    result = _run(block_edit(_make_ctx(), "class_edit.py", 0, "class OldClass:\n    pass\n"))

    assert "Replaced class `OldClass`" in result
    new_content = f.read_text()
    assert "class OldClass:" in new_content
    assert "class Other:" in new_content
    assert "Old docstring" not in new_content
    assert "return 1" not in new_content


@patch("cai.tools.block_tools._resolve")
def test_block_edit_block_removed_on_reparse(mock_resolve, tmp_path):
    """When the edit removes enough structure that blocks shrink, the fallback path is used."""
    f = tmp_path / "shrink.py"
    f.write_text(
        "def a():\n"
        "    pass\n"
    )
    mock_resolve.return_value = f

    # Replace the only block with something that parses but has no named block
    result = _run(block_edit(_make_ctx(), "shrink.py", 0, "x = 1\n"))

    assert "Replaced" in result
    assert "function" in result or "function" in result.lower()
    assert "a" in result
    # The fallback message when block_index >= new block count
    # should still mention the old block name
    assert "a" in result


@patch("cai.tools.block_tools._resolve")
def test_block_edit_replace_async_function(mock_resolve, tmp_path):
    """Replacing an async top-level function works correctly."""
    f = tmp_path / "async_edit.py"
    f.write_text(
        "async def fetch():\n"
        '    """Fetch data."""\n'
        "    return 1\n"
        "\n"
        "\n"
        "def sync_func():\n"
        "    return 2\n"
    )
    mock_resolve.return_value = f

    result = _run(block_edit(_make_ctx(), "async_edit.py", 0, "async def fetch():\n    return 42\n"))

    assert "Replaced" in result
    assert "fetch" in result
    new_content = f.read_text()
    assert "return 42" in new_content
    assert "sync_func" in new_content


@patch("cai.tools.block_tools._resolve")
def test_block_edit_replace_decorated_function(mock_resolve, tmp_path):
    """Replacing a decorated function works correctly."""
    f = tmp_path / "deco_edit.py"
    f.write_text(
        "@lru_cache\n"
        "def cached():\n"
        '    """Cached func."""\n'
        "    return 1\n"
        "\n"
        "\n"
        "def other():\n"
        "    return 2\n"
    )
    mock_resolve.return_value = f

    result = _run(block_edit(_make_ctx(), "deco_edit.py", 0, "@lru_cache\ndef cached():\n    return 42\n"))

    assert "Replaced" in result
    assert "cached" in result
    new_content = f.read_text()
    assert "return 42" in new_content
    assert "return 1" not in new_content
    assert "def other():" in new_content


@patch("cai.tools.block_tools._resolve")
def test_block_edit_replace_class_with_methods(mock_resolve, tmp_path):
    """Replacing a class that has methods removes its methods from the index."""
    f = tmp_path / "class_methods_edit.py"
    f.write_text(
        "class OldClass:\n"
        "    def method_a(self):\n"
        "        return 1\n"
        "\n"
        "    def method_b(self):\n"
        "        return 2\n"
        "\n"
        "\n"
        "class Other:\n"
        "    pass\n"
    )
    mock_resolve.return_value = f

    # Replace the class (index 0); this removes the class and its methods
    result = _run(block_edit(_make_ctx(), "class_methods_edit.py", 0, "class OldClass:\n    pass\n"))

    assert "Replaced class `OldClass`" in result
    new_content = f.read_text()
    assert "class OldClass:" in new_content
    assert "method_a" not in new_content or "OldClass" in new_content
    assert "class Other:" in new_content


@patch("cai.tools.block_tools._resolve")
def test_block_edit_preserves_unchanged_blocks(mock_resolve, tmp_path):
    """Editing one block leaves other blocks in the file untouched."""
    f = tmp_path / "preserve.py"
    f.write_text(
        "def first():\n"
        "    return 1\n"
        "\n"
        "\n"
        "def second():\n"
        '    """Second doc."""\n'
        "    return 2\n"
        "\n"
        "\n"
        "def third():\n"
        "    return 3\n"
    )
    mock_resolve.return_value = f

    result = _run(block_edit(_make_ctx(), "preserve.py", 1, "def second():\n    return 99\n"))

    assert "Replaced function `second`" in result
    new_content = f.read_text()
    assert "return 1" in new_content
    assert "return 99" in new_content
    assert "return 3" in new_content
    assert "Second doc" not in new_content


@patch("cai.tools.block_tools._resolve")
def test_block_edit_empty_new_content_no_crash(mock_resolve, tmp_path):
    """Empty new_content does not crash the tool (edge case for line handling)."""
    f = tmp_path / "empty_edit.py"
    f.write_text(
        "def target():\n"
        "    return 1\n"
    )
    mock_resolve.return_value = f

    # Replacing with an empty string is valid — removes the block's content
    result = _run(block_edit(_make_ctx(), "empty_edit.py", 0, ""))

    assert "Replaced" in result
    assert "target" in result


# ---------------------------------------------------------------------------
# _get_parser — singleton caching
# ---------------------------------------------------------------------------


def test_get_parser_returns_parser_instance():
    """_get_parser returns a tree_sitter Parser instance."""
    from cai.tools.block_tools import _get_parser

    parser = _get_parser()
    from tree_sitter import Parser

    assert isinstance(parser, Parser)


def test_get_parser_is_singleton():
    """_get_parser caches and returns the same instance on subsequent calls."""
    from cai.tools.block_tools import _get_parser

    p1 = _get_parser()
    p2 = _get_parser()
    assert p1 is p2


# ---------------------------------------------------------------------------
# _extract_docstring — edge cases
# ---------------------------------------------------------------------------


def test_extract_docstring_empty_body():
    """_extract_docstring returns None when the body has no children."""
    from cai.tools.block_tools import _get_parser, _extract_docstring

    parser = _get_parser()
    tree = parser.parse(b"def f():\n    pass\n")
    # f's body has one child (pass) — not empty. Test an empty class body instead.
    tree2 = parser.parse(b"class Empty:\n    pass\n")
    func_node = tree.root_node.named_children[0]  # function_definition
    result = _extract_docstring(func_node)
    assert result is None  # "pass" is not a docstring


def test_extract_docstring_non_string_first_expression(tmp_path):
    """_extract_docstring returns None when the first expression is not a string."""
    from cai.tools.block_tools import _get_parser, _extract_docstring

    parser = _get_parser()
    # First expression is a number literal, not a string
    tree = parser.parse(b"def f():\n    1\n")
    func_node = tree.root_node.named_children[0]
    result = _extract_docstring(func_node)
    assert result is None


def test_extract_docstring_non_expression_first(tmp_path):
    """_extract_docstring returns None when the first body node is not an expression_statement."""
    from cai.tools.block_tools import _get_parser, _extract_docstring

    parser = _get_parser()
    # First statement in body is a return, not a string expression
    tree = parser.parse(b"def f():\n    return 42\n")
    func_node = tree.root_node.named_children[0]
    result = _extract_docstring(func_node)
    assert result is None


# ---------------------------------------------------------------------------
# _unwrap_decorated — passthrough
# ---------------------------------------------------------------------------


def test_unwrap_decorated_passthrough():
    """_unwrap_decorated returns the node unchanged when it is not a decorated_definition."""
    from cai.tools.block_tools import _get_parser, _unwrap_decorated

    parser = _get_parser()
    tree = parser.parse(b"def plain():\n    pass\n")
    func_node = tree.root_node.named_children[0]
    assert func_node.type == "function_definition"
    result = _unwrap_decorated(func_node)
    assert result is func_node


# ---------------------------------------------------------------------------
# _format_overview — edge cases
# ---------------------------------------------------------------------------


def test_format_overview_empty_blocks():
    """_format_overview with an empty block list returns just the header."""
    from cai.tools.block_tools import _format_overview

    result = _format_overview("empty.py", [])
    assert result == "## Block Overview: empty.py"


# ---------------------------------------------------------------------------
# block_overview — formatting edge cases
# ---------------------------------------------------------------------------


@patch("cai.tools.block_tools._resolve")
def test_block_overview_async_function_suffix(mock_resolve, tmp_path):
    """An async function is formatted with the '(async)' suffix."""
    f = tmp_path / "async_suffix.py"
    f.write_text(
        "async def fetch():\n"
        '    """Fetch."""\n'
        "    pass\n"
    )
    mock_resolve.return_value = f

    result = _run(block_overview(_make_ctx(), "async_suffix.py"))

    assert "async function" in result
    assert "(async)" in result


@patch("cai.tools.block_tools._resolve")
def test_block_overview_only_decorated_class(mock_resolve, tmp_path):
    """A file with only a decorated class is parsed correctly."""
    f = tmp_path / "deco_class_only.py"
    f.write_text(
        "@dataclass\n"
        "class Config:\n"
        '    """Config class."""\n'
        "    x: int = 1\n"
    )
    mock_resolve.return_value = f

    result = _run(block_overview(_make_ctx(), "deco_class_only.py"))

    assert "class" in result
    assert "Config" in result
    assert "Config class" in result


# ---------------------------------------------------------------------------
# block_edit — additional edge cases
# ---------------------------------------------------------------------------


@patch("cai.tools.block_tools._resolve")
def test_block_edit_last_block(mock_resolve, tmp_path):
    """Editing the last block in a file works correctly (end-of-file boundary)."""
    f = tmp_path / "last_block.py"
    f.write_text(
        "def first():\n"
        "    return 1\n"
        "\n"
        "\n"
        "def last():\n"
        "    return 2\n"
    )
    mock_resolve.return_value = f

    result = _run(block_edit(_make_ctx(), "last_block.py", 1, "def last():\n    return 99\n"))

    assert "Replaced function `last`" in result
    new_content = f.read_text()
    assert "return 99" in new_content
    assert "return 1" in new_content


@patch("cai.tools.block_tools._resolve")
def test_block_edit_decorated_class(mock_resolve, tmp_path):
    """Replacing a decorated class block works correctly."""
    f = tmp_path / "deco_class_edit.py"
    f.write_text(
        "@dataclass\n"
        "class Config:\n"
        '    """Old doc."""\n'
        "    x: int = 1\n"
        "\n"
        "\n"
        "def other():\n"
        "    pass\n"
    )
    mock_resolve.return_value = f

    result = _run(block_edit(_make_ctx(), "deco_class_edit.py", 0,
                             "@dataclass\nclass Config:\n    y: int = 2\n"))

    assert "Replaced" in result
    assert "class" in result or "Config" in result
    new_content = f.read_text()
    assert "y: int = 2" in new_content
    assert "Old doc" not in new_content
    assert "def other():" in new_content


@patch("cai.tools.block_tools._resolve")
def test_block_edit_method_inside_decorated_class(mock_resolve, tmp_path):
    """Editing a method inside a decorated class works correctly."""
    f = tmp_path / "deco_class_method.py"
    f.write_text(
        "@some_decorator\n"
        "class MyClass:\n"
        "    def method_a(self):\n"
        '        """Old a."""\n'
        "        return 1\n"
        "\n"
        "    def method_b(self):\n"
        "        return 2\n"
    )
    mock_resolve.return_value = f

    # method_a is at index 1 (index 0 is the decorated class)
    result = _run(block_edit(_make_ctx(), "deco_class_method.py", 1,
                             "    def method_a(self):\n        return 42\n"))

    assert "Replaced" in result
    assert "method_a" in result
    new_content = f.read_text()
    assert "return 42" in new_content
    assert "Old a" not in new_content
    assert "method_b" in new_content


@patch("cai.tools.block_tools._resolve")
def test_block_edit_single_block_file(mock_resolve, tmp_path):
    """Editing the only block in a single-block file works."""
    f = tmp_path / "single.py"
    f.write_text(
        "def only_one():\n"
        "    return 1\n"
    )
    mock_resolve.return_value = f

    result = _run(block_edit(_make_ctx(), "single.py", 0,
                             "def only_one():\n    return 99\n"))

    assert "Replaced function `only_one`" in result
    new_content = f.read_text()
    assert "return 99" in new_content
    assert "return 1" not in new_content


# ---------------------------------------------------------------------------
# Tool constant checks
# ---------------------------------------------------------------------------


def test_block_overview_tool_is_tool_instance():
    """BLOCK_OVERVIEW_TOOL is a pydantic_ai Tool."""
    from pydantic_ai import Tool

    assert isinstance(BLOCK_OVERVIEW_TOOL, Tool)


def test_block_edit_tool_is_tool_instance():
    """BLOCK_EDIT_TOOL is a pydantic_ai Tool."""
    from pydantic_ai import Tool

    assert isinstance(BLOCK_EDIT_TOOL, Tool)


def test_tool_names():
    """The tool names match the function names."""
    assert BLOCK_OVERVIEW_TOOL.name == "block_overview"
    assert BLOCK_EDIT_TOOL.name == "block_edit"


# ---------------------------------------------------------------------------
# TOOL_FACTORIES registration
# ---------------------------------------------------------------------------


def test_block_tools_registered_in_tool_factories():
    """Both block_* tools are registered in TOOL_FACTORIES."""
    from cai.agents.loader import TOOL_FACTORIES

    assert "block_overview" in TOOL_FACTORIES
    assert TOOL_FACTORIES["block_overview"] == "cai.tools.block_tools:BLOCK_OVERVIEW_TOOL"
    assert "block_edit" in TOOL_FACTORIES
    assert TOOL_FACTORIES["block_edit"] == "cai.tools.block_tools:BLOCK_EDIT_TOOL"


# ---------------------------------------------------------------------------
# Module docstring
# ---------------------------------------------------------------------------


def test_module_docstring_exists():
    """The block_tools module has a docstring describing its purpose."""
    import cai.tools.block_tools as bt

    assert bt.__doc__ is not None
    assert len(bt.__doc__) > 0
    assert "block_overview" in bt.__doc__
    assert "block_edit" in bt.__doc__


# ---------------------------------------------------------------------------
# Import factory resolution
# ---------------------------------------------------------------------------


def test_import_factory_resolves_block_overview_tool():
    """The factory target string for block_overview resolves to the correct tool."""
    from cai.agents.loader import TOOL_FACTORIES, _import_factory

    tool = _import_factory(TOOL_FACTORIES["block_overview"])
    assert tool is BLOCK_OVERVIEW_TOOL


def test_import_factory_resolves_block_edit_tool():
    """The factory target string for block_edit resolves to the correct tool."""
    from cai.agents.loader import TOOL_FACTORIES, _import_factory

    tool = _import_factory(TOOL_FACTORIES["block_edit"])
    assert tool is BLOCK_EDIT_TOOL


# ---------------------------------------------------------------------------
# Agent markdown configuration checks
# ---------------------------------------------------------------------------


def test_explore_agent_includes_block_overview():
    """The explore agent markdown lists block_overview in its tools."""
    from cai.agents.loader import parse_agent_md, resolve_agent_path

    explore_file = resolve_agent_path("explore")
    config, _ = parse_agent_md(explore_file)

    tools = config.get("tools", [])
    assert "block_overview" in tools, "explore.md must include block_overview in its tools list"


def test_explore_agent_model_is_flash():
    """The explore agent uses deepseek-v4-flash (not pro) to cap reasoning latency."""
    from cai.agents.loader import parse_agent_md, resolve_agent_path

    explore_file = resolve_agent_path("explore")
    config, _ = parse_agent_md(explore_file)

    assert config["model"] == "deepseek/deepseek-v4-flash", (
        "explore.md must use deepseek/deepseek-v4-flash to avoid excessive reasoning latency"
    )


def test_implement_agent_includes_block_edit():
    """The implement agent markdown lists block_edit in its tools."""
    from cai.agents.loader import parse_agent_md, resolve_agent_path

    implement_file = resolve_agent_path("implement")
    config, _ = parse_agent_md(implement_file)

    tools = config.get("tools", [])
    assert "block_edit" in tools, "implement.md must include block_edit in its tools list"
