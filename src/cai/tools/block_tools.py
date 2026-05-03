"""``block_overview`` and ``block_edit`` — tree-sitter based functional block tools.

Parse Python files into meaningful top-level blocks (functions, classes,
and class methods) with associated docstrings, then let the agent read a
structured overview and replace any block by index.
"""
from __future__ import annotations

from pathlib import Path

from pydantic_ai import RunContext, Tool

from cai.agents.fs_ops import _resolve

# ---------------------------------------------------------------------------
# Lazy tree-sitter initialization (singleton)
# ---------------------------------------------------------------------------

_parser = None


def _get_parser():
    """Return a tree-sitter ``Parser`` configured for Python."""
    global _parser
    if _parser is None:
        import tree_sitter
        import tree_sitter_python

        lang = tree_sitter.Language(tree_sitter_python.language())
        _parser = tree_sitter.Parser(lang)
    return _parser


# ---------------------------------------------------------------------------
# Block extraction helpers
# ---------------------------------------------------------------------------


def _node_text(source: bytes, node) -> str:
    """Return the source text spanned by *node*."""
    return source[node.start_byte : node.end_byte].decode("utf-8")


def _extract_docstring(source: bytes, node) -> str | None:
    """Extract the docstring from a function or class body."""
    body = node.child_by_field_name("body")
    if body is None or body.type != "block":
        return None
    children = body.children
    if not children:
        return None
    first_stmt = children[0]
    if first_stmt.type != "expression_statement":
        return None
    for child in first_stmt.children:
        if child.type == "string":
            raw = _node_text(source, child)
            if raw.startswith('"""') or raw.startswith("'''"):
                inner = raw[3:-3] if len(raw) >= 6 else ""
            elif raw.startswith('"') or raw.startswith("'"):
                inner = raw[1:-1]
            else:
                return None
            return inner.strip() or None
    return None


def _node_line_range(node) -> tuple[int, int]:
    """Return 1-based inclusive (start_line, end_line) for a node."""
    return (node.start_point[0] + 1, node.end_point[0] + 1)


def _is_async(node) -> bool:
    """Return True if a ``function_definition`` node is async."""
    for child in node.children:
        if child.type == "async":
            return True
    return False


def _build_blocks(source: bytes, root_node) -> list[dict]:
    """Walk top-level nodes and build block descriptors.

    Top-level ``function_definition`` and ``class_definition`` nodes
    are collected, plus one level of nesting (methods inside classes).
    Each block dict carries an index, type, name, line range, and
    optional docstring.  The ``node`` key holds the tree-sitter node
    for use by ``block_edit``.
    """
    blocks: list[dict] = []
    idx = 0

    for child in root_node.children:
        if child.type == "function_definition":
            idx += 1
            name_node = child.child_by_field_name("name")
            name = _node_text(source, name_node) if name_node else "<unknown>"
            start, end = _node_line_range(child)
            docstring = _extract_docstring(source, child)
            async_ = _is_async(child)
            blocks.append({
                "index": idx,
                "type": "async function" if async_ else "function",
                "name": name,
                "start_line": start,
                "end_line": end,
                "docstring": docstring,
                "node": child,
            })

        elif child.type == "class_definition":
            idx += 1
            name_node = child.child_by_field_name("name")
            name = _node_text(source, name_node) if name_node else "<unknown>"
            start, end = _node_line_range(child)
            docstring = _extract_docstring(source, child)
            blocks.append({
                "index": idx,
                "type": "class",
                "name": name,
                "start_line": start,
                "end_line": end,
                "docstring": docstring,
                "node": child,
            })

            # Walk class body for method definitions (one nesting level)
            body = child.child_by_field_name("body")
            if body is not None and body.type == "block":
                for stmt in body.children:
                    if stmt.type == "function_definition":
                        idx += 1
                        mname_node = stmt.child_by_field_name("name")
                        mname = (
                            _node_text(source, mname_node)
                            if mname_node
                            else "<unknown>"
                        )
                        mstart, mend = _node_line_range(stmt)
                        mdoc = _extract_docstring(source, stmt)
                        masync = _is_async(stmt)
                        blocks.append({
                            "index": idx,
                            "type": "async method" if masync else "method",
                            "name": mname,
                            "start_line": mstart,
                            "end_line": mend,
                            "docstring": mdoc,
                            "node": stmt,
                        })

    return blocks


def _format_overview(blocks: list[dict]) -> str:
    """Format blocks as a Markdown listing."""
    lines: list[str] = []
    for b in blocks:
        type_ = b["type"]
        name = b["name"]
        start = b["start_line"]
        end = b["end_line"]
        idx = b["index"]
        is_method = type_ in ("method", "async method")

        indent = "    " if is_method else ""
        line = f"{indent}**{idx}.** `{type_}` `{name}` (lines {start}-{end})"
        if b["docstring"]:
            doc = b["docstring"]
            first_line = doc.split("\n")[0]
            line += f" — {first_line}"
        lines.append(line)

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# block_overview — read-only structured overview
# ---------------------------------------------------------------------------


async def block_overview(ctx: RunContext, path: str) -> str:
    """Parse a Python file and list all top-level functional blocks.

    Each block gets an index, type (``function`` / ``async function`` /
    ``class`` / ``method`` / ``async method``), name, 1-based inclusive
    line range, and docstring summary.  Class methods are nested under
    their parent class.  Only ``.py`` files are supported.

    Args:
        path: Path to the Python file (relative to repo root).

    Returns:
        Markdown-formatted block listing.
    """
    try:
        resolved = _resolve(ctx, path)
    except PermissionError as exc:
        return str(exc)

    if not resolved.exists():
        return f"File not found: {path!r}"
    if not resolved.is_file():
        return f"Not a regular file: {path!r}"
    if not path.endswith(".py"):
        return f"Not a Python file: {path!r} (only .py files are supported)"

    source = resolved.read_bytes()

    try:
        parser = _get_parser()
        tree = parser.parse(source)
    except Exception as exc:
        return f"Failed to parse {path!r}: {exc}"

    blocks = _build_blocks(source, tree.root_node)

    if not blocks:
        return f"No top-level blocks found in {path!r}"

    return _format_overview(blocks)


# ---------------------------------------------------------------------------
# block_edit — replace a block by index
# ---------------------------------------------------------------------------


async def block_edit(
    ctx: RunContext, path: str, block_index: int, new_content: str
) -> str:
    """Replace a functional block in a Python file by its index.

    Re-parses the file, validates the index, and replaces the block's
    lines with *new_content*.  The block's original leading indentation
    is preserved — *new_content* should include its own internal
    indentation but not the block's leading whitespace (it is
    reapplied).  The file is re-parsed after the edit to confirm
    validity.

    Args:
        path: Path to the Python file (relative to repo root).
        block_index: 1-based block index from ``block_overview``.
        new_content: Replacement source code for the block.

    Returns:
        Confirmation with the block name and new line range.
    """
    try:
        resolved = _resolve(ctx, path)
    except PermissionError as exc:
        return str(exc)

    if not resolved.exists():
        return f"File not found: {path!r}"
    if not resolved.is_file():
        return f"Not a regular file: {path!r}"
    if not path.endswith(".py"):
        return f"Not a Python file: {path!r} (only .py files are supported)"

    source = resolved.read_bytes()

    try:
        parser = _get_parser()
        tree = parser.parse(source)
    except Exception as exc:
        return f"Failed to parse {path!r}: {exc}"

    blocks = _build_blocks(source, tree.root_node)

    if block_index < 1 or block_index > len(blocks):
        return (
            f"Block index {block_index} out of range. "
            f"Valid indices: 1-{len(blocks)}. "
            f"Call block_overview to see the current block listing."
        )

    target = blocks[block_index - 1]
    node = target["node"]
    old_start = target["start_line"]
    old_end = target["end_line"]

    # Determine the block's leading indentation from its first line
    all_lines = source.decode("utf-8").splitlines(keepends=True)
    block_lines = all_lines[old_start - 1 : old_end]
    first_line = block_lines[0]
    indent = first_line[: len(first_line) - len(first_line.lstrip())]

    # Build replacement lines, reapplying the original indentation
    new_lines = new_content.splitlines(keepends=True)
    if not new_lines:
        new_lines = ["\n"]
    if new_lines and not new_lines[-1].endswith("\n"):
        new_lines[-1] = new_lines[-1] + "\n"
    indented_lines = [indent + line for line in new_lines]

    before = "".join(all_lines[: old_start - 1])
    after = "".join(all_lines[old_end:])
    new_source = before + "".join(indented_lines) + after

    # Re-parse to validate the edit
    try:
        parser.parse(new_source.encode("utf-8"))
    except Exception as exc:
        return f"Edit resulted in unparseable file: {exc}"

    # Write back
    resolved.write_text(new_source)

    new_line_count = len(indented_lines)
    new_end = old_start + new_line_count - 1
    block_name = target["name"]
    block_type = target["type"]
    return (
        f"Replaced {block_type} `{block_name}` (block {block_index}) "
        f"in {path!r}. Old lines {old_start}-{old_end}, "
        f"new lines {old_start}-{new_end}."
    )


BLOCK_OVERVIEW_TOOL = Tool(block_overview)
BLOCK_EDIT_TOOL = Tool(block_edit)
