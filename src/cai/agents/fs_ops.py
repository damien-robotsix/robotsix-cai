"""``move_file`` and ``delete_file`` — custom filesystem tools.

The built-in deep-console toolset covers read/write/edit but lacks
rename/move and delete.  These tools fill that gap with the same
path-validation approach as the backend: paths are resolved relative to
the backend root and constrained to stay within it.

The ``batch_*`` variants exist because mass-refactor turns (e.g. a
package rename touching dozens of files) otherwise become one tool call
per file plus per-file verification reads, dwarfing the actual edit
latency. The batch tools pre-validate every path before mutating any,
so a typo in entry 9 of 10 fails the whole batch instead of leaving the
tree half-moved.
"""
from __future__ import annotations

import shutil
from pathlib import Path

from pydantic import BaseModel
from pydantic_ai import RunContext, Tool


def _resolve(ctx: RunContext, rel_path: str) -> Path:
    root = Path(ctx.deps.backend.root_dir).resolve()
    resolved = (root / rel_path).resolve()
    if not str(resolved).startswith(str(root)):
        raise PermissionError(f"Path escapes repository root: {rel_path!r}")
    return resolved


async def move_file(ctx: RunContext, source: str, destination: str) -> str:
    """Move or rename a file or directory within the repository.

    Args:
        source: Path to the file or directory to move (relative to repo root).
        destination: Target path (relative to repo root). Parent directories
            are created automatically.

    Returns:
        Confirmation message on success, or an error description.
    """
    try:
        src = _resolve(ctx, source)
        dst = _resolve(ctx, destination)
    except PermissionError as exc:
        return str(exc)

    if not src.exists():
        return f"Source does not exist: {source!r}"

    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dst))
    return f"Moved {source!r} → {destination!r}"


async def delete_file(ctx: RunContext, path: str) -> str:
    """Delete a file or directory within the repository.

    Args:
        path: Path to delete (relative to repo root). Directories are
            deleted recursively.

    Returns:
        Confirmation message on success, or an error description.
    """
    try:
        target = _resolve(ctx, path)
    except PermissionError as exc:
        return str(exc)

    if not target.exists():
        return f"Path does not exist: {path!r}"

    if target.is_dir():
        shutil.rmtree(target)
        return f"Deleted directory {path!r}"
    else:
        target.unlink()
        return f"Deleted file {path!r}"


class MoveOp(BaseModel):
    source: str
    destination: str


async def batch_move(ctx: RunContext, moves: list[MoveOp]) -> str:
    """Move or rename many files/directories in one call.

    All entries are validated up front: if any source escapes the repo
    root or doesn't exist, nothing is moved and the error is returned.
    Use this for mass reorganizations (renames, package moves) instead
    of looping ``move_file`` — one tool call replaces N round-trips.

    Args:
        moves: Each entry has ``source`` and ``destination`` paths
            (both relative to repo root). Parent directories of each
            destination are created automatically.

    Returns:
        Confirmation listing every move performed, or an error
        description if pre-validation failed.
    """
    resolved: list[tuple[MoveOp, Path, Path]] = []
    for op in moves:
        try:
            src = _resolve(ctx, op.source)
            dst = _resolve(ctx, op.destination)
        except PermissionError as exc:
            return str(exc)
        if not src.exists():
            return f"Source does not exist: {op.source!r}"
        resolved.append((op, src, dst))

    for _op, _src, dst in resolved:
        dst.parent.mkdir(parents=True, exist_ok=True)

    for op, src, dst in resolved:
        shutil.move(str(src), str(dst))

    lines = [f"Moved {op.source!r} → {op.destination!r}" for op, _, _ in resolved]
    return f"Moved {len(resolved)} path(s):\n" + "\n".join(lines)


async def batch_delete(ctx: RunContext, paths: list[str]) -> str:
    """Delete many files or directories in one call.

    All entries are validated up front: if any path escapes the repo
    root or doesn't exist, nothing is deleted and the error is
    returned. Directories are deleted recursively.

    Args:
        paths: Paths to delete (relative to repo root).

    Returns:
        Confirmation listing every deletion performed, or an error
        description if pre-validation failed.
    """
    resolved: list[tuple[str, Path]] = []
    for path in paths:
        try:
            target = _resolve(ctx, path)
        except PermissionError as exc:
            return str(exc)
        if not target.exists():
            return f"Path does not exist: {path!r}"
        resolved.append((path, target))

    lines: list[str] = []
    for path, target in resolved:
        if target.is_dir():
            shutil.rmtree(target)
            lines.append(f"Deleted directory {path!r}")
        else:
            target.unlink()
            lines.append(f"Deleted file {path!r}")
    return f"Deleted {len(resolved)} path(s):\n" + "\n".join(lines)


MOVE_FILE_TOOL: Tool = Tool(move_file)
DELETE_FILE_TOOL: Tool = Tool(delete_file)
BATCH_MOVE_TOOL: Tool = Tool(batch_move)
BATCH_DELETE_TOOL: Tool = Tool(batch_delete)
