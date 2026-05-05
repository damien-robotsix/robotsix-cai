from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from cai.github.issues import IssueMeta
from cai.workflows.comment import CommentNode
from cai.workflows.state import IssueState


def _state_with_body(tmp_path: Path, body: str = "## Refined Issue\n\nBody.") -> IssueState:
    body_path = tmp_path / "42.md"
    body_path.write_text(body)
    bot = MagicMock()
    meta = IssueMeta(repo="o/r", number=42, title="Audit", labels=["cai:raised", "cai:sub-issue"])
    state = IssueState(
        bot=bot,
        meta=meta,
        body_path=body_path,
        repo_root=tmp_path / "repo",
    )
    state.new_meta = meta.model_copy()
    return state


def _wire_repo_and_issue(state: IssueState, source_labels: list[str]):
    """Set up bot.repo(...).get_issue(...) to return distinct target/source mocks.

    The order of get_issue() calls in CommentNode is:
      1. target_issue (for create_comment)
      2. source_issue (for label edit + close)
    """
    target_issue = MagicMock()
    target_comment = MagicMock()
    target_comment.html_url = "https://github.com/o/r/issues/100#issuecomment-1"
    target_issue.create_comment.return_value = target_comment

    source_issue = MagicMock()
    label_mocks = []
    for name in source_labels:
        m = MagicMock()
        m.name = name
        label_mocks.append(m)
    source_issue.labels = label_mocks

    repo_obj = MagicMock()
    repo_obj.get_issue.side_effect = [target_issue, source_issue]
    state.bot.repo.return_value = repo_obj
    return repo_obj, target_issue, source_issue, target_comment


def test_subissue_posts_comment_on_parent(tmp_path: Path):
    state = _state_with_body(tmp_path, "## Refined Issue\n\nFindings.")
    repo_obj, target_issue, source_issue, target_comment = _wire_repo_and_issue(
        state, source_labels=["cai:raised", "cai:sub-issue"]
    )

    with patch("cai.workflows.comment.get_parent_issue", return_value=100), \
         patch("cai.workflows.comment.ensure_labels"):
        result = asyncio.run(CommentNode().run(MagicMock(state=state)))

    # Posted on the parent (#100), not the source (#42).
    assert repo_obj.get_issue.call_args_list[0].args[0] == 100
    target_issue.create_comment.assert_called_once_with("## Refined Issue\n\nFindings.")
    assert state.comment_url == target_comment.html_url

    # Source issue closed with cai:resolved, cai:raised stripped.
    source_issue.edit.assert_called_once()
    edited = source_issue.edit.call_args.kwargs
    assert edited["state"] == "closed"
    assert edited["state_reason"] == "completed"
    assert "cai:resolved" in edited["labels"]
    assert "cai:raised" not in edited["labels"]
    assert "cai:sub-issue" in edited["labels"]  # preserved

    # End reached with new_meta carrying the closed state.
    assert state.new_meta.state == "closed"
    assert state.new_meta.state_reason == "completed"
    assert "cai:resolved" in state.new_meta.labels
    assert result.data is state.new_meta


def test_standalone_issue_posts_on_self(tmp_path: Path):
    state = _state_with_body(tmp_path)
    repo_obj, target_issue, source_issue, target_comment = _wire_repo_and_issue(
        state, source_labels=["cai:raised"]
    )

    with patch("cai.workflows.comment.get_parent_issue", return_value=None), \
         patch("cai.workflows.comment.ensure_labels"):
        asyncio.run(CommentNode().run(MagicMock(state=state)))

    # Both calls hit the same number (the source itself).
    calls = [c.args[0] for c in repo_obj.get_issue.call_args_list]
    assert calls == [42, 42]
    target_issue.create_comment.assert_called_once()


def test_idempotent_resolved_label(tmp_path: Path):
    """If cai:resolved is somehow already on the issue, don't duplicate it."""
    state = _state_with_body(tmp_path)
    repo_obj, target_issue, source_issue, _ = _wire_repo_and_issue(
        state, source_labels=["cai:resolved"]
    )

    with patch("cai.workflows.comment.get_parent_issue", return_value=None), \
         patch("cai.workflows.comment.ensure_labels"):
        asyncio.run(CommentNode().run(MagicMock(state=state)))

    edited_labels = source_issue.edit.call_args.kwargs["labels"]
    assert edited_labels.count("cai:resolved") == 1


def test_ensure_labels_called(tmp_path: Path):
    """cai:resolved must exist before adding it; ensure_labels guarantees that."""
    state = _state_with_body(tmp_path)
    _wire_repo_and_issue(state, source_labels=["cai:raised"])

    with patch("cai.workflows.comment.get_parent_issue", return_value=None), \
         patch("cai.workflows.comment.ensure_labels") as mock_ensure:
        asyncio.run(CommentNode().run(MagicMock(state=state)))

    mock_ensure.assert_called_once()
    args = mock_ensure.call_args.args
    assert args[0] is state.bot
    assert args[1] == "o/r"
    # The shared CAI_LABEL_SPECS list (imported by comment.py).
    assert any(spec.name == "cai:resolved" for spec in args[2])
