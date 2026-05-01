"""``file_info`` — discover a file's total line count, byte size, and mtime.

The built-in ``ls`` tool reports byte size but not line count, and
the ``read_file`` tool only reveals how many lines *remain* after
a partial read — agents cannot discover the total without an
O(n) sequence of offset-guessing reads. This tool fills that gap.
"""

from __future__ import annotations

import time
from pathlib import Path

from pydantic_ai import RunContext, Tool

from cai.agents.fs_ops import _resolve


async def file_info(ctx: RunContext, path: str) -> str:
    """Return total line count, byte size, and last-modified time for a file.

    Args:
        path: Path to the file (relative to repo root).

    Returns:
        Plain-text summary with line count, byte size, and modification
        timestamp.
    """
    try:
        resolved = _resolve(ctx, path)
    except PermissionError as exc:
        return str(exc)

    if not resolved.exists():
        return f"File not found: {path!r}"

    if not resolved.is_file():
        return f"Not a regular file: {path!r}"

    stat = resolved.stat()
    byte_size = stat.st_size
    mtime = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(stat.st_mtime))

    line_count = 0
    with resolved.open("rb") as f:
        for _ in f:
            line_count += 1

    return f"{path!r}: {line_count} lines, {byte_size} bytes, modified {mtime}"


FILE_INFO_TOOL = Tool(file_info)
