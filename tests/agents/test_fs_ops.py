"""Tests for fs_ops tools (move_file, delete_file, batch_move, batch_delete)."""
from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from cai.agents.fs_ops import (
    BATCH_DELETE_TOOL,
    BATCH_MOVE_TOOL,
    DELETE_FILE_TOOL,
    MOVE_FILE_TOOL,
    _resolve,
    batch_delete,
    batch_move,
    delete_file,
    move_file,
)


def _run(coro):
    return asyncio.run(coro)


def _make_ctx(root_dir: str = "/tmp/repo") -> MagicMock:
    """Build a minimal mock RunContext with deps.backend.root_dir."""
    ctx = MagicMock()
    ctx.deps.backend.root_dir = root_dir
    return ctx


# ---------------------------------------------------------------------------
# _resolve helper
# ---------------------------------------------------------------------------


def test_resolve_normal_path():
    """_resolve returns the absolute path within the repo root."""
    ctx = _make_ctx("/workspace/repo")
    result = _resolve(ctx, "src/foo.py")
    assert result == Path("/workspace/repo/src/foo.py").resolve()


def test_resolve_escape_raises_permission_error():
    """_resolve raises PermissionError when the path escapes the repo root."""
    ctx = _make_ctx("/workspace/repo")
    with pytest.raises(PermissionError, match="escapes repository root"):
        _resolve(ctx, "../outside.txt")


def test_resolve_absolute_path_outside():
    """Absolute paths outside the root are rejected."""
    ctx = _make_ctx("/workspace/repo")
    with pytest.raises(PermissionError, match="escapes repository root"):
        _resolve(ctx, "/etc/passwd")


def test_resolve_symlink_escape(tmp_path):
    """A symlink pointing outside the repo root is rejected."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    outside = tmp_path / "outside"
    outside.write_text("secret")
    link = repo_root / "link"
    link.symlink_to(outside, target_is_directory=False)

    ctx = _make_ctx(str(repo_root))
    with pytest.raises(PermissionError, match="escapes repository root"):
        _resolve(ctx, "link")


# ---------------------------------------------------------------------------
# move_file
# ---------------------------------------------------------------------------


def test_move_file_success(tmp_path):
    """move_file moves a file from source to destination."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    src = repo_root / "old.py"
    src.write_text("content")
    dst_path = repo_root / "subdir" / "new.py"

    ctx = _make_ctx(str(repo_root))
    result = _run(move_file(ctx, "old.py", "subdir/new.py"))

    assert "Moved" in result
    assert "old.py" in result
    assert "subdir/new.py" in result
    assert not src.exists()
    assert dst_path.exists()
    assert dst_path.read_text() == "content"


def test_move_file_source_not_exist(tmp_path):
    """move_file returns an error string when the source does not exist."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    ctx = _make_ctx(str(repo_root))
    result = _run(move_file(ctx, "nonexistent.py", "dest.py"))

    assert "Source does not exist" in result
    assert "nonexistent.py" in result


def test_move_file_permission_error():
    """move_file returns the PermissionError message when _resolve rejects."""
    ctx = _make_ctx("/workspace/repo")
    result = _run(move_file(ctx, "../escape.txt", "dest.py"))

    assert "Path escapes repository root" in result


def test_move_file_destination_creates_parent(tmp_path):
    """move_file creates parent directories for the destination."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    src = repo_root / "old.py"
    src.write_text("data")

    ctx = _make_ctx(str(repo_root))
    result = _run(move_file(ctx, "old.py", "a/b/c/new.py"))

    assert "Moved" in result
    assert (repo_root / "a" / "b" / "c" / "new.py").exists()


def test_move_file_renames_directory(tmp_path):
    """move_file can rename a directory."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    old_dir = repo_root / "old_pkg"
    old_dir.mkdir()
    (old_dir / "__init__.py").write_text("")
    (old_dir / "mod.py").write_text("x=1")

    ctx = _make_ctx(str(repo_root))
    result = _run(move_file(ctx, "old_pkg", "new_pkg"))

    assert "Moved" in result
    assert not old_dir.exists()
    assert (repo_root / "new_pkg").is_dir()
    assert (repo_root / "new_pkg" / "mod.py").exists()


# ---------------------------------------------------------------------------
# delete_file
# ---------------------------------------------------------------------------


def test_delete_file_success(tmp_path):
    """delete_file removes a regular file."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    f = repo_root / "target.py"
    f.write_text("delete me")

    ctx = _make_ctx(str(repo_root))
    result = _run(delete_file(ctx, "target.py"))

    assert "Deleted file" in result
    assert "target.py" in result
    assert not f.exists()


def test_delete_directory(tmp_path):
    """delete_file removes a directory recursively."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    d = repo_root / "pkg"
    d.mkdir()
    (d / "mod.py").write_text("x=1")

    ctx = _make_ctx(str(repo_root))
    result = _run(delete_file(ctx, "pkg"))

    assert "Deleted directory" in result
    assert "pkg" in result
    assert not d.exists()


def test_delete_file_not_exist(tmp_path):
    """delete_file returns an error when the path does not exist."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    ctx = _make_ctx(str(repo_root))
    result = _run(delete_file(ctx, "missing.py"))

    assert "Path does not exist" in result
    assert "missing.py" in result


def test_delete_file_permission_error():
    """delete_file returns the PermissionError message when _resolve rejects."""
    ctx = _make_ctx("/workspace/repo")
    result = _run(delete_file(ctx, "../escape.txt"))

    assert "Path escapes repository root" in result


# ---------------------------------------------------------------------------
# batch_move
# ---------------------------------------------------------------------------


def test_batch_move_success(tmp_path):
    """batch_move moves multiple files and returns a summary."""
    from cai.agents.fs_ops import MoveOp

    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    (repo_root / "a.py").write_text("a")
    (repo_root / "b.py").write_text("b")

    ctx = _make_ctx(str(repo_root))
    moves = [MoveOp(source="a.py", destination="dst/a.py"), MoveOp(source="b.py", destination="dst/b.py")]
    result = _run(batch_move(ctx, moves))

    assert "Moved 2 path(s)" in result
    assert "a.py" in result
    assert "b.py" in result
    assert not (repo_root / "a.py").exists()
    assert not (repo_root / "b.py").exists()
    assert (repo_root / "dst" / "a.py").exists()
    assert (repo_root / "dst" / "b.py").exists()


def test_batch_move_source_not_exist_aborts_all(tmp_path):
    """If any source does not exist, nothing is moved and the error is returned."""
    from cai.agents.fs_ops import MoveOp

    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    (repo_root / "a.py").write_text("a")

    ctx = _make_ctx(str(repo_root))
    moves = [MoveOp(source="a.py", destination="dst/a.py"), MoveOp(source="missing.py", destination="dst/missing.py")]
    result = _run(batch_move(ctx, moves))

    assert "Source does not exist" in result
    assert "missing.py" in result
    # a.py should not have been moved (pre-validation fails before any moves)
    assert (repo_root / "a.py").exists()


def test_batch_move_permission_error_aborts_all(tmp_path):
    """If any path escapes the repo, nothing is moved."""
    from cai.agents.fs_ops import MoveOp

    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    (repo_root / "a.py").write_text("a")

    ctx = _make_ctx(str(repo_root))
    moves = [MoveOp(source="a.py", destination="dst/a.py"), MoveOp(source="../escape.py", destination="dst/e.py")]
    result = _run(batch_move(ctx, moves))

    assert "escapes repository root" in result
    assert (repo_root / "a.py").exists()


def test_batch_move_empty_moves(tmp_path):
    """An empty moves list returns a zero-move summary."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    ctx = _make_ctx(str(repo_root))
    result = _run(batch_move(ctx, []))

    assert "Moved 0 path(s)" in result


# ---------------------------------------------------------------------------
# batch_delete
# ---------------------------------------------------------------------------


def test_batch_delete_success(tmp_path):
    """batch_delete removes multiple files and returns a summary."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    (repo_root / "a.py").write_text("a")
    (repo_root / "b.py").write_text("b")

    ctx = _make_ctx(str(repo_root))
    result = _run(batch_delete(ctx, ["a.py", "b.py"]))

    assert "Deleted 2 path(s)" in result
    assert "Deleted file" in result
    assert "a.py" in result
    assert "b.py" in result
    assert not (repo_root / "a.py").exists()
    assert not (repo_root / "b.py").exists()


def test_batch_delete_mixed_dirs_and_files(tmp_path):
    """batch_delete handles a mix of files and directories."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    (repo_root / "f.py").write_text("f")
    d = repo_root / "pkg"
    d.mkdir()
    (d / "mod.py").write_text("m")

    ctx = _make_ctx(str(repo_root))
    result = _run(batch_delete(ctx, ["f.py", "pkg"]))

    assert "Deleted 2 path(s)" in result
    assert "Deleted file" in result
    assert "Deleted directory" in result
    assert not (repo_root / "f.py").exists()
    assert not d.exists()


def test_batch_delete_path_not_exist_aborts_all(tmp_path):
    """If any path does not exist, nothing is deleted and the error is returned."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    (repo_root / "keep.py").write_text("keep")

    ctx = _make_ctx(str(repo_root))
    result = _run(batch_delete(ctx, ["keep.py", "missing.py"]))

    assert "Path does not exist" in result
    assert "missing.py" in result
    # keep.py should not have been deleted
    assert (repo_root / "keep.py").exists()


def test_batch_delete_permission_error_aborts_all(tmp_path):
    """If any path escapes the repo, nothing is deleted."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    (repo_root / "keep.py").write_text("keep")

    ctx = _make_ctx(str(repo_root))
    result = _run(batch_delete(ctx, ["keep.py", "../escape.txt"]))

    assert "escapes repository root" in result
    assert (repo_root / "keep.py").exists()


def test_batch_delete_empty_paths(tmp_path):
    """An empty paths list returns a zero-delete summary."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    ctx = _make_ctx(str(repo_root))
    result = _run(batch_delete(ctx, []))

    assert "Deleted 0 path(s)" in result


# ---------------------------------------------------------------------------
# MOVE_FILE_TOOL / DELETE_FILE_TOOL / BATCH_MOVE_TOOL / BATCH_DELETE_TOOL
# ---------------------------------------------------------------------------


def test_move_file_tool_is_tool_instance():
    """MOVE_FILE_TOOL is a pydantic_ai Tool."""
    from pydantic_ai import Tool

    assert isinstance(MOVE_FILE_TOOL, Tool)


def test_move_file_tool_name():
    """The tool name matches the function name."""
    assert MOVE_FILE_TOOL.name == "move_file"


def test_delete_file_tool_is_tool_instance():
    """DELETE_FILE_TOOL is a pydantic_ai Tool."""
    from pydantic_ai import Tool

    assert isinstance(DELETE_FILE_TOOL, Tool)


def test_delete_file_tool_name():
    """The tool name matches the function name."""
    assert DELETE_FILE_TOOL.name == "delete_file"


def test_batch_move_tool_is_tool_instance():
    """BATCH_MOVE_TOOL is a pydantic_ai Tool."""
    from pydantic_ai import Tool

    assert isinstance(BATCH_MOVE_TOOL, Tool)


def test_batch_move_tool_name():
    """The tool name matches the function name."""
    assert BATCH_MOVE_TOOL.name == "batch_move"


def test_batch_delete_tool_is_tool_instance():
    """BATCH_DELETE_TOOL is a pydantic_ai Tool."""
    from pydantic_ai import Tool

    assert isinstance(BATCH_DELETE_TOOL, Tool)


def test_batch_delete_tool_name():
    """The tool name matches the function name."""
    assert BATCH_DELETE_TOOL.name == "batch_delete"


# ---------------------------------------------------------------------------
# TOOL_FACTORIES registration
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "key, expected_target",
    [
        ("move_file", "cai.agents.fs_ops:MOVE_FILE_TOOL"),
        ("delete_file", "cai.agents.fs_ops:DELETE_FILE_TOOL"),
        ("batch_move", "cai.agents.fs_ops:BATCH_MOVE_TOOL"),
        ("batch_delete", "cai.agents.fs_ops:BATCH_DELETE_TOOL"),
    ],
)
def test_fs_ops_tools_registered_in_tool_factories(key, expected_target):
    """Each fs_ops tool is registered under its key in loader.py."""
    from cai.agents.loader import TOOL_FACTORIES

    assert key in TOOL_FACTORIES
    assert TOOL_FACTORIES[key] == expected_target


@pytest.mark.parametrize(
    "key, expected_tool",
    [
        ("move_file", MOVE_FILE_TOOL),
        ("delete_file", DELETE_FILE_TOOL),
        ("batch_move", BATCH_MOVE_TOOL),
        ("batch_delete", BATCH_DELETE_TOOL),
    ],
)
def test_import_factory_resolves_fs_ops_tool(key, expected_tool):
    """The factory target string imports and returns the correct tool."""
    from cai.agents.loader import TOOL_FACTORIES, _import_factory

    tool = _import_factory(TOOL_FACTORIES[key])
    assert tool is expected_tool


# ---------------------------------------------------------------------------
# Module docstring
# ---------------------------------------------------------------------------


def test_module_docstring_exists():
    """The fs_ops module has a docstring describing the tool's purpose."""
    import cai.agents.fs_ops as fs

    assert fs.__doc__ is not None
    assert len(fs.__doc__) > 0
