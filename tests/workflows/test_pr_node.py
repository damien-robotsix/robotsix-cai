from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import pytest

from cai.workflows.merge_eval import MergeEvaluationNode
from cai.workflows.pr import PRNode, _bundled_commit_message
from cai.workflows.state import (
    DocsOutput,
    GitHubWorkflowReviewOutput,
    ImplementOutput,
    PydanticAIReviewOutput,
    PythonReviewOutput,
    TestOutput,
)


@pytest.fixture
def state(state: "IssueState") -> "IssueState":
    state.implement_output = ImplementOutput(
        summary="s", commit_message="c", required_checks=[], replies=[]
    )
    return state


def _run(node, state):
    ctx = MagicMock()
    ctx.state = state
    return asyncio.run(node.run(ctx))


@patch("cai.workflows.pr.create_pull_request")
@patch("cai.workflows.pr.push_branch")
@patch("cai.workflows.pr._has_staged_changes", return_value=True)
@patch("cai.workflows.pr.commit")
@patch("cai.workflows.pr.rebase_onto", return_value=True)
@patch("cai.workflows.pr.fetch")
@patch("cai.workflows.pr.stage_all")
def test_pr_node_existing_pr_skips_create(
    mock_stage, mock_fetch, mock_rebase, mock_commit, mock_dirty, mock_push, mock_create, state
):
    # PR-mode conflict resolution: pr_number set, no review threads.
    state.pr_number = 99

    result = _run(PRNode(), state)

    assert isinstance(result, MergeEvaluationNode)
    mock_push.assert_called_once()
    mock_create.assert_not_called()


@patch("cai.workflows.pr.create_pull_request", return_value=("https://pr/1", 1))
@patch("cai.workflows.pr.push_branch")
@patch("cai.workflows.pr._has_staged_changes", return_value=True)
@patch("cai.workflows.pr.commit")
@patch("cai.workflows.pr.rebase_onto", return_value=True)
@patch("cai.workflows.pr.fetch")
@patch("cai.workflows.pr.stage_all")
def test_pr_node_issue_mode_creates_pr(
    mock_stage, mock_fetch, mock_rebase, mock_commit, mock_dirty, mock_push, mock_create, state
):
    # Issue mode: no pr_number, no threads -> opens a new PR.
    result = _run(PRNode(), state)

    assert isinstance(result, MergeEvaluationNode)
    mock_push.assert_called_once()
    mock_create.assert_called_once()
    assert state.pr_url == "https://pr/1"
    assert state.pr_number == 1


@patch("cai.workflows.pr.create_pull_request")
@patch("cai.workflows.pr.push_branch")
@patch("cai.workflows.pr._has_staged_changes", return_value=False)
@patch("cai.workflows.pr.commit")
@patch("cai.workflows.pr.rebase_onto", return_value=True)
@patch("cai.workflows.pr.fetch")
@patch("cai.workflows.pr.stage_all")
def test_pr_node_issue_mode_no_changes_closes_issue_as_not_planned(
    mock_stage, mock_fetch, mock_rebase, mock_commit, mock_dirty, mock_push, mock_create, state
):
    # Issue mode where the implement agent decided no code change was
    # needed: nothing is staged, so pushing an empty branch and creating a
    # no-diff PR both fail at GitHub. Skip both, comment with the agent's
    # reasoning, and close the issue as not_planned.
    state.implement_output.summary = "Already fixed by PR #42."
    issue = MagicMock()
    state.bot.repo.return_value.get_issue.return_value = issue

    result = _run(PRNode(), state)

    assert isinstance(result, MergeEvaluationNode)
    mock_commit.assert_not_called()
    mock_push.assert_not_called()
    mock_create.assert_not_called()
    assert state.pr_url is None
    assert state.pr_number is None
    state.bot.repo.assert_called_once_with("o/r")
    state.bot.repo.return_value.get_issue.assert_called_once_with(99)
    issue.create_comment.assert_called_once()
    comment_body = issue.create_comment.call_args.args[0]
    assert "not planned" in comment_body
    assert "Already fixed by PR #42." in comment_body
    issue.edit.assert_called_once_with(state="closed", state_reason="not_planned")


# ---------------------------------------------------------------------------
# _bundled_commit_message
# ---------------------------------------------------------------------------


def test_bundled_commit_message_includes_workflow_review(state):
    """_bundled_commit_message includes the workflow review commit message when present."""
    state.implement_output = ImplementOutput(
        summary="s", commit_message="feat: add feature", required_checks=[], replies=[],
    )
    state.github_workflow_review_output = GitHubWorkflowReviewOutput(
        summary="Fixed permissions in deploy.yml",
        commit_message="fix: add permissions to deploy workflow",
    )

    result = _bundled_commit_message(state)

    assert "feat: add feature" in result
    assert "fix: add permissions to deploy workflow" in result


def test_bundled_commit_message_skips_empty_workflow_review(state):
    """_bundled_commit_message skips the workflow review when its commit_message is empty."""
    state.implement_output = ImplementOutput(
        summary="s", commit_message="feat: add feature", required_checks=[], replies=[],
    )
    state.github_workflow_review_output = GitHubWorkflowReviewOutput(
        summary="No issues found.",
        commit_message="",
    )

    result = _bundled_commit_message(state)

    assert result == "feat: add feature"


def test_bundled_commit_message_includes_all_non_empty(state):
    """_bundled_commit_message includes all non-empty commit messages in order."""
    state.implement_output = ImplementOutput(
        summary="s", commit_message="feat: add feature", required_checks=[], replies=[],
    )
    state.test_output = TestOutput(
        summary="Tests written", commit_message="test: add tests",
    )
    state.python_review_output = PythonReviewOutput(
        summary="Fixed lint", commit_message="style: fix lint",
    )
    state.github_workflow_review_output = GitHubWorkflowReviewOutput(
        summary="Fixed workflow", commit_message="ci: fix workflow permissions",
    )
    state.pydantic_ai_review_output = PydanticAIReviewOutput(
        summary="Fixed agent construction", commit_message="fix: correct Agent construction",
    )
    state.docs_output = DocsOutput(
        summary="Docs updated", commit_message="docs: update readme",
    )

    result = _bundled_commit_message(state)

    assert result == (
        "feat: add feature\n\n"
        "test: add tests\n\n"
        "style: fix lint\n\n"
        "ci: fix workflow permissions\n\n"
        "fix: correct Agent construction\n\n"
        "docs: update readme"
    )


def test_bundled_commit_message_skips_none_outputs(state):
    """_bundled_commit_message gracefully handles None outputs for optional review fields."""
    state.implement_output = ImplementOutput(
        summary="s", commit_message="feat: add feature", required_checks=[], replies=[],
    )
    state.test_output = None
    state.python_review_output = None
    state.github_workflow_review_output = None
    state.pydantic_ai_review_output = None
    state.docs_output = None

    result = _bundled_commit_message(state)

    assert result == "feat: add feature"


def test_bundled_commit_message_includes_pydantic_ai_review(state):
    """_bundled_commit_message includes the pydantic-ai review commit message when present."""
    state.implement_output = ImplementOutput(
        summary="s", commit_message="feat: add feature", required_checks=[], replies=[],
    )
    state.pydantic_ai_review_output = PydanticAIReviewOutput(
        summary="Fixed deprecated API usage in solver.py",
        commit_message="fix: update deprecated pydantic-ai API",
    )

    result = _bundled_commit_message(state)

    assert "feat: add feature" in result
    assert "fix: update deprecated pydantic-ai API" in result


def test_bundled_commit_message_skips_empty_pydantic_ai_review(state):
    """_bundled_commit_message skips the pydantic-ai review when its commit_message is empty."""
    state.implement_output = ImplementOutput(
        summary="s", commit_message="feat: add feature", required_checks=[], replies=[],
    )
    state.pydantic_ai_review_output = PydanticAIReviewOutput(
        summary="No issues found.",
        commit_message="",
    )

    result = _bundled_commit_message(state)

    assert result == "feat: add feature"


# ---------------------------------------------------------------------------
# Pre-push rebase paths
# ---------------------------------------------------------------------------


@patch("cai.workflows.pr.create_pull_request", return_value=("https://pr/1", 1))
@patch("cai.workflows.pr.push_branch")
@patch("cai.workflows.pr.stage_all")
@patch("cai.workflows.conflicts._rebase_loop")
@patch("cai.workflows.pr.rebase_onto")
@patch("cai.workflows.pr.fetch")
@patch("cai.workflows.pr._has_staged_changes", return_value=True)
@patch("cai.workflows.pr.commit")
def test_pr_node_clean_rebase_proceeds_to_push(
    mock_commit, mock_dirty, mock_fetch, mock_rebase_onto,
    mock_rebase_loop, mock_stage, mock_push, mock_create, state,
):
    """When rebase_onto returns True (clean), push and PR creation proceed."""
    mock_rebase_onto.return_value = True

    result = _run(PRNode(), state)

    assert isinstance(result, MergeEvaluationNode)
    mock_fetch.assert_called_once_with(state.repo_root)
    mock_rebase_onto.assert_called_once_with(state.repo_root, "origin/main")
    mock_rebase_loop.assert_not_called()
    mock_push.assert_called_once()
    mock_create.assert_called_once()


@patch("cai.workflows.pr.create_pull_request", return_value=("https://pr/2", 2))
@patch("cai.workflows.pr.push_branch")
@patch("cai.workflows.pr.stage_all")
@patch("cai.workflows.conflicts._rebase_loop")
@patch("cai.workflows.pr.rebase_onto")
@patch("cai.workflows.pr.fetch")
@patch("cai.workflows.pr._has_staged_changes", return_value=True)
@patch("cai.workflows.pr.commit")
def test_pr_node_conflict_resolved_proceeds_to_push(
    mock_commit, mock_dirty, mock_fetch, mock_rebase_onto,
    mock_rebase_loop, mock_stage, mock_push, mock_create, state,
):
    """When rebase_onto returns False but _rebase_loop resolves, push proceeds."""
    mock_rebase_onto.return_value = False
    mock_rebase_loop.return_value = (True, ["a.py"])

    result = _run(PRNode(), state)

    assert isinstance(result, MergeEvaluationNode)
    mock_fetch.assert_called_once_with(state.repo_root)
    mock_rebase_onto.assert_called_once_with(state.repo_root, "origin/main")
    mock_rebase_loop.assert_called_once()
    mock_push.assert_called_once()
    mock_create.assert_called_once()


@patch("cai.workflows.pr.create_pull_request")
@patch("cai.workflows.pr.push_branch")
@patch("cai.workflows.pr.stage_all")
@patch("cai.workflows.conflicts._rebase_loop")
@patch("cai.workflows.pr.rebase_onto")
@patch("cai.workflows.pr.fetch")
@patch("cai.workflows.pr._has_staged_changes", return_value=True)
@patch("cai.workflows.pr.commit")
def test_pr_node_conflict_fail_aborts_and_raises(
    mock_commit, mock_dirty, mock_fetch, mock_rebase_onto,
    mock_rebase_loop, mock_stage, mock_push, mock_create, state,
):
    """When _rebase_loop returns False, abort the rebase and raise."""
    mock_rebase_onto.return_value = False
    mock_rebase_loop.return_value = (False, [])

    with pytest.raises(RuntimeError, match="Rebase of .* failed"):
        _run(PRNode(), state)

    mock_push.assert_not_called()
    mock_create.assert_not_called()
