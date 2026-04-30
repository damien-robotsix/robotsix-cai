"""Real-git tests for the merge helper.

These spin up a repo with two diverging branches and exercise both the
clean-merge and conflict paths of ``merge_no_commit``.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from git import Actor, Repo
from git.exc import GitCommandError

from cai.git import (
    commit,
    conflicted_paths,
    current_rebase_step,
    index_matches_head,
    merge_no_commit,
    rebase_abort,
    rebase_continue,
    rebase_in_progress,
    rebase_onto,
    rev_parse,
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


def test_rev_parse_resolves_refs(tmp_path):
    repo_root = tmp_path / "repo"
    repo = _init(repo_root)
    seed_sha = repo.head.commit.hexsha
    _commit_on_branch(repo, "feature", "b.txt", "feature\n", "add b")
    feature_sha = repo.head.commit.hexsha

    assert rev_parse(repo_root, "HEAD") == feature_sha
    assert rev_parse(repo_root, "main") == seed_sha
    assert rev_parse(repo_root, "feature") == feature_sha


def _install_pre_commit_hook(repo_root: Path, body: str) -> None:
    hook = repo_root / ".git" / "hooks" / "pre-commit"
    hook.write_text(body)
    hook.chmod(0o755)


def test_commit_runs_pre_commit_hook_and_succeeds_when_clean(tmp_path):
    """A pre-commit hook that does nothing must let the commit through."""
    repo_root = tmp_path / "repo"
    repo = _init(repo_root)
    marker = repo_root / "hook_ran"
    _install_pre_commit_hook(
        repo_root,
        f"#!/usr/bin/env bash\ntouch {marker}\nexit 0\n",
    )

    (repo_root / "x.txt").write_text("x\n")
    repo.git.add("x.txt")
    commit(repo_root, "add x", author_name="cai-bot", author_email="cai@example.com")

    assert marker.exists(), "pre-commit hook did not run"
    assert repo.head.commit.message.strip() == "add x"
    assert repo.head.commit.author.name == "cai-bot"


def test_commit_retries_when_hook_modifies_files(tmp_path):
    """When a hook regenerates a tracked file, the modification is staged
    and the commit retries — mirrors the regen-workflow-graphs flow."""
    repo_root = tmp_path / "repo"
    repo = _init(repo_root)
    (repo_root / "gen.txt").write_text("stale\n")
    repo.git.add("gen.txt")
    repo.index.commit("seed gen.txt")

    flag = repo_root / ".hook_already_ran"
    _install_pre_commit_hook(
        repo_root,
        # First invocation rewrites gen.txt and exits 1; subsequent
        # invocations are no-ops so the retry can succeed.
        f"""#!/usr/bin/env bash
if [ -f {flag} ]; then exit 0; fi
touch {flag}
echo regenerated > {repo_root}/gen.txt
exit 1
""",
    )

    (repo_root / "y.txt").write_text("y\n")
    repo.git.add("y.txt")
    commit(repo_root, "add y", author_name="cai-bot", author_email="cai@example.com")

    assert (repo_root / "gen.txt").read_text() == "regenerated\n"
    # The committed tree contains both the original staged change and the
    # hook's regeneration.
    assert "y.txt" in repo.head.commit.tree
    assert repo.head.commit.tree["gen.txt"].data_stream.read().decode() == "regenerated\n"


def test_commit_raises_when_hook_keeps_failing(tmp_path):
    """A hook that never succeeds (e.g. lint failure) bubbles up."""
    repo_root = tmp_path / "repo"
    repo = _init(repo_root)
    _install_pre_commit_hook(repo_root, "#!/usr/bin/env bash\nexit 1\n")

    (repo_root / "z.txt").write_text("z\n")
    repo.git.add("z.txt")
    with pytest.raises(GitCommandError):
        commit(repo_root, "add z", author_name="cai-bot", author_email="cai@example.com")


def test_index_matches_head(tmp_path):
    """``index_matches_head`` is True iff the staged tree equals HEAD."""
    repo_root = tmp_path / "repo"
    repo = _init(repo_root)

    # Fresh repo: index matches HEAD.
    assert index_matches_head(repo_root) is True

    # Untracked file: still matches (not staged).
    (repo_root / "u.txt").write_text("u\n")
    assert index_matches_head(repo_root) is True

    # Staged change: no longer matches.
    repo.index.add(["u.txt"])
    assert index_matches_head(repo_root) is False

    # Modify-and-stage on a tracked file also flips the result.
    repo.index.commit("add u")
    assert index_matches_head(repo_root) is True
    (repo_root / "u.txt").write_text("u2\n")
    repo.git.add("u.txt")
    assert index_matches_head(repo_root) is False
