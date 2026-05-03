from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import pytest

from cai.workflows.pre_push_validate import PrePushValidationNode, _is_allow_listed, _parse_files_to_change
from cai.workflows.implement import ImplementNode
from cai.workflows.pr import PRNode

def _run(node, state):
    ctx = MagicMock()
    ctx.state = state
    return asyncio.run(node.run(ctx))


# ---------------------------------------------------------------------------
# _parse_files_to_change
# ---------------------------------------------------------------------------


def test_parse_files_to_change_bullet_list():
    body = "### Files to change\n\n- `src/foo.py`\n- `tests/test_foo.py`"
    result = _parse_files_to_change(body)
    assert result == ["src/foo.py", "tests/test_foo.py"]


def test_parse_files_to_change_star_bullets():
    body = "## Files\n\n* src/bar.py\n* docs/readme.md"
    result = _parse_files_to_change(body)
    assert result == ["src/bar.py", "docs/readme.md"]


def test_parse_files_to_change_no_section():
    body = "## Description\n\nSome text.\n\n### Other\n\n- not-a-file"
    result = _parse_files_to_change(body)
    assert result is None


def test_parse_files_to_change_stops_at_next_heading():
    body = "### Files to change\n\n- `a.py`\n\n## Next section\n\n- `b.py`"
    result = _parse_files_to_change(body)
    assert result == ["a.py"]


def test_parse_files_to_change_handles_parenthetical_comments():
    body = "### Files to change\n\n- `src/foo.py` (new file)\n- tests/bar.py (update)"
    result = _parse_files_to_change(body)
    assert result == ["src/foo.py", "tests/bar.py"]


def test_parse_files_to_change_comma_separated():
    body = "### Files to change\n\nsrc/foo.py, tests/test_foo.py, docs/readme.md"
    result = _parse_files_to_change(body)
    assert result == ["src/foo.py", "tests/test_foo.py", "docs/readme.md"]


def test_parse_files_to_change_empty_body():
    """An empty body returns None."""
    assert _parse_files_to_change("") is None


def test_parse_files_to_change_only_code_block():
    """A body with only a code block in the files section returns empty list -> None."""
    body = "### Files to change\n\n```\nnot a real file\n```"
    result = _parse_files_to_change(body)
    assert result is None


def test_parse_files_to_change_handles_mixed_list_and_comma():
    """Mixed bullet list items and comma-separated lines."""
    body = "### Files to change\n\n- `src/foo.py`\nsrc/bar.py, src/baz.py"
    result = _parse_files_to_change(body)
    assert result == ["src/foo.py", "src/bar.py", "src/baz.py"]


def test_parse_files_to_change_code_fence_surrounding_files():
    """Files before and after a code fence should both be collected."""
    body = "### Files to change\n\n- `src/keep.py`\n\n```\njunk content\n```\n\n- `src/also_keep.py`"
    result = _parse_files_to_change(body)
    assert result == ["src/keep.py", "src/also_keep.py"]


def test_parse_files_to_change_unclosed_fence():
    """An unclosed code fence should cause all content after it to be skipped."""
    body = "### Files to change\n\n- `src/before.py`\n\n```\nunclosed block\n- `src/after.py`"
    result = _parse_files_to_change(body)
    assert result == ["src/before.py"]


def test_parse_files_to_change_multiple_fences():
    """Multiple alternating code fences should toggle correctly."""
    body = (
        "### Files to change\n\n"
        "- `src/first.py`\n\n"
        "```\njunk\n```\n\n"
        "- `src/second.py`\n\n"
        "```\nmore junk\n```\n\n"
        "- `src/third.py`"
    )
    result = _parse_files_to_change(body)
    assert result == ["src/first.py", "src/second.py", "src/third.py"]


# ---------------------------------------------------------------------------
# _is_allow_listed
# ---------------------------------------------------------------------------


def test_is_allow_listed_workflow_yaml():
    assert _is_allow_listed(".github/workflows/cai-foo.yml") is True


def test_is_allow_listed_workflow_yaml_not_matching_pattern():
    assert _is_allow_listed(".github/workflows/build.yml") is False


def test_is_allow_listed_docs_workflows_md():
    assert _is_allow_listed("docs/workflows/solve.md") is True


def test_is_allow_listed_random_file():
    assert _is_allow_listed("src/foo.py") is False


def test_is_allow_listed_empty_string():
    assert _is_allow_listed("") is False


# ---------------------------------------------------------------------------
# Empty-file gate
# ---------------------------------------------------------------------------


@patch("cai.workflows.pre_push_validate.stage_all")
@patch("cai.workflows.pre_push_validate.Repo")
def test_empty_new_file_fails(mock_repo_class, mock_stage_all, state, tmp_path):
    """Stage a new empty file, assert run() returns ImplementNode and
    push_validation_failure names the file."""
    mock_repo = MagicMock()
    mock_repo_class.return_value = mock_repo
    mock_repo.git.diff.side_effect = lambda *args: {
        ("--cached", "--name-only", "--diff-filter=A", "main..."): "tests/empty.py\n",
        ("--cached", "--name-only", "main..."): "tests/empty.py\n",
    }.get(tuple(args), "")

    # Create the empty file on disk
    empty_file = tmp_path / "tests" / "empty.py"
    empty_file.parent.mkdir(parents=True, exist_ok=True)
    empty_file.write_text("")

    # Write body with Files to change section so out-of-scope doesn't fire
    state.body_path.write_text("### Files to change\n\n- tests/empty.py\n")

    result = _run(PrePushValidationNode(), state)

    assert isinstance(result, ImplementNode)
    assert "empty scratch file" in state.push_validation_failure
    assert "tests/empty.py" in state.push_validation_failure
    assert state.push_validation_retry_count == 1


@patch("cai.workflows.pre_push_validate.stage_all")
@patch("cai.workflows.pre_push_validate.Repo")
def test_non_empty_new_file_passes(mock_repo_class, mock_stage_all, state, tmp_path):
    """Stage a new non-empty file, assert empty-file gate does not fire."""
    mock_repo = MagicMock()
    mock_repo_class.return_value = mock_repo
    mock_repo.git.diff.side_effect = lambda *args: {
        ("--cached", "--name-only", "--diff-filter=A", "main..."): "tests/nonempty.py\n",
        ("--cached", "--name-only", "main..."): "tests/nonempty.py\n",
    }.get(tuple(args), "")

    # Create the non-empty file on disk
    nonempty_file = tmp_path / "tests" / "nonempty.py"
    nonempty_file.parent.mkdir(parents=True, exist_ok=True)
    nonempty_file.write_text("x = 1\n")

    state.body_path.write_text("### Files to change\n\n- tests/nonempty.py\n")

    result = _run(PrePushValidationNode(), state)

    assert isinstance(result, PRNode)
    # No failure message should have been set
    assert state.push_validation_failure == ""


@patch("cai.workflows.pre_push_validate.stage_all")
@patch("cai.workflows.pre_push_validate.Repo")
def test_empty_file_not_on_disk_skipped(mock_repo_class, mock_stage_all, state, tmp_path):
    """A file listed in git diff but that doesn't exist on disk is skipped
    (OSError path is handled gracefully)."""
    mock_repo = MagicMock()
    mock_repo_class.return_value = mock_repo
    mock_repo.git.diff.side_effect = lambda *args: {
        ("--cached", "--name-only", "--diff-filter=A", "main..."): "tests/ghost.py\n",
        ("--cached", "--name-only", "main..."): "tests/ghost.py\n",
    }.get(tuple(args), "")

    # Do NOT create the file on disk — it appears in git but is missing from the filesystem
    state.body_path.write_text("### Files to change\n\n- src/foo.py\n")

    result = _run(PrePushValidationNode(), state)

    # Should pass through empty-file gate (file doesn't exist) and out-of-scope gate
    # (declared_files is ["src/foo.py"], staged is ["tests/ghost.py"])
    assert isinstance(result, ImplementNode)
    assert "outside the issue scope" in state.push_validation_failure


@patch("cai.workflows.pre_push_validate.stage_all")
@patch("cai.workflows.pre_push_validate.Repo")
def test_multiple_empty_files_fails(mock_repo_class, mock_stage_all, state, tmp_path):
    """Stage multiple new empty files, assert all are named in failure message."""
    mock_repo = MagicMock()
    mock_repo_class.return_value = mock_repo
    mock_repo.git.diff.side_effect = lambda *args: {
        ("--cached", "--name-only", "--diff-filter=A", "main..."): (
            "src/a.py\nsrc/b.py\nsrc/c.py\n"
        ),
        ("--cached", "--name-only", "main..."): "src/a.py\nsrc/b.py\nsrc/c.py\n",
    }.get(tuple(args), "")

    for name in ("a.py", "b.py", "c.py"):
        p = tmp_path / "src" / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("")

    state.body_path.write_text("### Files to change\n\n- src/a.py\n- src/b.py\n- src/c.py\n")

    result = _run(PrePushValidationNode(), state)

    assert isinstance(result, ImplementNode)
    assert "empty scratch file" in state.push_validation_failure
    assert "src/a.py" in state.push_validation_failure
    assert "src/b.py" in state.push_validation_failure
    assert "src/c.py" in state.push_validation_failure


# ---------------------------------------------------------------------------
# Out-of-scope gate
# ---------------------------------------------------------------------------


@patch("cai.workflows.pre_push_validate.stage_all")
@patch("cai.workflows.pre_push_validate.Repo")
def test_out_of_scope_file_fails(mock_repo_class, mock_stage_all, state, tmp_path):
    """Stage a file not in 'Files to change', assert it returns
    ImplementNode with the expected message."""
    mock_repo = MagicMock()
    mock_repo_class.return_value = mock_repo
    mock_repo.git.diff.side_effect = lambda *args: {
        ("--cached", "--name-only", "--diff-filter=A", "main..."): "",
        ("--cached", "--name-only", "main..."): "src/other.py\n",
    }.get(tuple(args), "")

    state.body_path.write_text("### Files to change\n\n- src/foo.py\n")

    result = _run(PrePushValidationNode(), state)

    assert isinstance(result, ImplementNode)
    assert "outside the issue scope" in state.push_validation_failure
    assert "src/other.py" in state.push_validation_failure
    assert state.push_validation_retry_count == 1


# ---------------------------------------------------------------------------
# All files in scope passes
# ---------------------------------------------------------------------------


@patch("cai.workflows.pre_push_validate.stage_all")
@patch("cai.workflows.pre_push_validate.Repo")
def test_all_files_in_scope_passes(mock_repo_class, mock_stage_all, state):
    """Stage only files listed in 'Files to change', assert it returns PRNode."""
    mock_repo = MagicMock()
    mock_repo_class.return_value = mock_repo
    mock_repo.git.diff.side_effect = lambda *args: {
        ("--cached", "--name-only", "--diff-filter=A", "main..."): "",
        ("--cached", "--name-only", "main..."): "src/foo.py\n",
    }.get(tuple(args), "")

    state.body_path.write_text("### Files to change\n\n- src/foo.py\n")

    result = _run(PrePushValidationNode(), state)

    assert isinstance(result, PRNode)


# ---------------------------------------------------------------------------
# Allow-list files pass
# ---------------------------------------------------------------------------


@patch("cai.workflows.pre_push_validate.stage_all")
@patch("cai.workflows.pre_push_validate.Repo")
def test_allow_list_files_pass(mock_repo_class, mock_stage_all, state):
    """Stage only .github/workflows/cai-foo.yml as out-of-scope, assert
    it returns PRNode."""
    mock_repo = MagicMock()
    mock_repo_class.return_value = mock_repo
    mock_repo.git.diff.side_effect = lambda *args: {
        ("--cached", "--name-only", "--diff-filter=A", "main..."): "",
        ("--cached", "--name-only", "main..."): ".github/workflows/cai-foo.yml\n",
    }.get(tuple(args), "")

    state.body_path.write_text("### Files to change\n\n- src/foo.py\n")

    result = _run(PrePushValidationNode(), state)

    assert isinstance(result, PRNode)


# ---------------------------------------------------------------------------
# No "Files to change" section skips out-of-scope gate
# ---------------------------------------------------------------------------


@patch("cai.workflows.pre_push_validate.stage_all")
@patch("cai.workflows.pre_push_validate.Repo")
def test_no_files_section_skips_out_of_scope_gate(mock_repo_class, mock_stage_all, state):
    """Body without 'Files to change' section, any file passes."""
    mock_repo = MagicMock()
    mock_repo_class.return_value = mock_repo
    mock_repo.git.diff.side_effect = lambda *args: {
        ("--cached", "--name-only", "--diff-filter=A", "main..."): "",
        ("--cached", "--name-only", "main..."): "src/anything.py\n",
    }.get(tuple(args), "")

    state.body_path.write_text("## Description\n\nSome text without a files section.\n")

    result = _run(PrePushValidationNode(), state)

    assert isinstance(result, PRNode)


# ---------------------------------------------------------------------------
# Retry exhaustion
# ---------------------------------------------------------------------------


@patch("cai.workflows.pre_push_validate.stage_all")
@patch("cai.workflows.pre_push_validate.Repo")
def test_retry_exhaustion_raises(mock_repo_class, mock_stage_all, state, tmp_path):
    """After 3 attempts (count >= 2), assert RuntimeError is raised."""
    mock_repo = MagicMock()
    mock_repo_class.return_value = mock_repo
    mock_repo.git.diff.side_effect = lambda *args: {
        ("--cached", "--name-only", "--diff-filter=A", "main..."): "tests/empty.py\n",
        ("--cached", "--name-only", "main..."): "tests/empty.py\n",
    }.get(tuple(args), "")

    # Create the empty file on disk
    empty_file = tmp_path / "tests" / "empty.py"
    empty_file.parent.mkdir(parents=True, exist_ok=True)
    empty_file.write_text("")

    state.body_path.write_text("### Files to change\n\n- tests/empty.py\n")
    state.push_validation_retry_count = 2

    with pytest.raises(RuntimeError, match="empty scratch file"):
        _run(PrePushValidationNode(), state)


@patch("cai.workflows.pre_push_validate.stage_all")
@patch("cai.workflows.pre_push_validate.Repo")
def test_retry_exhaustion_out_of_scope_raises(mock_repo_class, mock_stage_all, state):
    """After 3 attempts via out-of-scope gate, assert RuntimeError is raised."""
    mock_repo = MagicMock()
    mock_repo_class.return_value = mock_repo
    mock_repo.git.diff.side_effect = lambda *args: {
        ("--cached", "--name-only", "--diff-filter=A", "main..."): "",
        ("--cached", "--name-only", "main..."): "src/unlisted.py\n",
    }.get(tuple(args), "")

    state.body_path.write_text("### Files to change\n\n- src/foo.py\n")
    state.push_validation_retry_count = 2

    with pytest.raises(RuntimeError, match="outside the issue scope"):
        _run(PrePushValidationNode(), state)


# ---------------------------------------------------------------------------
# cai:human-review skips
# ---------------------------------------------------------------------------


@patch("cai.workflows.pre_push_validate.stage_all")
@patch("cai.workflows.pre_push_validate.Repo")
def test_human_review_skips(mock_repo_class, mock_stage_all, state, tmp_path):
    """Label present, assert it returns PRNode regardless."""
    mock_repo = MagicMock()
    mock_repo_class.return_value = mock_repo
    mock_repo.git.diff.side_effect = lambda *args: {
        ("--cached", "--name-only", "--diff-filter=A", "main..."): "tests/empty.py\n",
        ("--cached", "--name-only", "main..."): "tests/empty.py\n",
    }.get(tuple(args), "")

    # Create the empty file on disk
    empty_file = tmp_path / "tests" / "empty.py"
    empty_file.parent.mkdir(parents=True, exist_ok=True)
    empty_file.write_text("")

    state.body_path.write_text("### Files to change\n\n- src/foo.py\n")
    state.meta.labels.append("cai:human-review")

    result = _run(PrePushValidationNode(), state)

    assert isinstance(result, PRNode)
