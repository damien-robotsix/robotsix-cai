"""``block_overview`` and ``block_edit`` — tree-sitter based functional block read/edit tools for Python files.

``block_overview`` parses a Python file into meaningful top-level blocks
(functions, classes, and class methods) with associated docstrings and
returns a structured markdown overview. ``block_edit`` replaces a block
by index using tree-sitter to locate the exact line range.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

from pydantic_ai import RunContext, Tool
from tree_sitter import Language, Parser
from tree_sitter_python import language as py_language

from cai.agents.fs_ops import _resolve

_parser: Parser | None = None


def _get_parser() -> Parser:
    global _parser
    if _parser is None:
        _parser = Parser(Language(py_language()))
    return _parser


@dataclass
class _Block:
    index: int
    kind: str  # "function", "async function", "class", "method"
    name: str
    start: int  # 1-based inclusive
    end: int  # 1-based inclusive
    docstring: str | None = None
    parent_class: str | None = None


def _extract_docstring(node) -> str | None:
    """Extract the docstring from a function or class body, or None."""
    body = node.child_by_field_name("body")
    if body is None or body.named_child_count == 0:
        return None
    first = body.named_children[0]
    if first.type != "expression_statement":
        return None
    for child in first.children:
        if child.type == "string":
            text = child.text.decode()
            if text.startswith('"""') and text.endswith('"""') and len(text) >= 6:
                return text[3:-3]
            if text.startswith("'''") and text.endswith("'''") and len(text) >= 6:
                return text[3:-3]
            if len(text) >= 2:
                return text[1:-1]
    return None


def _unwrap_decorated(node):
    """Return the inner function_definition or class_definition from a decorated_definition, or the node itself."""
    if node.type == "decorated_definition":
        for child in node.named_children:
            if child.type in ("function_definition", "class_definition"):
                return child
    return node


def _make_block(node, index: int, *, parent_class: str | None = None) -> _Block:
    """Build a _Block from a function_definition, class_definition, or decorated_definition node."""
    start = node.start_point[0] + 1
    end = node.end_point[0] + 1

    inner = _unwrap_decorated(node)
    name_node = inner.child_by_field_name("name")
    name = name_node.text.decode() if name_node is not None else "?"

    is_async = any(child.type == "async" for child in inner.children)

    if inner.type == "class_definition":
        kind = "class"
    elif parent_class is not None:
        kind = "method"
    else:
        kind = "async function" if is_async else "function"

    docstring = _extract_docstring(inner)

    return _Block(
        index=index,
        kind=kind,
        name=name,
        start=start,
        end=end,
        docstring=docstring,
        parent_class=parent_class,
    )


def _collect_blocks(root_node) -> list[_Block]:
    """Walk the root module node collecting top-level blocks and class methods."""
    blocks: list[_Block] = []
    index = 0

    for child in root_node.named_children:
        if child.type == "function_definition":
            blocks.append(_make_block(child, index))
            index += 1
        elif child.type == "class_definition":
            blocks.append(_make_block(child, index))
            index += 1
            # Collect methods
            body = child.child_by_field_name("body")
            if body is not None:
                cls_name = blocks[-1].name
                for body_child in body.named_children:
                    if body_child.type in ("function_definition", "decorated_definition"):
                        blocks.append(_make_block(body_child, index, parent_class=cls_name))
                        index += 1
        elif child.type == "decorated_definition":
            inner = _unwrap_decorated(child)
            blocks.append(_make_block(child, index))
            index += 1
            # If it wraps a class, collect its methods too
            if inner.type == "class_definition":
                cls_name = blocks[-1].name
                body = inner.child_by_field_name("body")
                if body is not None:
                    for body_child in body.named_children:
                        if body_child.type in ("function_definition", "decorated_definition"):
                            blocks.append(_make_block(body_child, index, parent_class=cls_name))
                            index += 1

    return blocks


def _format_overview(path: str, blocks: list[_Block]) -> str:
    """Format a block list as a markdown overview string."""
    lines = [f"## Block Overview: {path}"]
    for block in blocks:
        if block.kind == "method":
            prefix = f"   {block.index}. [method   ]"
            doc_indent = "      "
        elif block.kind == "async function":
            prefix = f"{block.index}. [async function]"
            doc_indent = "   "
        else:
            prefix = f"{block.index}. [{block.kind:<9}]"
            doc_indent = "   "

        suffix = " (async)" if block.kind == "async function" else ""
        line = f"{prefix} `{block.name}` — lines {block.start}-{block.end}{suffix}"
        lines.append(line)

        if block.docstring is not None:
            lines.append(f"{doc_indent}docstring: {block.docstring}")

    return "\n".join(lines)


async def _resolve_and_parse(ctx: RunContext, path: str) -> tuple[Path, list[_Block]]:
    resolved = _resolve(ctx, path)
    if not resolved.exists():
        raise FileNotFoundError(f"File not found: {path!r}")
    if resolved.suffix != ".py":
        raise ValueError(f"Error: {path!r} is not a Python file (.py extension required)")
    file_bytes = resolved.read_bytes()
    tree = await asyncio.to_thread(_get_parser().parse, file_bytes)
    if tree.root_node.has_error:
        raise ValueError(f"Error: {path!r} contains syntax errors and could not be parsed")
    blocks = _collect_blocks(tree.root_node)
    return resolved, blocks


async def block_overview(ctx: RunContext, path: str) -> str:
    """Parse a Python file into an overview of top-level blocks (functions, classes, methods).

    Args:
        path: Path to the Python file (relative to repo root).

    Returns:
        Markdown listing of blocks with indices, kinds, names, line ranges,
        and docstrings.
    """
    try:
        resolved, blocks = await _resolve_and_parse(ctx, path)
    except (ValueError, FileNotFoundError, PermissionError) as exc:
        return str(exc)
    return _format_overview(path, blocks)


async def block_edit(ctx: RunContext, path: str, block_index: int, new_content: str) -> str:
    """Replace a functional block (function, class, or method) in a Python file by its index.

    Args:
        path: Path to the Python file (relative to repo root).
        block_index: Zero-based index of the block to replace (from block_overview).
        new_content: New source text to replace the block with.

    Returns:
        Confirmation with block name and new line range, or an error.
        On re-parse failure a warning is returned but the edit is kept on disk.
    """
    try:
        resolved, blocks = await _resolve_and_parse(ctx, path)
    except (ValueError, FileNotFoundError, PermissionError) as exc:
        return str(exc)

    if block_index < 0 or block_index >= len(blocks):
        max_idx = len(blocks) - 1
        return f"Error: block_index {block_index} is out of range. Valid indices: 0-{max_idx}"

    block = blocks[block_index]

    # Read as text with line endings preserved
    lines = resolved.read_text().splitlines(keepends=True)
    new_lines = lines[: block.start - 1] + [new_content] + lines[block.end :]
    new_text = "".join(new_lines)

    resolved.write_text(new_text)

    # Re-parse to confirm
    new_tree = await asyncio.to_thread(_get_parser().parse, new_text.encode())
    if new_tree.root_node.has_error:
        return (
            f"Warning: replaced {block.kind} `{block.name}` (was lines "
            f"{block.start}-{block.end}). The file now has syntax errors — the "
            f"edit was applied but the file may be broken. Review the new content "
            f"and fix any issues."
        )

    # Re-collect to get new line range
    new_blocks = _collect_blocks(new_tree.root_node)
    if block_index < len(new_blocks):
        nb = new_blocks[block_index]
        return (
            f"Replaced {block.kind} `{block.name}` (was lines {block.start}-{block.end}, "
            f"now lines {nb.start}-{nb.end})."
        )
    else:
        return f"Replaced {block.kind} `{block.name}` (was lines {block.start}-{block.end})."


BLOCK_OVERVIEW_TOOL = Tool(block_overview)
BLOCK_EDIT_TOOL = Tool(block_edit)
