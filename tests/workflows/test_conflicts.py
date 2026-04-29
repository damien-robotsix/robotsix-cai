from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from cai.github.repo import PRWorkspace
from cai.workflows.conflicts import (
    _conflict_body,
    _has_conflict_markers,
    _step_prompt,
    _test_fix_body,
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


def test_conflict_body_lists_files():
    out = _conflict_body("main", "feature/x", ["src/a.py", "docs/b.md"])
    assert "`src/a.py`" in out
    assert "`docs/b.md`" in out
    assert "main" in out and "feature/x" in out
    assert "<<<<<<<" in out


def test_test_fix_body_includes_failure():
    out = _test_fix_body(
        "main", "feature/x", ["src/a.py"], "FAILED tests/test_a.py::test_x"
    )
    assert "FAILED tests/test_a.py::test_x" in out
    assert "`src/a.py`" in out


def test_has_conflict_markers_detects_each_marker(tmp_path: Path):
    f = tmp_path / "x.py"
    f.write_text("ok\n=======\nstuff\n")
    assert _has_conflict_markers(tmp_path, ["x.py"]) is True
    f.write_text("clean\n")
    assert _has_conflict_markers(tmp_path, ["x.py"]) is False
    # Missing files are silently skipped, not errors.
    assert _has_conflict_markers(tmp_path, ["nope.py"]) is False


@patch("cai.workflows.conflicts.langfuse_workflow")
@patch("cai.workflows.conflicts.push_branch")
@patch("cai.workflows.conflicts._rebase_loop")
def test_solve_conflicts_clean_rebase_skips_tests_and_implement(
    mock_loop, mock_push, mock_langfuse, workspace
):
    mock_loop.return_value = (True, [])  # rebase no-op (no conflicts touched)
    bot = MagicMock()
    bot.token_for.return_value = "tok"

    result = solve_conflicts(bot, workspace)

    assert result == {"mode": "clean", "conflicted_files": []}
    mock_push.assert_called_once()


@patch("cai.workflows.conflicts.langfuse_workflow")
@patch("cai.workflows.conflicts._run_tests")
@patch("cai.workflows.conflicts.push_branch")
@patch("cai.workflows.conflicts._rebase_loop")
def test_solve_conflicts_resolved_pushes_when_tests_pass(
    mock_loop, mock_push, mock_run_tests, mock_langfuse, workspace
):
    mock_loop.return_value = (True, ["src/a.py"])
    mock_run_tests.return_value = (True, "")
    bot = MagicMock()
    bot.token_for.return_value = "tok"

    result = solve_conflicts(bot, workspace)

    assert result == {"mode": "rebased", "conflicted_files": ["src/a.py"]}
    mock_run_tests.assert_called_once()
    mock_push.assert_called_once()


@patch("cai.workflows.conflicts.langfuse_workflow")
@patch("cai.workflows.conflicts._fall_back_to_implement")
@patch("cai.workflows.conflicts._run_tests")
@patch("cai.workflows.conflicts.push_branch")
@patch("cai.workflows.conflicts._rebase_loop")
def test_solve_conflicts_falls_back_when_tests_fail(
    mock_loop,
    mock_push,
    mock_run_tests,
    mock_fallback,
    mock_langfuse,
    workspace,
):
    mock_loop.return_value = (True, ["src/a.py"])
    mock_run_tests.return_value = (False, "FAILED tests/test_a.py::test_x")
    bot = MagicMock()
    bot.token_for.return_value = "tok"

    result = solve_conflicts(bot, workspace)

    assert result["mode"] == "implement_fallback"
    assert result["reason"] == "tests_failed"
    assert result["conflicted_files"] == ["src/a.py"]
    mock_fallback.assert_called_once()
    # Body passed to fallback mentions the failing test output.
    body = mock_fallback.call_args[0][2]
    assert "FAILED tests/test_a.py::test_x" in body
    # No direct push when we hand off to ImplementNode — PRNode pushes.
    mock_push.assert_not_called()


@patch("cai.workflows.conflicts.langfuse_workflow")
@patch("cai.workflows.conflicts._fall_back_to_implement")
@patch("cai.workflows.conflicts._merge_for_fallback")
@patch("cai.workflows.conflicts.rebase_in_progress")
@patch("cai.workflows.conflicts.push_branch")
@patch("cai.workflows.conflicts._rebase_loop")
def test_solve_conflicts_falls_back_when_rebase_fails(
    mock_loop,
    mock_push,
    mock_in_progress,
    mock_merge,
    mock_fallback,
    mock_langfuse,
    workspace,
):
    mock_loop.return_value = (False, [])
    mock_in_progress.return_value = False
    mock_merge.return_value = ["src/a.py"]
    bot = MagicMock()
    bot.token_for.return_value = "tok"

    result = solve_conflicts(bot, workspace)

    assert result["mode"] == "implement_fallback"
    assert result["reason"] == "rebase_failed"
    assert result["conflicted_files"] == ["src/a.py"]
    mock_merge.assert_called_once()
    # Merge commit gets pushed so the PR has a stable HEAD before
    # ImplementNode rewrites the working tree.
    mock_push.assert_called_once()
    mock_fallback.assert_called_once()


@patch("cai.workflows.conflicts.langfuse_workflow")
@patch("cai.workflows.conflicts._fall_back_to_implement")
@patch("cai.workflows.conflicts._merge_for_fallback")
@patch("cai.workflows.conflicts.rebase_in_progress")
@patch("cai.workflows.conflicts.push_branch")
@patch("cai.workflows.conflicts._rebase_loop")
def test_solve_conflicts_falls_back_clean_when_merge_clean(
    mock_loop,
    mock_push,
    mock_in_progress,
    mock_merge,
    mock_fallback,
    mock_langfuse,
    workspace,
):
    """Rebase failed but the merge fallback turned out to be clean.

    No conflict body is needed; we just push the merge and skip the
    implement agent.
    """
    mock_loop.return_value = (False, [])
    mock_in_progress.return_value = False
    mock_merge.return_value = []
    bot = MagicMock()
    bot.token_for.return_value = "tok"

    result = solve_conflicts(bot, workspace)

    assert result == {"mode": "clean", "conflicted_files": []}
    mock_fallback.assert_not_called()
    mock_push.assert_called_once()
