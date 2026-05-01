"""Read-only git history tools for the explore agent.

Four tools expose git history to AI agents via GitPython:

- ``git_log``: recent commits, optionally filtered by path or date
- ``git_diff``: unified diff between two refs
- ``git_blame``: per-line authorship for a file
- ``git_show``: full commit metadata + diff for a single commit

All four are read-only by construction — they only call GitPython
read operations (log, diff, blame, show). No write, commit, push,
rebase, or any other mutating git command is exposed.
"""

from __future__ import annotations

from git import Repo
from pydantic_ai import RunContext, Tool


def _repo(ctx: RunContext) -> Repo:
    """Return the GitPython Repo for the backend root."""
    return Repo(str(ctx.deps.backend.root_dir))


async def git_log(
    ctx: RunContext,
    max_count: int = 10,
    path: str | None = None,
    since: str | None = None,
) -> str:
    """Pretty-formatted log of recent commits, optionally filtered by path or date.

    Args:
        max_count: Maximum number of commits to show (default 10).
        path: Optional repo-relative path to filter commits by.
        since: Optional date string (e.g. \"2024-01-01\", \"2 weeks ago\")
            to show commits more recent than this date.

    Returns:
        Pretty one-line-per-commit log (hash, date, author, subject).
    """
    repo = _repo(ctx)
    args = [f"-{max_count}", "--format=%h %ad %an: %s", "--date=short"]
    if since:
        args.append(f"--since={since}")
    if path:
        args.append("--")
        args.append(path)
    result = repo.git.log(*args)
    return result if result.strip() else "(no commits)"


async def git_diff(
    ctx: RunContext,
    commit_range: str,
) -> str:
    """Unified diff between two refs.

    Args:
        commit_range: Range expression like \"HEAD~3..HEAD\",
            \"main..feature\", or a single ref like \"HEAD~1\".

    Returns:
        Unified diff output.
    """
    repo = _repo(ctx)
    result = repo.git.diff(commit_range)
    return result if result.strip() else "(no changes)"


async def git_blame(
    ctx: RunContext,
    path: str,
    start_line: int | None = None,
    end_line: int | None = None,
) -> str:
    """Per-line authorship for a file.

    Args:
        path: Repo-relative path to the file to blame.
        start_line: Optional start line (1-based, inclusive).
        end_line: Optional end line (1-based, inclusive).

    Returns:
        Per-line blame output (short commit hash, author, date,
        line number, and line content).
    """
    repo = _repo(ctx)
    args: list[str] = []
    if start_line is not None and end_line is not None:
        args.extend(["-L", f"{start_line},{end_line}"])
    elif start_line is not None:
        args.extend(["-L", f"{start_line},"])
    args.append(path)
    result = repo.git.blame(*args)
    return result if result.strip() else "(no output)"


async def git_show(
    ctx: RunContext,
    commit: str,
) -> str:
    """Full commit metadata + diff for a single commit.

    Args:
        commit: A commit reference (SHA, branch, tag, or relative
            ref like \"HEAD~1\").

    Returns:
        Full commit information: author, date, message, and unified
        diff.
    """
    repo = _repo(ctx)
    result = repo.git.show(commit)
    return result if result.strip() else "(no output)"


GIT_LOG_TOOL = Tool(git_log)
GIT_DIFF_TOOL = Tool(git_diff)
GIT_BLAME_TOOL = Tool(git_blame)
GIT_SHOW_TOOL = Tool(git_show)
