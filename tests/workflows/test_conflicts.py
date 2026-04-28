from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from cai.github.repo import PRWorkspace
from cai.workflows.conflicts import _conflict_body, solve_conflicts


@pytest.fixture
def workspace(tmp_path: Path) -> PRWorkspace:
    body = tmp_path / "42.md"
    body.write_text("original PR body")
    return PRWorkspace(
        root=tmp_path,
        repo_root=tmp_path / "repo",
        body_path=body,
        repo="owner/name",
        number=42,
        head_branch="feature/x",
        base_branch="main",
        title="Add x",
        body="original PR body",
    )


def test_conflict_body_lists_files():
    out = _conflict_body("main", "feature/x", ["src/a.py", "docs/b.md"])
    assert "`src/a.py`" in out
    assert "`docs/b.md`" in out
    assert "main" in out and "feature/x" in out
    assert "<<<<<<<" in out  # marker explanation included


@patch("cai.workflows.conflicts.solve_graph")
@patch("cai.workflows.conflicts.push_branch")
@patch("cai.workflows.conflicts.commit")
@patch("cai.workflows.conflicts.stage_all")
@patch("cai.workflows.conflicts.merge_no_commit")
@patch("cai.workflows.conflicts.fetch")
def test_solve_conflicts_clean_merge_skips_graph(
    mock_fetch,
    mock_merge,
    mock_stage,
    mock_commit,
    mock_push,
    mock_graph,
    workspace,
):
    mock_merge.return_value = []  # no conflicts
    bot = MagicMock()
    bot.token_for.return_value = "tok"

    result = solve_conflicts(bot, workspace)

    assert result == {"mode": "clean", "conflicted_files": []}
    mock_fetch.assert_called_once()
    mock_merge.assert_called_once()
    mock_stage.assert_called_once()
    mock_commit.assert_called_once()
    mock_push.assert_called_once()
    mock_graph.run_sync.assert_not_called()
    # Synthetic body should NOT be written when merge is clean.
    assert workspace.body_path.read_text() == "original PR body"


@patch("cai.workflows.conflicts.langfuse_workflow")
@patch("cai.workflows.conflicts.solve_graph")
@patch("cai.workflows.conflicts.push_branch")
@patch("cai.workflows.conflicts.commit")
@patch("cai.workflows.conflicts.stage_all")
@patch("cai.workflows.conflicts.merge_no_commit")
@patch("cai.workflows.conflicts.fetch")
def test_solve_conflicts_with_conflicts_runs_graph(
    mock_fetch,
    mock_merge,
    mock_stage,
    mock_commit,
    mock_push,
    mock_graph,
    mock_langfuse,
    workspace,
):
    mock_merge.return_value = ["src/a.py"]
    bot = MagicMock()
    bot.token_for.return_value = "tok"

    result = solve_conflicts(bot, workspace)

    assert result == {"mode": "resolved", "conflicted_files": ["src/a.py"]}
    mock_graph.run_sync.assert_called_once()
    # Synthetic body should be written for the agent.
    rewritten = workspace.body_path.read_text()
    assert "src/a.py" in rewritten
    assert "Resolve merge conflicts" in rewritten
