"""Thin wrappers around the git operations cai-solve performs."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from git import Repo
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


def index_matches_head(repo_root: Path) -> bool:
    """Return True when the staged index is identical to ``HEAD``.

    Used by the rebase loop to distinguish a genuinely empty cherry-pick
    (every staged change is already on the new parent) from other
    commit-time failures (pre-commit hooks, signing) that leave the
    rebase paused with a non-empty staged tree.
    """
    repo = Repo(str(repo_root))
    try:
        repo.git.diff("--cached", "--quiet", "HEAD")
        return True
    except GitCommandError:
        return False


def commit(
    repo_root: Path,
    message: str,
    *,
    author_name: str,
    author_email: str,
) -> None:
    """Commit the staged index using ``author_name``/``author_email``.

    Goes through ``git commit`` (not GitPython's ``index.commit``) so the
    repository's pre-commit hooks fire — the regen-workflow-graphs hook
    relies on this to keep ``docs/workflows/*.md`` in sync with the live
    graph topology. When a hook modifies tracked files (the auto-fix
    path), the modifications are staged and the commit is retried once;
    a second hook-driven modification is treated as a real failure.
    """
    repo = Repo(str(repo_root))
    env = {
        "GIT_AUTHOR_NAME": author_name,
        "GIT_AUTHOR_EMAIL": author_email,
        "GIT_COMMITTER_NAME": author_name,
        "GIT_COMMITTER_EMAIL": author_email,
    }
    for attempt in range(2):
        try:
            with repo.git.custom_environment(**env):
                repo.git.commit("-m", message)
            return
        except GitCommandError:
            modified = repo.git.diff("--name-only").splitlines()
            if not modified or attempt == 1:
                raise
            repo.git.add("-A")


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


def rebase_onto(repo_root: Path, ref: str) -> bool:
    """Run ``git rebase <ref>``. Return ``True`` when the rebase finished
    cleanly with no conflicts, ``False`` when it stopped at a conflict.

    Any other failure (bad ref, dirty tree, etc.) is re-raised. The caller
    drives the conflict loop via :func:`current_rebase_step`,
    :func:`conflicted_paths`, and :func:`rebase_continue`.
    """
    repo = Repo(str(repo_root))
    try:
        repo.git.rebase(ref)
        return True
    except GitCommandError:
        if rebase_in_progress(repo_root):
            return False
        raise


def rebase_continue(repo_root: Path) -> bool:
    """Run ``git rebase --continue`` keeping the original commit message.

    ``GIT_EDITOR=true`` short-circuits the editor invocation git would
    otherwise pop up to confirm the message; ``--no-edit`` alone is not
    accepted by older git versions.

    On ``GitCommandError`` git's stderr is forwarded to the caller's
    ``sys.stderr`` so the actual reason (empty cherry-pick, pre-commit
    hook, signing failure, …) shows up in the workflow logs instead of
    being silently swallowed.
    """
    import sys

    repo = Repo(str(repo_root))
    try:
        with repo.git.custom_environment(GIT_EDITOR="true"):
            repo.git.rebase("--continue")
        return True
    except GitCommandError as exc:
        if rebase_in_progress(repo_root):
            stderr = (exc.stderr or "").strip()
            if stderr:
                print(
                    f"[rebase_continue] git stderr:\n{stderr}",
                    file=sys.stderr,
                )
            return False
        raise


def rebase_skip(repo_root: Path) -> bool:
    """Run ``git rebase --skip`` to discard the current empty commit.

    Returns True when the rebase finished, False when it stopped at another
    conflict.  Raises on any unexpected git error.
    """
    repo = Repo(str(repo_root))
    try:
        repo.git.rebase("--skip")
        return True
    except GitCommandError:
        if rebase_in_progress(repo_root):
            return False
        raise


def rebase_abort(repo_root: Path) -> None:
    """Run ``git rebase --abort`` if a rebase is in progress; no-op otherwise."""
    if not rebase_in_progress(repo_root):
        return
    Repo(str(repo_root)).git.rebase("--abort")


def rev_parse(repo_root: Path, ref: str) -> str:
    """Return the SHA that ``ref`` resolves to in ``repo_root``."""
    return Repo(str(repo_root)).git.rev_parse(ref)


def rebase_in_progress(repo_root: Path) -> bool:
    """Return True when git is mid-rebase (either rebase-merge or rebase-apply)."""
    git_dir = Path(repo_root) / ".git"
    return (git_dir / "rebase-merge").is_dir() or (git_dir / "rebase-apply").is_dir()


def conflicted_paths(repo_root: Path) -> list[str]:
    """Return paths with unmerged entries in the index."""
    repo = Repo(str(repo_root))
    out = repo.git.diff("--name-only", "--diff-filter=U").splitlines()
    return [p.strip() for p in out if p.strip()]


def current_rebase_step(repo_root: Path) -> dict[str, Any] | None:
    """Return ``{sha, subject, message, diff}`` for the commit being replayed.

    Returns ``None`` when no rebase is in progress. The diff is the patch
    the rebase is trying to apply (i.e. ``git show <sha>``); reading it
    tells the agent what change the PR author originally intended at this
    step, which is the only way to disambiguate intent from the markers.
    """
    if not rebase_in_progress(repo_root):
        return None
    repo = Repo(str(repo_root))
    sha = repo.git.rev_parse("REBASE_HEAD")
    commit = repo.commit(sha)
    diff = repo.git.show(sha)
    return {
        "sha": sha,
        "subject": commit.summary,
        "message": commit.message,
        "diff": diff,
    }


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
