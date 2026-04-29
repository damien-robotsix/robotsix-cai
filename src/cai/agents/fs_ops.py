"""``move_file`` and ``delete_file`` — custom filesystem tools.

The built-in deep-console toolset covers read/write/edit but lacks
rename/move and delete.  These tools fill that gap with the same
path-validation approach as the backend: paths are resolved relative to
the backend root and constrained to stay within it.
"""
from __future__ import annotations

import shutil
from pathlib import Path

from pydantic_ai import RunContext, Tool


def _resolve(ctx: RunContext, rel_path: str) -> Path:
    root = Path(ctx.deps.backend.root_dir).resolve()
    resolved = (root / rel_path).resolve()
    if not str(resolved).startswith(str(root)):
        raise PermissionError(f"Path {rel_path!r} escapes repository root")
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


MOVE_FILE_TOOL = Tool(move_file)
DELETE_FILE_TOOL = Tool(delete_file)
