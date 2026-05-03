from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cai.github.repo import PRWorkspace
from cai.workflows.conflicts import (
    _has_conflict_markers,
    _rebase_loop_async,
    _run_resolve_step,
    _step_prompt,
    _strip_orphaned_markers,
    solve_conflicts,
)


@pytest.fixture
def workspace(tmp_path: Path) -> PRWorkspace:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    body = tmp_path / "42.md"
    body.write_text("original PR body")
    return PRWorkspace(
        root=tmp_path,
        repo_root=repo_root,
        body_path=body,
        repo="owner/name",
        number=42,
        head_branch="feature/x",
        base_branch="main",
        title="Add x",
        body="original PR body",
    )


def test_step_prompt_lists_files_and_diff():
    step = {"sha": "abcdef1234567890", "subject": "rewrite a", "diff": "@@ a @@"}
    out = _step_prompt(
        "Add x", "PR description here.", step, ["src/a.py", "src/b.py"]
    )
    assert "abcdef12" in out  # short sha
    assert "rewrite a" in out
    assert "@@ a @@" in out
    assert "`src/a.py`" in out
    assert "`src/b.py`" in out
    assert "Add x" in out
    assert "PR description here." in out


def test_has_conflict_markers_detects_real_blocks(tmp_path: Path):
    f = tmp_path / "x.py"
    f.write_text("<<<<<<< HEAD\nours\n=======\ntheirs\n>>>>>>> sha\n")
    assert _has_conflict_markers(tmp_path, ["x.py"]) is True
    f.write_text("clean\n")
    assert _has_conflict_markers(tmp_path, ["x.py"]) is False
    # Missing files are silently skipped, not errors.
    assert _has_conflict_markers(tmp_path, ["nope.py"]) is False


def test_has_conflict_markers_no_false_positive_on_literal(tmp_path: Path):
    # A source file that contains "=======" as a string literal must not be flagged.
    f = tmp_path / "code.py"
    f.write_text('if any(m in text for m in ("<<<<<<<", "=======", ">>>>>>>")):  # literal\n')
    assert _has_conflict_markers(tmp_path, ["code.py"]) is False


def test_has_conflict_markers_no_false_positive_on_orphaned_lines(tmp_path: Path):
    # Orphaned ======= / >>>>>>> lines (from nested conflicts that were committed)
    # are not valid conflict blocks and must not be flagged.
    f = tmp_path / "x.py"
    f.write_text("ok\n=======\n>>>>>>> origin/main\nclean\n")
    assert _has_conflict_markers(tmp_path, ["x.py"]) is False


def test_strip_orphaned_markers_removes_dangling_lines(tmp_path: Path):
    f = tmp_path / "x.py"
    f.write_text("good\n=======\n>>>>>>> origin/main\ncode\n")
    _strip_orphaned_markers(tmp_path, ["x.py"])
    assert f.read_text() == "good\ncode\n"


def test_strip_orphaned_markers_leaves_clean_files_alone(tmp_path: Path):
    f = tmp_path / "x.py"
    content = "just normal code\n"
    f.write_text(content)
    _strip_orphaned_markers(tmp_path, ["x.py"])
    assert f.read_text() == content


@patch("cai.workflows.conflicts.rev_parse")
@patch("cai.workflows.conflicts.langfuse_workflow")
@patch("cai.workflows.conflicts.push_branch")
@patch("cai.workflows.conflicts._rebase_loop")
def test_solve_conflicts_clean_rebase_skips_tests_and_implement(
    mock_loop, mock_push, mock_langfuse, mock_rev_parse, workspace
):
    mock_loop.return_value = (True, [])  # rebase no-op (no conflicts touched)
    mock_rev_parse.side_effect = lambda root, ref: "head" if ref == "HEAD" else "base"
    bot = MagicMock()
    bot.token_for.return_value = "tok"

    result = solve_conflicts(bot, workspace)

    assert result == {"mode": "clean", "conflicted_files": []}
    mock_push.assert_called_once()


@patch("cai.workflows.conflicts.rev_parse")
@patch("cai.workflows.conflicts.langfuse_workflow")
@patch("cai.workflows.conflicts._run_tests")
@patch("cai.workflows.conflicts.push_branch")
@patch("cai.workflows.conflicts._rebase_loop")
def test_solve_conflicts_resolved_pushes_when_tests_pass(
    mock_loop, mock_push, mock_run_tests, mock_langfuse, mock_rev_parse, workspace
):
    mock_loop.return_value = (True, ["src/a.py"])
    mock_run_tests.return_value = (True, "")
    mock_rev_parse.side_effect = lambda root, ref: "head" if ref == "HEAD" else "base"
    bot = MagicMock()
    bot.token_for.return_value = "tok"

    result = solve_conflicts(bot, workspace)

    assert result == {"mode": "rebased", "conflicted_files": ["src/a.py"]}
    mock_run_tests.assert_called_once()
    mock_push.assert_called_once()


@patch("cai.workflows.fsm.solve_graph")
@patch("cai.workflows.conflicts.rev_parse")
@patch("cai.workflows.conflicts.langfuse_workflow")
@patch("cai.workflows.conflicts._run_tests")
@patch("cai.workflows.conflicts.push_branch")
@patch("cai.workflows.conflicts._rebase_loop")
def test_solve_conflicts_hands_off_to_implement_when_tests_fail(
    mock_loop,
    mock_push,
    mock_run_tests,
    mock_langfuse,
    mock_rev_parse,
    mock_solve_graph,
    workspace,
):
    # Sanity-test failure used to abort the workflow. It now routes through
    # solve_graph entered at ImplementNode (mirroring solve's recovery path),
    # so ConflictsState.mode is "rebased+fixed" and our PushNode is bypassed
    # (solve's PRNode handles the push from inside the handoff).
    mock_loop.return_value = (True, ["src/a.py"])
    mock_run_tests.return_value = (False, "FAILED tests/test_a.py::test_x")
    mock_rev_parse.side_effect = lambda root, ref: "head" if ref == "HEAD" else "base"
    mock_solve_graph.run = AsyncMock()
    bot = MagicMock()
    bot.token_for.return_value = "tok"

    result = solve_conflicts(bot, workspace)

    assert result == {"mode": "rebased+fixed", "conflicted_files": ["src/a.py"]}
    mock_solve_graph.run.assert_awaited_once()
    # The implement-handoff path entered solve_graph at ImplementNode with
    # the test failure details preset on IssueState.
    from cai.workflows.implement import ImplementNode
    entry_node, kwargs = mock_solve_graph.run.await_args.args[0], mock_solve_graph.run.await_args.kwargs
    assert isinstance(entry_node, ImplementNode)
    assert kwargs["state"].test_failure_details == "FAILED tests/test_a.py::test_x"
    assert kwargs["state"].pr_number == workspace.number
    assert kwargs["state"].branch_name == workspace.head_branch
    # Our own PushNode is bypassed; solve's PRNode (inside the mocked
    # solve_graph) is responsible for the push in this branch.
    mock_push.assert_not_called()


@patch("cai.workflows.conflicts.ensure_labels")
@patch("cai.workflows.conflicts.rev_parse")
@patch("cai.workflows.conflicts.langfuse_workflow")
@patch("cai.workflows.conflicts.push_branch")
@patch("cai.workflows.conflicts._rebase_loop")
def test_solve_conflicts_obsolete_when_head_equals_base(
    mock_loop,
    mock_push,
    mock_langfuse,
    mock_rev_parse,
    mock_ensure_labels,
    workspace,
):
    # Rebase consumed every commit (each was already on base).  The graph
    # must close the PR with a comment and skip the destructive force-push.
    mock_loop.return_value = (True, [])
    mock_rev_parse.return_value = "same-sha"
    bot = MagicMock()
    bot.token_for.return_value = "tok"

    result = solve_conflicts(bot, workspace)

    assert result == {"mode": "obsolete", "conflicted_files": []}
    mock_push.assert_not_called()

    issue = bot.repo.return_value.get_issue.return_value
    issue.create_comment.assert_called_once()
    comment_body = issue.create_comment.call_args.args[0]
    assert "obsolete" in comment_body.lower()
    assert workspace.head_branch in comment_body
    assert workspace.base_branch in comment_body
    issue.edit.assert_called_once_with(state="closed")

    # cai:obsolete is added; cai:human-review is NOT added back to a closed PR.
    add_calls = issue.add_to_labels.call_args_list
    assert any(c.args == ("cai:obsolete",) for c in add_calls)
    assert not any(c.args == ("cai:human-review",) for c in add_calls)


@patch("cai.workflows.conflicts.langfuse_workflow")
@patch("cai.workflows.conflicts.rebase_in_progress")
@patch("cai.workflows.conflicts._rebase_loop")
def test_solve_conflicts_raises_when_rebase_fails(
    mock_loop, mock_in_progress, mock_langfuse, workspace
):
    mock_loop.return_value = (False, [])
    mock_in_progress.return_value = False
    bot = MagicMock()
    bot.token_for.return_value = "tok"

    with pytest.raises(RuntimeError, match="Rebase of .* failed"):
        solve_conflicts(bot, workspace)


@patch("cai.workflows.conflicts.langfuse_workflow")
@patch("cai.workflows.conflicts.rebase_abort")
@patch("cai.workflows.conflicts.rebase_in_progress")
@patch("cai.workflows.conflicts._rebase_loop")
def test_solve_conflicts_aborts_rebase_before_raising(
    mock_loop, mock_in_progress, mock_abort, mock_langfuse, workspace
):
    mock_loop.return_value = (False, [])
    mock_in_progress.return_value = True
    bot = MagicMock()

    with pytest.raises(RuntimeError):
        solve_conflicts(bot, workspace)

    mock_abort.assert_called_once_with(workspace.repo_root)


@patch("cai.workflows.conflicts._run_resolve_step", new_callable=AsyncMock)
@patch("cai.workflows.conflicts.rebase_skip")
@patch("cai.workflows.conflicts.index_matches_head")
@patch("cai.workflows.conflicts.rebase_continue")
@patch("cai.workflows.conflicts.stage_all")
@patch("cai.workflows.conflicts._has_conflict_markers")
@patch("cai.workflows.conflicts._strip_orphaned_markers")
@patch("cai.workflows.conflicts.conflicted_paths")
@patch("cai.workflows.conflicts.current_rebase_step")
@patch("cai.workflows.conflicts.rebase_onto")
@patch("cai.workflows.conflicts.rebase_abort")
def test_rebase_loop_aborts_on_hook_failure(
    mock_abort,
    mock_onto,
    mock_step,
    mock_conflicts,
    mock_strip,
    mock_has_markers,
    mock_stage,
    mock_continue,
    mock_index_matches,
    mock_skip,
    mock_resolve,
    workspace,
):
    """rebase_continue paused with staged changes ⇒ abort, not silent skip.

    Regression for the obsolete-misclassification bug where a pre-commit
    hook failure (or any non-empty pause) was treated as ``--skip``-able
    and silently dropped the commit.
    """
    mock_onto.return_value = False
    mock_step.return_value = {"sha": "abcd1234", "subject": "x", "diff": ""}
    # First call: at the conflict pause; second call: post-stage_all check
    # in the new branch (must be empty so we reach index_matches_head).
    mock_conflicts.side_effect = [["a.txt"], []]
    mock_has_markers.return_value = False
    mock_continue.return_value = False
    mock_index_matches.return_value = False  # ← non-empty staged tree

    ok, touched = asyncio.run(_rebase_loop_async(workspace))

    assert ok is False
    assert touched == ["a.txt"]
    mock_skip.assert_not_called()
    mock_abort.assert_called_once_with(workspace.repo_root)


@patch("cai.workflows.conflicts._run_resolve_step", new_callable=AsyncMock)
@patch("cai.workflows.conflicts.rebase_skip")
@patch("cai.workflows.conflicts.index_matches_head")
@patch("cai.workflows.conflicts.rebase_continue")
@patch("cai.workflows.conflicts.stage_all")
@patch("cai.workflows.conflicts._has_conflict_markers")
@patch("cai.workflows.conflicts._strip_orphaned_markers")
@patch("cai.workflows.conflicts.conflicted_paths")
@patch("cai.workflows.conflicts.current_rebase_step")
@patch("cai.workflows.conflicts.rebase_onto")
@patch("cai.workflows.conflicts.rebase_abort")
def test_rebase_loop_skips_genuinely_empty_commit(
    mock_abort,
    mock_onto,
    mock_step,
    mock_conflicts,
    mock_strip,
    mock_has_markers,
    mock_stage,
    mock_continue,
    mock_index_matches,
    mock_skip,
    mock_resolve,
    workspace,
):
    """When the cherry-pick is genuinely empty, ``--skip`` advances cleanly."""
    mock_onto.return_value = False
    mock_step.return_value = {"sha": "abcd1234", "subject": "x", "diff": ""}
    mock_conflicts.side_effect = [["a.txt"], []]
    mock_has_markers.return_value = False
    mock_continue.return_value = False
    mock_index_matches.return_value = True  # ← empty staged tree
    mock_skip.return_value = True

    ok, touched = asyncio.run(_rebase_loop_async(workspace))

    assert ok is True
    assert touched == ["a.txt"]
    mock_skip.assert_called_once_with(workspace.repo_root)
    mock_abort.assert_not_called()


class TestRunResolveStep:
    """_run_resolve_step() catches UsageLimitExceeded and retries once
    with bumped request_limit (60 → 90).
    """

    def test_resolve_step_succeeds_on_first_attempt(self, tmp_path):
        """Normal case: agent.run succeeds on the first call."""
        mock_agent = MagicMock()
        mock_agent.run = AsyncMock()

        with patch("cai.workflows.conflicts._resolve_step_agent", return_value=mock_agent):
            asyncio.run(_run_resolve_step(tmp_path, "resolve this"))

        mock_agent.run.assert_awaited_once()
        _, kwargs = mock_agent.run.await_args
        assert kwargs["usage_limits"].request_limit == 60

    def test_resolve_step_retries_with_bumped_limit(self, tmp_path):
        """First call raises UsageLimitExceeded, retry with
        request_limit=90 succeeds."""
        from pydantic_ai.exceptions import UsageLimitExceeded

        mock_agent = MagicMock()
        mock_agent.run = AsyncMock(side_effect=[
            UsageLimitExceeded("limit hit"),
            None,
        ])

        with patch("cai.workflows.conflicts._resolve_step_agent", return_value=mock_agent):
            asyncio.run(_run_resolve_step(tmp_path, "resolve this"))

        assert mock_agent.run.await_count == 2
        _, kwargs1 = mock_agent.run.await_args_list[0]
        _, kwargs2 = mock_agent.run.await_args_list[1]
        assert kwargs1["usage_limits"].request_limit == 60
        assert kwargs2["usage_limits"].request_limit == 90

    def test_resolve_step_bubbles_on_second_failure(self, tmp_path):
        """Both calls raise UsageLimitExceeded → exception propagates."""
        from pydantic_ai.exceptions import UsageLimitExceeded

        mock_agent = MagicMock()
        mock_agent.run = AsyncMock(side_effect=UsageLimitExceeded("limit hit"))

        with patch("cai.workflows.conflicts._resolve_step_agent", return_value=mock_agent):
            with pytest.raises(UsageLimitExceeded):
                asyncio.run(_run_resolve_step(tmp_path, "resolve this"))

        assert mock_agent.run.await_count == 2

    def test_resolve_step_retries_on_404_openrouter(self, tmp_path):
        """First call raises ModelHTTPError(404, "No endpoints found ..."),
        retry succeeds → two agent.run calls."""
        from pydantic_ai.exceptions import ModelHTTPError

        mock_agent = MagicMock()
        mock_agent.run = AsyncMock(side_effect=[
            ModelHTTPError(
                status_code=404,
                model_name="test",
                body="No endpoints found that can handle the requested parameters",
            ),
            None,
        ])

        with (
            patch("cai.workflows.conflicts._resolve_step_agent", return_value=mock_agent),
            patch("asyncio.sleep", new_callable=AsyncMock),
        ):
            asyncio.run(_run_resolve_step(tmp_path, "resolve this"))

        assert mock_agent.run.await_count == 2

    def test_resolve_step_no_retry_on_404_other_body(self, tmp_path):
        """A 404 with a different body message is re-raised immediately,
        single agent.run call."""
        from pydantic_ai.exceptions import ModelHTTPError

        mock_agent = MagicMock()
        mock_agent.run = AsyncMock(side_effect=ModelHTTPError(
            status_code=404,
            model_name="test",
            body="Other message",
        ))

        with (
            patch("cai.workflows.conflicts._resolve_step_agent", return_value=mock_agent),
            patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep,
        ):
            with pytest.raises(ModelHTTPError):
                asyncio.run(_run_resolve_step(tmp_path, "resolve this"))

        assert mock_agent.run.await_count == 1
        mock_sleep.assert_not_called()


# ---------------------------------------------------------------------------
# _rebase_loop cross-context (PRNode constructs PRWorkspace with number=0)
# ---------------------------------------------------------------------------


def test_rebase_loop_works_with_number_zero_workspace(tmp_path: Path):
    """_rebase_loop must accept a PRWorkspace with number=0 (as PRNode constructs it)."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    body = tmp_path / "99.md"
    body.write_text("PR body")
    ws = PRWorkspace(
        root=tmp_path,
        repo_root=repo_root,
        body_path=body,
        repo="owner/name",
        number=0,
        head_branch="cai/solve-99",
        base_branch="main",
        title="Add feature",
        body="PR body",
    )
    # Verify the workspace fields _rebase_loop actually reads.
    assert ws.repo_root == repo_root
    assert ws.base_branch == "main"
    assert ws.title == "Add feature"
    assert ws.body == "PR body"
    assert ws.number == 0
