"""Materialize the per-issue / per-PR workspaces for ``cai-solve``.

Issue workspace (per ``owner/repo#number``)::

    /tmp/cai-solve/<owner>/<name>/<number>/
    ├── <number>.json   # issue metadata
    ├── <number>.md     # issue body
    └── repo/           # local clone of <owner>/<name>

PR workspace (when ``cai-solve`` is invoked against a pull request)::

    /tmp/cai-solve-pr/<owner>/<name>/<pr>/
    ├── <pr>.md         # PR body
    └── repo/           # clone with the PR head branch checked out

A second invocation against the same issue or PR picks up the existing
directory as-is so in-progress agent work is preserved.
"""
from __future__ import annotations

import argparse
import re
from dataclasses import dataclass
from pathlib import Path

from cai.git import clone, set_local

from .bot import CaiBot
from .issues import pull
from .pr import get_pr_meta

WORKSPACE_ROOT = Path("/tmp/cai-solve")
PR_WORKSPACE_ROOT = Path("/tmp/cai-solve-pr")

_ISSUE_REF_RE = re.compile(r"^(?P<repo>[^/\s]+/[^/#\s]+)#(?P<number>\d+)$")


def parse_issue_ref(text: str) -> tuple[str, int] | None:
    """Parse ``owner/repo#number``. Return ``None`` on miss."""
    match = _ISSUE_REF_RE.match(text)
    if not match:
        return None
    return match["repo"], int(match["number"])


def parse_ref_and_bot(
    prog: str,
    description: str,
    ref_help: str = "Issue reference, formatted as owner/repo#number.",
) -> tuple[CaiBot, str, int]:
    """Parse ``owner/repo#number`` from CLI args and return (bot, repo, number).

    Wraps argparse, parse_issue_ref validation, and CaiBot instantiation.
    Calls ``parser.error(...)`` (which exits) on an unparseable ref.
    """
    parser = argparse.ArgumentParser(
        prog=prog,
        description=description,
    )
    parser.add_argument(
        "ref",
        help=ref_help,
    )
    args = parser.parse_args()

    parsed = parse_issue_ref(args.ref)
    if parsed is None:
        parser.error(f"expected owner/repo#number, got {args.ref!r}")
    repo, number = parsed

    return CaiBot(), repo, number


# Same wire format as an issue ref (``owner/repo#N``); aliased so callers
# can spell the intent at the call site.
parse_pr_ref = parse_issue_ref


def issue_workspace(repo: str, number: int) -> Path:
    """Return the per-issue workspace path. Pure — does not touch disk."""
    owner, name = repo.split("/", 1)
    return WORKSPACE_ROOT / owner / name / str(number)


def _configure_identity(repo_root: Path, bot: CaiBot) -> None:
    """Write ``user.name`` / ``user.email`` to the clone's local config.

    Without this, ``git rebase --continue`` (and any other path where git
    records a commit on its own) aborts with "Committer identity unknown"
    because the workspace clone inherits no identity from the host. The
    values mirror what ``cai-app-init`` writes for an interactive setup.
    """
    set_local("user.name", bot.bot_login, repo_root=repo_root)
    set_local(
        "user.email",
        f"{bot.app_id}+{bot.bot_login}@users.noreply.github.com",
        repo_root=repo_root,
    )


@dataclass(frozen=True)
class IssueWorkspace:
    root: Path
    issue_json: Path
    issue_md: Path
    repo_root: Path


def prepare_workspace(bot: CaiBot, repo: str, number: int) -> IssueWorkspace:
    """Ensure the per-issue workspace exists; return its paths.

    Idempotent: existing issue files and clone are kept as-is on a
    re-run. Docker-based workflows always start with a fresh
    ``/tmp/cai-solve``, so no explicit refresh logic is needed.
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
    _configure_identity(repo_root, bot)

    return IssueWorkspace(
        root=root,
        issue_json=json_path,
        issue_md=md_path,
        repo_root=repo_root,
    )


def pr_workspace(repo: str, number: int) -> Path:
    """Return the per-PR workspace path. Pure — does not touch disk."""
    owner, name = repo.split("/", 1)
    return PR_WORKSPACE_ROOT / owner / name / str(number)


@dataclass(frozen=True)
class PRWorkspace:
    root: Path
    repo_root: Path
    body_path: Path
    repo: str
    number: int
    head_branch: str
    base_branch: str
    title: str
    body: str


def prepare_pr_workspace(bot: CaiBot, repo: str, number: int) -> PRWorkspace:
    """Clone the repo with the PR head branch checked out.

    Idempotent: existing clones are kept as-is on a re-run. PR metadata
    (title/body/head_branch) is resolved against GitHub on every call so
    the same workspace can be reused even after a PR is renamed (the ref
    name persists, but the call would fail loudly anyway if the branch
    was deleted).
    """
    title, body, head_branch, base_branch = get_pr_meta(bot, repo, number)
    root = pr_workspace(repo, number)
    root.mkdir(parents=True, exist_ok=True)

    body_path = root / f"{number}.md"
    body_path.write_text(body)

    repo_root = root / "repo"
    if not repo_root.exists():
        clone(
            f"https://github.com/{repo}.git",
            repo_root,
            branch=head_branch,
            env={"GIT_TERMINAL_PROMPT": "0"},
        )
    _configure_identity(repo_root, bot)

    return PRWorkspace(
        root=root,
        repo_root=repo_root,
        body_path=body_path,
        repo=repo,
        number=number,
        head_branch=head_branch,
        base_branch=base_branch,
        title=title,
        body=body,
    )


def is_pull_request(bot: CaiBot, repo: str, number: int) -> bool:
    """Return True when ``number`` references a pull request, not an issue.

    GitHub stores PRs as a subtype of issues — the ``issues`` endpoint
    returns both, and PRs carry a non-null ``pull_request`` link.
    """
    issue = bot.repo(repo).get_issue(number)
    return issue.pull_request is not None
