from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from cai.github.repo import PRWorkspace
from cai.workflows.conflicts import (
    _has_conflict_markers,
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


@patch("cai.workflows.conflicts.rev_parse")
@patch("cai.workflows.conflicts.langfuse_workflow")
@patch("cai.workflows.conflicts._run_tests")
@patch("cai.workflows.conflicts.push_branch")
@patch("cai.workflows.conflicts._rebase_loop")
def test_solve_conflicts_raises_when_tests_fail(
    mock_loop, mock_push, mock_run_tests, mock_langfuse, mock_rev_parse, workspace
):
    mock_loop.return_value = (True, ["src/a.py"])
    mock_run_tests.return_value = (False, "FAILED tests/test_a.py::test_x")
    mock_rev_parse.side_effect = lambda root, ref: "head" if ref == "HEAD" else "base"
    bot = MagicMock()
    bot.token_for.return_value = "tok"

    with pytest.raises(RuntimeError, match="sanity test pass failed"):
        solve_conflicts(bot, workspace)

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
