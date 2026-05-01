from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from cai.github.issues import IssueMeta
from cai.workflows.state import ImplementOutput, IssueState
from cai.workflows.test_runner import TestNode, _python_changes


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=str(repo),
        check=True,
        capture_output=True,
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "config", "user.email", "t@t")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / "seed.txt").write_text("seed")
    _git(tmp_path, "add", "seed.txt")
    _git(tmp_path, "commit", "-q", "-m", "seed")
    return tmp_path


def test_python_changes_detects_modified_py(repo: Path):
    f = repo / "a.py"
    f.write_text("x = 1\n")
    _git(repo, "add", "a.py")
    _git(repo, "commit", "-q", "-m", "add a.py")
    f.write_text("x = 2\n")
    assert _python_changes(repo) == ["a.py"]


def test_python_changes_detects_untracked_py(repo: Path):
    (repo / "new.py").write_text("# new\n")
    assert _python_changes(repo) == ["new.py"]


def test_python_changes_ignores_non_py(repo: Path):
    (repo / "doc.md").write_text("# docs\n")
    (repo / "config.yml").write_text("k: v\n")
    assert _python_changes(repo) == []


def test_python_changes_handles_paths_with_spaces(repo: Path):
    sub = repo / "src dir"
    sub.mkdir()
    (sub / "weird name.py").write_text("z = 1\n")
    assert _python_changes(repo) == ["src dir/weird name.py"]


def test_python_changes_handles_renames(repo: Path):
    f = repo / "old.py"
    f.write_text("x = 1\n")
    _git(repo, "add", "old.py")
    _git(repo, "commit", "-q", "-m", "add old.py")
    _git(repo, "mv", "old.py", "new.py")
    paths = _python_changes(repo)
    assert paths == ["new.py"]


def _state(repo_root: Path) -> IssueState:
    body = repo_root / "body.md"
    body.write_text("body")
    meta = IssueMeta(repo="o/r", number=99, title="t")
    bot = MagicMock()
    bot.token_for.return_value = "tok"
    s = IssueState(
        bot=bot,
        meta=meta,
        body_path=body,
        repo_root=repo_root,
        branch_name="feature/x",
    )
    s.new_meta = meta
    s.implement_output = ImplementOutput(
        summary="docs only",
        commit_message="docs: tweak",
        required_checks=["documentation"],
    )
    return s


def _run(node, state):
    ctx = MagicMock()
    ctx.state = state
    return asyncio.run(node.run(ctx))


@patch("cai.workflows.test_runner._test_writer_agent")
@patch("cai.workflows.test_runner._run_tests")
def test_test_node_skips_writer_when_no_python_changes(
    mock_run_tests, mock_agent, repo: Path
):
    """Docs-only change: agent must not be invoked, but pytest sanity still runs."""
    (repo / "doc.md").write_text("# new docs\n")
    mock_run_tests.return_value = (True, "")
    state = _state(repo)

    _run(TestNode(), state)

    mock_agent.assert_not_called()
    mock_run_tests.assert_called_once_with(repo)


@patch("cai.workflows.test_runner.repo_deps")
@patch("cai.workflows.test_runner._test_writer_agent")
@patch("cai.workflows.test_runner._run_tests")
def test_test_node_invokes_writer_when_python_changed(
    mock_run_tests, mock_agent, mock_deps, repo: Path
):
    (repo / "feature.py").write_text("def f(): return 1\n")
    mock_run_tests.return_value = (True, "")
    mock_deps.return_value = MagicMock()

    instance = MagicMock()
    mock_agent.return_value = instance

    async def fake_run(*args, **kwargs):
        result = MagicMock()
        result.output = MagicMock()
        return result

    instance.run.side_effect = fake_run

    _run(TestNode(), _state(repo))

    instance.run.assert_called_once()
    args, kwargs = instance.run.call_args
    prompt = args[0]
    assert "feature.py" in prompt
