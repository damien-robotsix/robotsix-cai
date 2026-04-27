"""Thin wrappers around the git operations cai-solve performs."""
from __future__ import annotations

from pathlib import Path
from typing import Mapping

from git import Actor, Repo


def clone(url: str, dest: Path, *, env: Mapping[str, str] | None = None) -> Repo:
    """Clone ``url`` into ``dest`` and return the new ``Repo``."""
    return Repo.clone_from(url, str(dest), env=dict(env) if env else None)


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


def push_branch(
    repo_root: Path,
    remote_url: str,
    branch_name: str,
    *,
    env: Mapping[str, str] | None = None,
) -> None:
    """Push ``branch_name`` to ``remote_url`` using a full refspec."""
    repo = Repo(str(repo_root))
    refspec = f"refs/heads/{branch_name}:refs/heads/{branch_name}"
    if env:
        with repo.git.custom_environment(**env):
            repo.git.push(remote_url, refspec)
    else:
        repo.git.push(remote_url, refspec)
