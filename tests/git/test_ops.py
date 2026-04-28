"""Real-git tests for the merge helper.

These spin up a repo with two diverging branches and exercise both the
clean-merge and conflict paths of ``merge_no_commit``.
"""
from __future__ import annotations

from pathlib import Path

from git import Actor, Repo

from cai.git import merge_no_commit


def _init(repo_root: Path) -> Repo:
    repo = Repo.init(str(repo_root), initial_branch="main")
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


def test_merge_no_commit_clean(tmp_path):
    repo_root = tmp_path / "repo"
    repo = _init(repo_root)
    _commit_on_branch(repo, "feature", "b.txt", "feature\n", "add b on feature")
    repo.git.checkout("main")
    _commit_on_branch(repo, "main", "c.txt", "main\n", "add c on main")
    repo.git.checkout("feature")

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


def test_merge_no_commit_conflict(tmp_path):
    repo_root = tmp_path / "repo"
    repo = _init(repo_root)
    _commit_on_branch(repo, "feature", "a.txt", "feature\n", "rewrite a on feature")
    repo.git.checkout("main")
    _commit_on_branch(repo, "main", "a.txt", "main\n", "rewrite a on main")
    repo.git.checkout("feature")

    conflicts = merge_no_commit(
        repo_root,
        "main",
        author_name="cai-bot",
        author_email="cai@example.com",
    )

    assert conflicts == ["a.txt"]
    body = (repo_root / "a.txt").read_text()
    assert "<<<<<<<" in body and ">>>>>>>" in body
