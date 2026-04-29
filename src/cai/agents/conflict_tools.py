"""Targeted conflict-marker resolution tools for the resolve_step agent.

Instead of writing an entire file from scratch (error-prone) or using
edit_file with exact conflict-marker strings (brittle), these tools let
the agent work block-by-block:

  conflict_list  — show every conflict block in a file with an index
  conflict_resolve — replace one block with "ours", "theirs", or custom text
"""
from __future__ import annotations

from pathlib import Path

from pydantic_ai import RunContext, Tool


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

def _parse_conflicts(lines: list[str]) -> list[dict]:
    """Return one dict per conflict block found in ``lines``.

    Each dict has keys:
        index  — zero-based position among all blocks in the file
        start  — line index of the <<<<<<< line
        sep    — line index of the ======= line
        end    — line index of the >>>>>>> line
        ours   — joined text of the HEAD side (no trailing newline stripped)
        theirs — joined text of the incoming side
    """
    blocks = []
    i = 0
    while i < len(lines):
        if lines[i].startswith("<<<<<<<"):
            start = i
            sep = None
            i += 1
            while i < len(lines):
                if lines[i].startswith("=======") and sep is None:
                    sep = i
                    i += 1
                elif lines[i].startswith(">>>>>>>"):
                    if sep is not None:
                        blocks.append({
                            "index": len(blocks),
                            "start": start,
                            "sep": sep,
                            "end": i,
                            "ours": "".join(lines[start + 1 : sep]),
                            "theirs": "".join(lines[sep + 1 : i]),
                        })
                    i += 1
                    break
                else:
                    i += 1
        else:
            i += 1
    return blocks


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

def _root(ctx: RunContext) -> Path:
    return Path(ctx.deps.backend.root_dir).resolve()


def _resolve_path(ctx: RunContext, rel: str) -> Path | None:
    root = _root(ctx)
    full = (root / rel).resolve()
    if not str(full).startswith(str(root)):
        return None
    return full


async def conflict_list(ctx: RunContext, path: str) -> str:
    """List all conflict blocks in a file with their index and both sides.

    Args:
        path: File path relative to the repository root.
    """
    full = _resolve_path(ctx, path)
    if full is None:
        return f"Permission denied: {path!r} escapes repository root"
    if not full.exists():
        return f"File not found: {path!r}"

    lines = full.read_text(errors="ignore").splitlines(keepends=True)
    blocks = _parse_conflicts(lines)

    if not blocks:
        return f"No conflict markers in {path!r}"

    out: list[str] = [f"{len(blocks)} conflict(s) in {path!r}"]
    for b in blocks:
        ours_preview = b["ours"][:300] + ("…" if len(b["ours"]) > 300 else "")
        theirs_preview = b["theirs"][:300] + ("…" if len(b["theirs"]) > 300 else "")
        out.append(
            f"\n[{b['index']}] line {b['start'] + 1}"
            f"\n  OURS (HEAD):\n{_indent(ours_preview)}"
            f"\n  THEIRS:\n{_indent(theirs_preview)}"
        )
    return "\n".join(out)


def _indent(text: str, prefix: str = "    ") -> str:
    if not text:
        return f"{prefix}(empty)"
    return "\n".join(prefix + l for l in text.splitlines())


async def conflict_resolve(
    ctx: RunContext,
    path: str,
    index: int,
    resolution: str,
) -> str:
    """Resolve one conflict block in a file.

    Args:
        path: File path relative to the repository root.
        index: Zero-based index of the conflict block (from conflict_list).
        resolution: "ours" to keep HEAD, "theirs" to take the incoming side,
            or any other string to use as the literal replacement content.
    """
    full = _resolve_path(ctx, path)
    if full is None:
        return f"Permission denied: {path!r} escapes repository root"
    if not full.exists():
        return f"File not found: {path!r}"

    lines = full.read_text(errors="ignore").splitlines(keepends=True)
    blocks = _parse_conflicts(lines)

    if not blocks:
        return f"No conflict markers in {path!r}"
    if index < 0 or index >= len(blocks):
        return f"Index {index} out of range — {len(blocks)} conflict(s) found (0…{len(blocks) - 1})"

    b = blocks[index]
    if resolution == "ours":
        replacement_lines = lines[b["start"] + 1 : b["sep"]]
        label = "ours"
    elif resolution == "theirs":
        replacement_lines = lines[b["sep"] + 1 : b["end"]]
        label = "theirs"
    else:
        # Normalise: ensure each logical line ends with a newline to match the
        # surrounding file lines so no newlines are dropped or doubled.
        raw = resolution if resolution.endswith("\n") else resolution + "\n"
        replacement_lines = raw.splitlines(keepends=True)
        label = "custom"

    new_lines = lines[: b["start"]] + replacement_lines + lines[b["end"] + 1 :]
    full.write_text("".join(new_lines))

    remaining = len(_parse_conflicts(new_lines))
    suffix = f", {remaining} conflict(s) remaining" if remaining else ", file is now clean"
    return f"Resolved conflict {index} in {path!r} ({label}){suffix}"


CONFLICT_LIST_TOOL = Tool(conflict_list)
CONFLICT_RESOLVE_TOOL = Tool(conflict_resolve)
