"""Real-git tests for the merge helper.

These spin up a repo with two diverging branches and exercise both the
clean-merge and conflict paths of ``merge_no_commit``.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from git import Actor, Repo

from cai.git import (
    conflicted_paths,
    current_rebase_step,
    merge_no_commit,
    rebase_abort,
    rebase_continue,
    rebase_in_progress,
    rebase_onto,
    stage_all,
)


def _init(repo_root: Path) -> Repo:
    repo = Repo.init(str(repo_root), initial_branch="main")
    with repo.config_writer() as cw:
        cw.set_value("user", "name", "Test User")
        cw.set_value("user", "email", "test@example.com")
    actor = Actor("seed", "seed@example.com")
    (repo_root / "a.txt").write_text("base\n")
    repo.index.add(["a.txt"])
    repo.index.commit("seed", author=actor, committer=actor)
    return repo


def _commit_on_branch(
    repo: Repo, branch: str, path: str, content: str, message: str
) -> None:
    repo.git.checkout("-B", branch)
    actor = Actor("user", "user@example.com")
    Path(repo.working_tree_dir, path).write_text(content)
    repo.index.add([path])
    repo.index.commit(message, author=actor, committer=actor)


@pytest.fixture
def clean_diverged_repo(tmp_path: Path) -> Path:
    """Repo with 'main' and 'feature' branches that diverged without touching the same files."""
    repo_root = tmp_path / "repo"
    repo = _init(repo_root)
    _commit_on_branch(repo, "feature", "b.txt", "feature\n", "add b on feature")
    repo.git.checkout("main")
    _commit_on_branch(repo, "main", "c.txt", "main\n", "add c on main")
    repo.git.checkout("feature")
    return repo_root


@pytest.fixture
def conflicting_repo(tmp_path: Path) -> Path:
    """Repo with 'main' and 'feature' branches that both modified the same file, creating a merge conflict."""
    repo_root = tmp_path / "repo"
    repo = _init(repo_root)
    _commit_on_branch(repo, "feature", "a.txt", "feature\n", "rewrite a on feature")
    repo.git.checkout("main")
    _commit_on_branch(repo, "main", "a.txt", "main\n", "rewrite a on main")
    repo.git.checkout("feature")
    return repo_root


def test_merge_no_commit_clean(clean_diverged_repo: Path) -> None:
    repo_root = clean_diverged_repo

    conflicts = merge_no_commit(
        repo_root,
        "main",
        author_name="cai-bot",
        author_email="cai@example.com",
    )

    assert conflicts == []
    # The merge is staged but uncommitted; both files exist in the tree.
    assert (repo_root / "b.txt").exists()
    assert (repo_root / "c.txt").exists()


def test_merge_no_commit_conflict(conflicting_repo: Path) -> None:
    repo_root = conflicting_repo

    conflicts = merge_no_commit(
        repo_root,
        "main",
        author_name="cai-bot",
        author_email="cai@example.com",
    )

    assert conflicts == ["a.txt"]
    body = (repo_root / "a.txt").read_text()
    assert "<<<<<<<" in body and ">>>>>>>" in body


def test_rebase_clean(tmp_path):
    """Rebase finishes in one shot when the diverging commits don't touch the same lines."""
    repo_root = tmp_path / "repo"
    repo = _init(repo_root)
    _commit_on_branch(repo, "feature", "b.txt", "feature\n", "add b on feature")
    repo.git.checkout("main")
    _commit_on_branch(repo, "main", "c.txt", "main\n", "add c on main")
    repo.git.checkout("feature")

    finished = rebase_onto(repo_root, "main")

    assert finished is True
    assert rebase_in_progress(repo_root) is False
    # Rebased feature contains both base and feature changes.
    assert (repo_root / "b.txt").exists()
    assert (repo_root / "c.txt").exists()


def test_rebase_stops_at_conflict_then_continues(tmp_path):
    """A real conflict pauses the rebase; resolving + --continue finishes it."""
    repo_root = tmp_path / "repo"
    repo = _init(repo_root)
    _commit_on_branch(repo, "feature", "a.txt", "feature\n", "rewrite a on feature")
    repo.git.checkout("main")
    _commit_on_branch(repo, "main", "a.txt", "main\n", "rewrite a on main")
    repo.git.checkout("feature")

    finished = rebase_onto(repo_root, "main")
    assert finished is False
    assert rebase_in_progress(repo_root) is True

    # Conflicted paths and current-step info are exposed for the agent.
    assert conflicted_paths(repo_root) == ["a.txt"]
    step = current_rebase_step(repo_root)
    assert step is not None
    assert step["subject"] == "rewrite a on feature"
    assert "<<<<<<< " not in step["diff"]  # diff is the picked commit, not the working tree
    assert (repo_root / "a.txt").read_text().count("<<<<<<<") == 1

    # Resolve the conflict by hand and continue.
    (repo_root / "a.txt").write_text("feature wins\n")
    stage_all(repo_root)
    finished = rebase_continue(repo_root)

    assert finished is True
    assert rebase_in_progress(repo_root) is False
    assert (repo_root / "a.txt").read_text() == "feature wins\n"


def test_rebase_abort_returns_to_original_head(tmp_path):
    repo_root = tmp_path / "repo"
    repo = _init(repo_root)
    _commit_on_branch(repo, "feature", "a.txt", "feature\n", "rewrite a on feature")
    feature_sha = repo.head.commit.hexsha
    repo.git.checkout("main")
    _commit_on_branch(repo, "main", "a.txt", "main\n", "rewrite a on main")
    repo.git.checkout("feature")

    rebase_onto(repo_root, "main")
    assert rebase_in_progress(repo_root) is True

    rebase_abort(repo_root)

    assert rebase_in_progress(repo_root) is False
    assert repo.head.commit.hexsha == feature_sha
    # No-op when no rebase is in progress.
    rebase_abort(repo_root)


def test_current_rebase_step_returns_none_outside_rebase(tmp_path):
    repo_root = tmp_path / "repo"
    _init(repo_root)
    assert current_rebase_step(repo_root) is None
