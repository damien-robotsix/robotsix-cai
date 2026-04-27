"""Helpers for reading and writing repository-local git config.

Wraps ``git config --local`` via GitPython so callers don't need to
shell out themselves. ``repo_root`` defaults to the current working
directory to match the behaviour of the CLI.
"""
from __future__ import annotations

from pathlib import Path

from git import GitCommandError, Repo


def _repo(repo_root: Path | None) -> Repo:
    return Repo(str(repo_root) if repo_root else ".")


def set_local(key: str, value: str, *, repo_root: Path | None = None) -> None:
    """Set a single-valued repository-local config entry."""
    _repo(repo_root).git.config("--local", key, value)


def add_local(key: str, value: str, *, repo_root: Path | None = None) -> None:
    """Append a value to a multi-valued repository-local config entry."""
    _repo(repo_root).git.config("--local", "--add", key, value)


def unset_all_local(key: str, *, repo_root: Path | None = None) -> None:
    """Remove every value for ``key``. Silent no-op if the key is absent."""
    try:
        _repo(repo_root).git.config("--local", "--unset-all", key)
    except GitCommandError:
        # `git config --unset-all` exits 5 when the key isn't set; we
        # treat that as success so callers can use this idempotently.
        pass
