"""Tests for read-only git history tools (git_log, git_diff, git_blame, git_show)."""
from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import pytest

from cai.tools.git_tools import (
    GIT_BLAME_TOOL,
    GIT_DIFF_TOOL,
    GIT_LOG_TOOL,
    GIT_SHOW_TOOL,
    _repo,
    git_blame,
    git_diff,
    git_log,
    git_show,
)


def _run(coro):
    return asyncio.run(coro)


def _make_ctx(root_dir: str = "/tmp/repo") -> MagicMock:
    """Build a minimal mock RunContext with deps.backend.root_dir."""
    ctx = MagicMock()
    ctx.deps.backend.root_dir = root_dir
    return ctx


# ---------------------------------------------------------------------------
# _repo helper
# ---------------------------------------------------------------------------


@patch("cai.tools.git_tools.Repo")
def test_repo_opens_from_ctx_root_dir(mock_repo_cls):
    """_repo instantiates Repo with the path from ctx.deps.backend.root_dir."""
    ctx = _make_ctx("/workspace/issue-42/repo")
    _repo(ctx)
    mock_repo_cls.assert_called_once_with("/workspace/issue-42/repo")


@patch("cai.tools.git_tools.Repo")
def test_repo_returns_repo_instance(mock_repo_cls):
    """_repo returns the Repo instance created by the constructor."""
    mock_instance = MagicMock()
    mock_repo_cls.return_value = mock_instance
    result = _repo(_make_ctx())
    assert result is mock_instance


# ---------------------------------------------------------------------------
# git_log
# ---------------------------------------------------------------------------


@patch("cai.tools.git_tools.Repo")
def test_git_log_default_args(mock_repo_cls):
    """git_log with default parameters passes -10 and a short format."""
    mock_repo = MagicMock()
    mock_repo.git.log.return_value = "2024-01-01 author: msg"
    mock_repo_cls.return_value = mock_repo

    result = _run(git_log(_make_ctx()))

    mock_repo.git.log.assert_called_once_with(
        "-10", "--format=%h %ad %an: %s", "--date=short"
    )
    assert result == "2024-01-01 author: msg"


@patch("cai.tools.git_tools.Repo")
def test_git_log_with_path(mock_repo_cls):
    """git_log with a path appends '-- <path>' to the call."""
    mock_repo = MagicMock()
    mock_repo.git.log.return_value = "abc123 2024-01-01 Alice: fix bug"
    mock_repo_cls.return_value = mock_repo

    result = _run(git_log(_make_ctx(), path="src/cai/tools/git_tools.py"))

    args = mock_repo.git.log.call_args[0]
    assert "-10" in args
    assert "--" in args
    assert "src/cai/tools/git_tools.py" in args
    assert result == "abc123 2024-01-01 Alice: fix bug"


@patch("cai.tools.git_tools.Repo")
def test_git_log_with_since(mock_repo_cls):
    """git_log with a date filter passes --since=<date>."""
    mock_repo = MagicMock()
    mock_repo.git.log.return_value = "def456 2024-03-01 Bob: feature"
    mock_repo_cls.return_value = mock_repo

    result = _run(git_log(_make_ctx(), since="2024-01-01"))

    mock_repo.git.log.assert_called_once_with(
        "-10", "--format=%h %ad %an: %s", "--date=short", "--since=2024-01-01"
    )
    assert result == "def456 2024-03-01 Bob: feature"


@patch("cai.tools.git_tools.Repo")
def test_git_log_with_path_and_since(mock_repo_cls):
    """git_log combines path and since filters correctly."""
    mock_repo = MagicMock()
    mock_repo.git.log.return_value = "abc123 2024-02-15 Charlie: docs"
    mock_repo_cls.return_value = mock_repo

    result = _run(git_log(_make_ctx(), path="README.md", since="2 weeks ago"))

    args = mock_repo.git.log.call_args[0]
    assert "-10" in args
    assert "--since=2 weeks ago" in args
    assert "--" in args
    assert "README.md" in args
    assert result == "abc123 2024-02-15 Charlie: docs"


@patch("cai.tools.git_tools.Repo")
def test_git_log_with_custom_max_count(mock_repo_cls):
    """git_log respects a custom max_count."""
    mock_repo = MagicMock()
    mock_repo.git.log.return_value = "commit1\ncommit2"
    mock_repo_cls.return_value = mock_repo

    _run(git_log(_make_ctx(), max_count=5))

    mock_repo.git.log.assert_called_once_with(
        "-5", "--format=%h %ad %an: %s", "--date=short"
    )


@patch("cai.tools.git_tools.Repo")
def test_git_log_empty_result(mock_repo_cls):
    """git_log returns '(no commits)' when the log is empty."""
    mock_repo = MagicMock()
    mock_repo.git.log.return_value = ""
    mock_repo_cls.return_value = mock_repo

    result = _run(git_log(_make_ctx()))
    assert result == "(no commits)"


@patch("cai.tools.git_tools.Repo")
def test_git_log_whitespace_only_result(mock_repo_cls):
    """git_log returns '(no commits)' for whitespace-only output."""
    mock_repo = MagicMock()
    mock_repo.git.log.return_value = "   \n  \n"
    mock_repo_cls.return_value = mock_repo

    result = _run(git_log(_make_ctx()))
    assert result == "(no commits)"


# ---------------------------------------------------------------------------
# git_diff
# ---------------------------------------------------------------------------


@patch("cai.tools.git_tools.Repo")
def test_git_diff_basic(mock_repo_cls):
    """git_diff passes the commit_range to git.diff."""
    mock_repo = MagicMock()
    mock_repo.git.diff.return_value = "diff --git a/foo.py b/foo.py\n..."
    mock_repo_cls.return_value = mock_repo

    result = _run(git_diff(_make_ctx(), "HEAD~3..HEAD"))

    mock_repo.git.diff.assert_called_once_with("HEAD~3..HEAD")
    assert result == "diff --git a/foo.py b/foo.py\n..."


@patch("cai.tools.git_tools.Repo")
def test_git_diff_single_ref(mock_repo_cls):
    """git_diff works with a single ref (e.g. HEAD~1)."""
    mock_repo = MagicMock()
    mock_repo.git.diff.return_value = "some diff"
    mock_repo_cls.return_value = mock_repo

    result = _run(git_diff(_make_ctx(), "HEAD~1"))

    mock_repo.git.diff.assert_called_once_with("HEAD~1")
    assert result == "some diff"


@patch("cai.tools.git_tools.Repo")
def test_git_diff_branch_compare(mock_repo_cls):
    """git_diff works with branch range expressions."""
    mock_repo = MagicMock()
    mock_repo.git.diff.return_value = "diff content"
    mock_repo_cls.return_value = mock_repo

    result = _run(git_diff(_make_ctx(), "main..feature"))

    mock_repo.git.diff.assert_called_once_with("main..feature")
    assert result == "diff content"


@patch("cai.tools.git_tools.Repo")
def test_git_diff_empty_result(mock_repo_cls):
    """git_diff returns '(no changes)' when diff is empty."""
    mock_repo = MagicMock()
    mock_repo.git.diff.return_value = ""
    mock_repo_cls.return_value = mock_repo

    result = _run(git_diff(_make_ctx(), "HEAD~1"))
    assert result == "(no changes)"


# ---------------------------------------------------------------------------
# git_blame
# ---------------------------------------------------------------------------


@patch("cai.tools.git_tools.Repo")
def test_git_blame_basic(mock_repo_cls):
    """git_blame passes the path to git.blame."""
    mock_repo = MagicMock()
    mock_repo.git.blame.return_value = "abc123 (Alice 2024-01-01 1) line content"
    mock_repo_cls.return_value = mock_repo

    result = _run(git_blame(_make_ctx(), "src/cai/tools/git_tools.py"))

    mock_repo.git.blame.assert_called_once_with("src/cai/tools/git_tools.py")
    assert result == "abc123 (Alice 2024-01-01 1) line content"


@patch("cai.tools.git_tools.Repo")
def test_git_blame_with_line_range(mock_repo_cls):
    """git_blame with start_line and end_line passes -L."""
    mock_repo = MagicMock()
    mock_repo.git.blame.return_value = "def456 (Bob 2024-02-15 10) line"
    mock_repo_cls.return_value = mock_repo

    result = _run(git_blame(_make_ctx(), "foo.py", start_line=10, end_line=20))

    mock_repo.git.blame.assert_called_once_with("-L", "10,20", "foo.py")
    assert result == "def456 (Bob 2024-02-15 10) line"


@patch("cai.tools.git_tools.Repo")
def test_git_blame_with_start_line_only(mock_repo_cls):
    """git_blame with only start_line passes '-L <start>,'."""
    mock_repo = MagicMock()
    mock_repo.git.blame.return_value = "output"
    mock_repo_cls.return_value = mock_repo

    result = _run(git_blame(_make_ctx(), "bar.py", start_line=42))

    mock_repo.git.blame.assert_called_once_with("-L", "42,", "bar.py")
    assert result == "output"


@patch("cai.tools.git_tools.Repo")
def test_git_blame_empty_result(mock_repo_cls):
    """git_blame returns '(no output)' when blame is empty."""
    mock_repo = MagicMock()
    mock_repo.git.blame.return_value = ""
    mock_repo_cls.return_value = mock_repo

    result = _run(git_blame(_make_ctx(), "missing.py"))
    assert result == "(no output)"


# ---------------------------------------------------------------------------
# git_show
# ---------------------------------------------------------------------------


@patch("cai.tools.git_tools.Repo")
def test_git_show_sha(mock_repo_cls):
    """git_show passes the commit ref to git.show."""
    mock_repo = MagicMock()
    mock_repo.git.show.return_value = "commit abc123\nAuthor: Alice\n\ndiff..."
    mock_repo_cls.return_value = mock_repo

    result = _run(git_show(_make_ctx(), "abc123def"))

    mock_repo.git.show.assert_called_once_with("abc123def")
    assert result == "commit abc123\nAuthor: Alice\n\ndiff..."


@patch("cai.tools.git_tools.Repo")
def test_git_show_relative_ref(mock_repo_cls):
    """git_show works with relative refs like HEAD~1."""
    mock_repo = MagicMock()
    mock_repo.git.show.return_value = "commit info"
    mock_repo_cls.return_value = mock_repo

    result = _run(git_show(_make_ctx(), "HEAD~1"))

    mock_repo.git.show.assert_called_once_with("HEAD~1")
    assert result == "commit info"


@patch("cai.tools.git_tools.Repo")
def test_git_show_branch_ref(mock_repo_cls):
    """git_show works with branch names."""
    mock_repo = MagicMock()
    mock_repo.git.show.return_value = "commit info"
    mock_repo_cls.return_value = mock_repo

    result = _run(git_show(_make_ctx(), "main"))

    mock_repo.git.show.assert_called_once_with("main")
    assert result == "commit info"


@patch("cai.tools.git_tools.Repo")
def test_git_show_empty_result(mock_repo_cls):
    """git_show returns '(no output)' when show is empty."""
    mock_repo = MagicMock()
    mock_repo.git.show.return_value = ""
    mock_repo_cls.return_value = mock_repo

    result = _run(git_show(_make_ctx(), "HEAD"))
    assert result == "(no output)"


# ---------------------------------------------------------------------------
# Tool constants
# ---------------------------------------------------------------------------


def test_git_log_tool_is_tool_instance():
    """GIT_LOG_TOOL is a pydantic_ai Tool."""
    from pydantic_ai import Tool

    assert isinstance(GIT_LOG_TOOL, Tool)


def test_git_log_tool_name():
    """The tool name matches the function name."""
    assert GIT_LOG_TOOL.name == "git_log"


def test_git_diff_tool_is_tool_instance():
    """GIT_DIFF_TOOL is a pydantic_ai Tool."""
    from pydantic_ai import Tool

    assert isinstance(GIT_DIFF_TOOL, Tool)


def test_git_diff_tool_name():
    """The tool name matches the function name."""
    assert GIT_DIFF_TOOL.name == "git_diff"


def test_git_blame_tool_is_tool_instance():
    """GIT_BLAME_TOOL is a pydantic_ai Tool."""
    from pydantic_ai import Tool

    assert isinstance(GIT_BLAME_TOOL, Tool)


def test_git_blame_tool_name():
    """The tool name matches the function name."""
    assert GIT_BLAME_TOOL.name == "git_blame"


def test_git_show_tool_is_tool_instance():
    """GIT_SHOW_TOOL is a pydantic_ai Tool."""
    from pydantic_ai import Tool

    assert isinstance(GIT_SHOW_TOOL, Tool)


def test_git_show_tool_name():
    """The tool name matches the function name."""
    assert GIT_SHOW_TOOL.name == "git_show"


# ---------------------------------------------------------------------------
# Registration in TOOL_FACTORIES
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "key, expected_target",
    [
        ("git_log", "cai.tools.git_tools:GIT_LOG_TOOL"),
        ("git_diff", "cai.tools.git_tools:GIT_DIFF_TOOL"),
        ("git_blame", "cai.tools.git_tools:GIT_BLAME_TOOL"),
        ("git_show", "cai.tools.git_tools:GIT_SHOW_TOOL"),
    ],
)
def test_git_tools_registered_in_tool_factories(key, expected_target):
    """Each git tool is registered under its key in loader.py."""
    from cai.agents.loader import TOOL_FACTORIES

    assert key in TOOL_FACTORIES
    assert TOOL_FACTORIES[key] == expected_target


@pytest.mark.parametrize(
    "key, expected_tool",
    [
        ("git_log", GIT_LOG_TOOL),
        ("git_diff", GIT_DIFF_TOOL),
        ("git_blame", GIT_BLAME_TOOL),
        ("git_show", GIT_SHOW_TOOL),
    ],
)
def test_import_factory_resolves_git_tool(key, expected_tool):
    """The factory target string imports and returns the correct tool."""
    from cai.agents.loader import TOOL_FACTORIES, _import_factory

    tool = _import_factory(TOOL_FACTORIES[key])
    assert tool is expected_tool


# ---------------------------------------------------------------------------
# Module docstring
# ---------------------------------------------------------------------------


def test_module_docstring_exists():
    """The git_tools module has a docstring describing the tool's purpose."""
    import cai.tools.git_tools as gt

    assert gt.__doc__ is not None
    assert len(gt.__doc__) > 0
    assert "read-only" in gt.__doc__.lower()
