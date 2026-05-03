from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import pytest

from cai.workflows.pre_push_validate import PrePushValidationNode, _parse_files_to_change
from cai.workflows.implement import ImplementNode
from cai.workflows.pr import PRNode


# ---------------------------------------------------------------------------
# _parse_files_to_change — edge-case coverage
# ---------------------------------------------------------------------------


def _run(node, state):
    ctx = MagicMock()
    ctx.state = state
    return asyncio.run(node.run(ctx))


# ---------------------------------------------------------------------------
# _parse_files_to_change
# ---------------------------------------------------------------------------


def test_parse_files_to_change_with_heading():
    body = "### Files to change\n\n- `src/foo.py`\n- `tests/bar.py`\n"
    result = _parse_files_to_change(body)
    assert result == {"src/foo.py", "tests/bar.py"}


def test_parse_files_to_change_case_insensitive():
    body = "## FILES\n\n- `src/foo.py`\n"
    result = _parse_files_to_change(body)
    assert result == {"src/foo.py"}


def test_parse_files_to_change_no_section():
    body = "### Description\n\nSome text.\n"
    assert _parse_files_to_change(body) is None


def test_parse_files_to_change_stops_at_next_heading():
    body = "### Files to change\n\n- `src/foo.py`\n\n### Next section\n\nMore text.\n"
    result = _parse_files_to_change(body)
    assert result == {"src/foo.py"}


def test_parse_files_to_change_comma_separated():
    body = "### Files\n\nsrc/foo.py, tests/bar.py\n"
    result = _parse_files_to_change(body)
    assert result == {"src/foo.py", "tests/bar.py"}


def test_parse_files_to_change_bullet_without_backticks():
    """Bullet items without backticks are split by comma and added."""
    body = "### Files to change\n\n- src/foo.py\n- tests/bar.py\n"
    result = _parse_files_to_change(body)
    assert result == {"src/foo.py", "tests/bar.py"}


def test_parse_files_to_change_multiple_fences_in_bullet():
    """Multiple backtick-paths in a single bullet are all extracted."""
    body = "### Files to change\n\n- `src/foo.py`, `tests/bar.py`\n"
    result = _parse_files_to_change(body)
    assert result == {"src/foo.py", "tests/bar.py"}


def test_parse_files_to_change_fence_in_plain_text():
    """Backtick paths in a non-bullet plain text line are extracted."""
    body = "### Files\n\nUpdate `src/foo.py` and `tests/bar.py` accordingly.\n"
    result = _parse_files_to_change(body)
    assert result == {"src/foo.py", "tests/bar.py"}


# ---------------------------------------------------------------------------
# PrePushValidationNode
# ---------------------------------------------------------------------------


@patch("cai.workflows.pre_push_validate.Repo")
@patch("cai.workflows.pre_push_validate.stage_all")
def test_empty_new_file_fails(mock_stage, mock_repo_class, state, tmp_path):
    """Stage a new empty file -> returns ImplementNode with failure message."""
    empty_file = tmp_path / "tests" / "empty_test.py"
    empty_file.parent.mkdir(parents=True, exist_ok=True)
    empty_file.write_text("")

    mock_repo = MagicMock()
    mock_repo_class.return_value = mock_repo

    def diff_side_effect(*args, **kwargs):
        cmd = " ".join(args)
        if "--diff-filter=A" in cmd:
            return "tests/empty_test.py"
        return "tests/empty_test.py"

    mock_repo.git.diff.side_effect = diff_side_effect

    state.body_path.write_text("### Description\n\nSome issue.\n")

    result = _run(PrePushValidationNode(), state)

    assert isinstance(result, ImplementNode)
    assert "empty scratch file" in state.push_validation_failure
    assert "tests/empty_test.py" in state.push_validation_failure
    assert state.push_validation_retry_count == 1


@patch("cai.workflows.pre_push_validate.Repo")
@patch("cai.workflows.pre_push_validate.stage_all")
def test_out_of_scope_file_fails(mock_stage, mock_repo_class, state, tmp_path):
    """Stage a file not in 'Files to change' -> returns ImplementNode."""
    scoped_file = tmp_path / "src" / "in_scope.py"
    scoped_file.parent.mkdir(parents=True, exist_ok=True)
    scoped_file.write_text("print('hello')")

    mock_repo = MagicMock()
    mock_repo_class.return_value = mock_repo

    def diff_side_effect(*args, **kwargs):
        cmd = " ".join(args)
        if "--diff-filter=A" in cmd:
            return ""
        return "src/in_scope.py\nsrc/out_of_scope.py"

    mock_repo.git.diff.side_effect = diff_side_effect

    state.body_path.write_text("### Files to change\n\n- `src/in_scope.py`\n")

    result = _run(PrePushValidationNode(), state)

    assert isinstance(result, ImplementNode)
    assert "outside the issue scope" in state.push_validation_failure
    assert "src/out_of_scope.py" in state.push_validation_failure


@patch("cai.workflows.pre_push_validate.Repo")
@patch("cai.workflows.pre_push_validate.stage_all")
def test_all_files_in_scope_passes(mock_stage, mock_repo_class, state, tmp_path):
    """Stage only files listed in 'Files to change' -> returns PRNode."""
    scoped_file = tmp_path / "src" / "in_scope.py"
    scoped_file.parent.mkdir(parents=True, exist_ok=True)
    scoped_file.write_text("print('hello')")

    mock_repo = MagicMock()
    mock_repo_class.return_value = mock_repo

    def diff_side_effect(*args, **kwargs):
        cmd = " ".join(args)
        if "--diff-filter=A" in cmd:
            return ""
        return "src/in_scope.py"

    mock_repo.git.diff.side_effect = diff_side_effect

    state.body_path.write_text("### Files to change\n\n- `src/in_scope.py`\n")

    result = _run(PrePushValidationNode(), state)

    assert isinstance(result, PRNode)
    assert state.push_validation_failure == ""
    assert state.push_validation_retry_count == 0


@patch("cai.workflows.pre_push_validate.Repo")
@patch("cai.workflows.pre_push_validate.stage_all")
def test_allow_list_files_pass(mock_stage, mock_repo_class, state, tmp_path):
    """Stage only allow-listed files -> returns PRNode."""
    mock_repo = MagicMock()
    mock_repo_class.return_value = mock_repo

    def diff_side_effect(*args, **kwargs):
        cmd = " ".join(args)
        if "--diff-filter=A" in cmd:
            return ""
        return ".github/workflows/cai-foo.yml"

    mock_repo.git.diff.side_effect = diff_side_effect

    state.body_path.write_text("### Files to change\n\n- `src/in_scope.py`\n")

    result = _run(PrePushValidationNode(), state)

    assert isinstance(result, PRNode)


@patch("cai.workflows.pre_push_validate.Repo")
@patch("cai.workflows.pre_push_validate.stage_all")
def test_no_files_section_skips_gate(mock_stage, mock_repo_class, state, tmp_path):
    """Body without 'Files to change' section -> any file passes."""
    scoped_file = tmp_path / "src" / "any_file.py"
    scoped_file.parent.mkdir(parents=True, exist_ok=True)
    scoped_file.write_text("print('hello')")

    mock_repo = MagicMock()
    mock_repo_class.return_value = mock_repo

    def diff_side_effect(*args, **kwargs):
        cmd = " ".join(args)
        if "--diff-filter=A" in cmd:
            return ""
        return "src/any_file.py"

    mock_repo.git.diff.side_effect = diff_side_effect

    state.body_path.write_text("### Description\n\nNo files section here.\n")

    result = _run(PrePushValidationNode(), state)

    assert isinstance(result, PRNode)


@patch("cai.workflows.pre_push_validate.Repo")
@patch("cai.workflows.pre_push_validate.stage_all")
def test_retry_exhaustion(mock_stage, mock_repo_class, state, tmp_path):
    """After 3 attempts (count >= 2), raises RuntimeError."""
    state.push_validation_retry_count = 2

    empty_file = tmp_path / "tests" / "empty_test.py"
    empty_file.parent.mkdir(parents=True, exist_ok=True)
    empty_file.write_text("")

    mock_repo = MagicMock()
    mock_repo_class.return_value = mock_repo

    def diff_side_effect(*args, **kwargs):
        cmd = " ".join(args)
        if "--diff-filter=A" in cmd:
            return "tests/empty_test.py"
        return "tests/empty_test.py"

    mock_repo.git.diff.side_effect = diff_side_effect

    state.body_path.write_text("### Description\n\nSome issue.\n")

    with pytest.raises(RuntimeError) as exc_info:
        _run(PrePushValidationNode(), state)

    assert "empty scratch file" in str(exc_info.value)


@patch("cai.workflows.pre_push_validate.Repo")
@patch("cai.workflows.pre_push_validate.stage_all")
def test_human_review_skips(mock_stage, mock_repo_class, state):
    """Label 'cai:human-review' present -> returns PRNode regardless."""
    state.meta.labels = ["cai:human-review", "bug"]

    result = _run(PrePushValidationNode(), state)

    assert isinstance(result, PRNode)
    mock_stage.assert_not_called()


@patch("cai.workflows.pre_push_validate.Repo")
@patch("cai.workflows.pre_push_validate.stage_all")
def test_both_empty_and_out_of_scope_fail(mock_stage, mock_repo_class, state, tmp_path):
    """Both empty file AND out-of-scope file failures are reported together."""
    empty_file = tmp_path / "tests" / "empty_test.py"
    empty_file.parent.mkdir(parents=True, exist_ok=True)
    empty_file.write_text("")

    mock_repo = MagicMock()
    mock_repo_class.return_value = mock_repo

    def diff_side_effect(*args, **kwargs):
        cmd = " ".join(args)
        if "--diff-filter=A" in cmd:
            return "tests/empty_test.py"
        return "tests/empty_test.py\nsrc/out_of_scope.py"

    mock_repo.git.diff.side_effect = diff_side_effect

    state.body_path.write_text("### Files to change\n\n- `src/in_scope.py`\n")

    result = _run(PrePushValidationNode(), state)

    assert isinstance(result, ImplementNode)
    assert "empty scratch file" in state.push_validation_failure
    assert "tests/empty_test.py" in state.push_validation_failure
    assert "outside the issue scope" in state.push_validation_failure
    assert "src/out_of_scope.py" in state.push_validation_failure


@patch("cai.workflows.pre_push_validate.Repo")
@patch("cai.workflows.pre_push_validate.stage_all")
def test_allow_list_docs_workflow_path(mock_stage, mock_repo_class, state, tmp_path):
    """Allow-listed docs/workflows/*.md paths pass even when not in 'Files to change'."""
    mock_repo = MagicMock()
    mock_repo_class.return_value = mock_repo

    def diff_side_effect(*args, **kwargs):
        cmd = " ".join(args)
        if "--diff-filter=A" in cmd:
            return ""
        return "docs/workflows/implement.md"

    mock_repo.git.diff.side_effect = diff_side_effect

    state.body_path.write_text("### Files to change\n\n- `src/in_scope.py`\n")

    result = _run(PrePushValidationNode(), state)

    assert isinstance(result, PRNode)


@patch("cai.workflows.pre_push_validate.Repo")
@patch("cai.workflows.pre_push_validate.stage_all")
def test_retry_count_increments_from_zero(mock_stage, mock_repo_class, state, tmp_path):
    """Starting from retry_count=0, one failure increments to 1."""
    empty_file = tmp_path / "tests" / "empty_test.py"
    empty_file.parent.mkdir(parents=True, exist_ok=True)
    empty_file.write_text("")

    mock_repo = MagicMock()
    mock_repo_class.return_value = mock_repo

    def diff_side_effect(*args, **kwargs):
        cmd = " ".join(args)
        if "--diff-filter=A" in cmd:
            return "tests/empty_test.py"
        return "tests/empty_test.py"

    mock_repo.git.diff.side_effect = diff_side_effect

    state.body_path.write_text("### Description\n\nSome issue.\n")

    result = _run(PrePushValidationNode(), state)

    assert isinstance(result, ImplementNode)
    assert state.push_validation_retry_count == 1

