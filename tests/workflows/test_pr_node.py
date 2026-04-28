from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from pydantic_graph import End

from cai.github.issues import IssueMeta
from cai.workflows.pr import PRNode
from cai.workflows.state import ImplementOutput, IssueState


@pytest.fixture
def state(tmp_path: Path) -> IssueState:
    body = tmp_path / "body.md"
    body.write_text("body")
    meta = IssueMeta(repo="o/r", number=99, title="t")
    bot = MagicMock()
    bot.token_for.return_value = "tok"
    s = IssueState(
        bot=bot,
        meta=meta,
        body_path=body,
        repo_root=tmp_path,
        branch_name="feature/x",
    )
    s.new_meta = meta
    s.implement_output = ImplementOutput(
        summary="s", commit_message="c", required_checks=[], replies=[]
    )
    return s


def _run(node, state):
    ctx = MagicMock()
    ctx.state = state
    return asyncio.run(node.run(ctx))


@patch("cai.workflows.pr.create_pull_request")
@patch("cai.workflows.pr.push_branch")
@patch("cai.workflows.pr._has_staged_changes", return_value=True)
@patch("cai.workflows.pr.commit")
@patch("cai.workflows.pr.stage_all")
def test_pr_node_existing_pr_skips_create(
    mock_stage, mock_commit, mock_dirty, mock_push, mock_create, state
):
    # PR-mode conflict resolution: pr_number set, no review threads.
    state.pr_number = 99

    result = _run(PRNode(), state)

    assert isinstance(result, End)
    mock_push.assert_called_once()
    mock_create.assert_not_called()


@patch("cai.workflows.pr.create_pull_request", return_value="https://pr/1")
@patch("cai.workflows.pr.push_branch")
@patch("cai.workflows.pr._has_staged_changes", return_value=True)
@patch("cai.workflows.pr.commit")
@patch("cai.workflows.pr.stage_all")
def test_pr_node_issue_mode_creates_pr(
    mock_stage, mock_commit, mock_dirty, mock_push, mock_create, state
):
    # Issue mode: no pr_number, no threads → opens a new PR.
    result = _run(PRNode(), state)

    assert isinstance(result, End)
    mock_push.assert_called_once()
    mock_create.assert_called_once()
    assert state.pr_url == "https://pr/1"
