"""Materialize the per-issue workspace for ``cai-solve``.

Layout (deterministic per ``owner/repo#number``)::

    /tmp/cai-solve/<owner>/<name>/<number>/
    ├── <number>.json   # issue metadata
    ├── <number>.md     # issue body
    └── repo/           # local clone of <owner>/<name>

A second invocation against the same issue picks up the existing
directory as-is so in-progress agent work is preserved.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from cai.git import clone

from .bot import CaiBot
from .issues import pull

WORKSPACE_ROOT = Path("/tmp/cai-solve")

_ISSUE_REF_RE = re.compile(r"^(?P<repo>[^/\s]+/[^/#\s]+)#(?P<number>\d+)$")


def parse_issue_ref(text: str) -> tuple[str, int] | None:
    """Parse ``owner/repo#number``. Return ``None`` on miss."""
    match = _ISSUE_REF_RE.match(text)
    if not match:
        return None
    return match["repo"], int(match["number"])


def issue_workspace(repo: str, number: int) -> Path:
    """Return the per-issue workspace path. Pure — does not touch disk."""
    owner, name = repo.split("/", 1)
    return WORKSPACE_ROOT / owner / name / str(number)


@dataclass(frozen=True)
class IssueWorkspace:
    root: Path
    issue_json: Path
    issue_md: Path
    repo_root: Path


def prepare_workspace(bot: CaiBot, repo: str, number: int) -> IssueWorkspace:
    """Ensure the per-issue workspace exists; return its paths.

    Idempotent: existing issue files and clone are kept as-is on a
    re-run. TODO: when reusing an existing workspace, decide how to
    refresh stale state (fetch+reset, stash, branch hygiene) — for now
    we trust the on-disk copy.
    """
    root = issue_workspace(repo, number)
    root.mkdir(parents=True, exist_ok=True)

    json_path = root / f"{number}.json"
    md_path = root / f"{number}.md"
    if not json_path.exists():
        pull(bot, repo, number, root)

    repo_root = root / "repo"
    if not repo_root.exists():
        # GIT_TERMINAL_PROMPT=0 makes a missing credential helper fail
        # fast instead of hanging on an interactive password prompt.
        clone(
            f"https://github.com/{repo}.git",
            repo_root,
            env={"GIT_TERMINAL_PROMPT": "0"},
        )

    return IssueWorkspace(
        root=root,
        issue_json=json_path,
        issue_md=md_path,
        repo_root=repo_root,
    )
