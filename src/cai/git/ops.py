"""Thin wrappers around the git operations cai-solve performs."""
from __future__ import annotations

from pathlib import Path
from typing import Mapping

from git import Actor, Repo
from git.exc import GitCommandError


def clone(
    url: str,
    dest: Path,
    *,
    branch: str | None = None,
    env: Mapping[str, str] | None = None,
) -> Repo:
    """Clone ``url`` into ``dest`` and return the new ``Repo``.

    When ``branch`` is given, the clone checks out that ref directly.
    """
    kwargs: dict = {"env": dict(env)} if env else {}
    if branch is not None:
        kwargs["branch"] = branch
    return Repo.clone_from(url, str(dest), **kwargs)


def checkout_branch(repo_root: Path, branch_name: str) -> None:
    """Create and check out a new branch at HEAD."""
    Repo(str(repo_root)).git.checkout("-b", branch_name)


def stage_all(repo_root: Path) -> None:
    """Stage every change in the working tree, including untracked files."""
    Repo(str(repo_root)).git.add("-A")


def commit(
    repo_root: Path,
    message: str,
    *,
    author_name: str,
    author_email: str,
) -> None:
    """Commit the staged index using ``author_name``/``author_email``."""
    actor = Actor(author_name, author_email)
    Repo(str(repo_root)).index.commit(message, author=actor, committer=actor)


def fetch(
    repo_root: Path,
    remote: str = "origin",
    *,
    env: Mapping[str, str] | None = None,
) -> None:
    """Run ``git fetch <remote>``."""
    repo = Repo(str(repo_root))
    if env:
        with repo.git.custom_environment(**env):
            repo.git.fetch(remote)
    else:
        repo.git.fetch(remote)


def merge_no_commit(
    repo_root: Path,
    ref: str,
    *,
    author_name: str,
    author_email: str,
) -> list[str]:
    """Merge ``ref`` into the current branch with ``--no-ff --no-commit``.

    Returns the list of conflicted paths (empty when the merge succeeds
    cleanly, in which case the caller still needs to commit). When there
    are conflicts the working tree is left with conflict markers staged
    via ``git add -A`` by the caller; this helper does not commit.
    """
    repo = Repo(str(repo_root))
    with repo.git.custom_environment(
        GIT_AUTHOR_NAME=author_name,
        GIT_AUTHOR_EMAIL=author_email,
        GIT_COMMITTER_NAME=author_name,
        GIT_COMMITTER_EMAIL=author_email,
    ):
        try:
            repo.git.merge(ref, "--no-ff", "--no-commit")
            return []
        except GitCommandError:
            # Conflicts are reported via ``git diff --name-only --diff-filter=U``;
            # any other failure we re-raise.
            unmerged = repo.git.diff("--name-only", "--diff-filter=U").splitlines()
            if not unmerged:
                raise
            return [path.strip() for path in unmerged if path.strip()]


def push_branch(
    repo_root: Path,
    remote_url: str,
    branch_name: str,
    *,
    env: Mapping[str, str] | None = None,
) -> None:
    """Push ``branch_name`` to ``remote_url`` using a full refspec.

    The push is **forced** (refspec prefixed with ``+``). cai-solve owns
    the ``cai/solve-<n>`` namespace: when an earlier run's PR is closed,
    its branch sticks around on the remote and a re-run's commit graph
    diverges from it. A fast-forward push then fails on
    ``non-fast-forward`` and the whole workflow aborts even though the
    new work is fine. The orphaned branch isn't load-bearing — closed
    PRs reference their commits by SHA, not by the moving ref — so
    overwriting it is the right behaviour.
    """
    repo = Repo(str(repo_root))
    refspec = f"+refs/heads/{branch_name}:refs/heads/{branch_name}"
    if env:
        with repo.git.custom_environment(**env):
            repo.git.push(remote_url, refspec)
    else:
        repo.git.push(remote_url, refspec)
