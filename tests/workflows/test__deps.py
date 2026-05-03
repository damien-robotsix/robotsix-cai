from __future__ import annotations

from pathlib import Path

from pydantic_deep import DeepAgentDeps, LocalBackend

from cai.workflows._deps import _glob_dir_prefix, repo_deps


# ---------------------------------------------------------------------------
# _glob_dir_prefix
# ---------------------------------------------------------------------------


def test_glob_dir_prefix_no_wildcards():
    """Returns the full path when no wildcards are present."""
    result = _glob_dir_prefix("a/b/c")
    assert result == Path("a/b/c")


def test_glob_dir_prefix_stops_at_first_wildcard():
    """Stops at the first component containing a wildcard character."""
    result = _glob_dir_prefix("a/b/**/d")
    assert result == Path("a/b")


def test_glob_dir_prefix_stops_at_question_mark():
    """Stops at the first component containing a '?' character."""
    result = _glob_dir_prefix("a/b/?/d")
    assert result == Path("a/b")


def test_glob_dir_prefix_wildcard_in_first_component():
    """Returns root when the first component itself contains a wildcard."""
    result = _glob_dir_prefix("**/a/b")
    assert result == Path("/")


def test_glob_dir_prefix_single_part():
    """Returns root for a single wildcard part."""
    result = _glob_dir_prefix("*")
    assert result == Path("/")


def test_glob_dir_prefix_empty_pattern():
    """Returns root for an empty path pattern."""
    result = _glob_dir_prefix("")
    assert result == Path("/")


def test_glob_dir_prefix_stops_at_char_class():
    """Stops at the first component containing a '[' character."""
    result = _glob_dir_prefix("a/b/[c-d]/e")
    assert result == Path("a/b")


def test_glob_dir_prefix_absolute_path():
    """Works correctly with an absolute path containing wildcards."""
    result = _glob_dir_prefix("/tmp/foo/*.txt")
    assert result == Path("/tmp/foo")


# ---------------------------------------------------------------------------
# repo_deps -- structure
# ---------------------------------------------------------------------------


def test_repo_deps_returns_deep_agent_deps(tmp_path):
    """repo_deps returns a DeepAgentDeps instance."""
    deps = repo_deps(tmp_path)
    assert isinstance(deps, DeepAgentDeps)


def test_repo_deps_backend_is_local_backend(tmp_path):
    """The backend is a LocalBackend."""
    deps = repo_deps(tmp_path)
    assert isinstance(deps.backend, LocalBackend)


def test_repo_deps_root_dir_resolved(tmp_path):
    """The root_dir is the resolved absolute path of repo_root."""
    deps = repo_deps(tmp_path)
    assert str(deps.backend.root_dir) == str(tmp_path.resolve())


def test_repo_deps_allowed_directories_includes_repo_root(tmp_path):
    """The repo_root itself is always in allowed_directories."""
    deps = repo_deps(tmp_path)
    assert tmp_path.resolve() in deps.backend._allowed_directories


# ---------------------------------------------------------------------------
# repo_deps — write_dirs
# ---------------------------------------------------------------------------


def test_repo_deps_write_dirs_in_allowed_directories(tmp_path):
    """Each write_dir is included in allowed_directories."""
    write_a = tmp_path / "a"
    write_b = tmp_path / "b"
    deps = repo_deps(tmp_path, write_dirs=[write_a, write_b])
    allowed = list(deps.backend._allowed_directories)
    assert write_a.resolve() in allowed
    assert write_b.resolve() in allowed


def test_repo_deps_write_dirs_allow_write(tmp_path):
    """Write permission rules are created for each write_dir as a glob pattern."""
    write_dir = tmp_path / "output"
    deps = repo_deps(tmp_path, write_dirs=[write_dir])
    write_rules = deps.backend._permissions.write.rules
    expected_pattern = f"{write_dir.resolve()}/**"
    assert any(r.pattern == expected_pattern for r in write_rules)


def test_repo_deps_write_dirs_allow_edit(tmp_path):
    """Edit permission rules are created for each write_dir."""
    write_dir = tmp_path / "output"
    deps = repo_deps(tmp_path, write_dirs=[write_dir])
    edit_rules = deps.backend._permissions.edit.rules
    expected_pattern = f"{write_dir.resolve()}/**"
    assert any(r.pattern == expected_pattern for r in edit_rules)


# ---------------------------------------------------------------------------
# repo_deps — write_globs
# ---------------------------------------------------------------------------


def test_repo_deps_write_globs_in_allowed_directories(tmp_path):
    """Glob prefix directories are included in allowed_directories."""
    issue_dir = tmp_path / "issues" / "42"
    issue_dir.mkdir(parents=True, exist_ok=True)
    deps = repo_deps(tmp_path, write_globs=["issues/42/*.md"])
    allowed = list(deps.backend._allowed_directories)
    prefix_dir = tmp_path.resolve() / "issues" / "42"
    assert prefix_dir in allowed


def test_repo_deps_write_globs_allow_write(tmp_path):
    """Write permission rules are created for each write_glob."""
    deps = repo_deps(tmp_path, write_globs=["tmp/*.txt"])
    write_rules = deps.backend._permissions.write.rules
    assert any(r.pattern == "tmp/*.txt" for r in write_rules)


def test_repo_deps_write_globs_allow_edit(tmp_path):
    """Edit permission rules are created for each write_glob."""
    deps = repo_deps(tmp_path, write_globs=["tmp/*.txt"])
    edit_rules = deps.backend._permissions.edit.rules
    assert any(r.pattern == "tmp/*.txt" for r in edit_rules)


# ---------------------------------------------------------------------------
# repo_deps — no write arguments => read-only
# ---------------------------------------------------------------------------


def test_repo_deps_no_write_args_denies_write(tmp_path):
    """When no write_dirs or write_globs are given, write is denied."""
    deps = repo_deps(tmp_path)
    assert deps.backend._permissions.write.default == "deny"


def test_repo_deps_no_write_args_denies_edit(tmp_path):
    """When no write_dirs or write_globs are given, edit is denied."""
    deps = repo_deps(tmp_path)
    assert deps.backend._permissions.edit.default == "deny"


def test_repo_deps_no_write_args_has_empty_write_rules(tmp_path):
    """When no write args, write and edit rulesets are empty."""
    deps = repo_deps(tmp_path)
    assert deps.backend._permissions.write.rules == []
    assert deps.backend._permissions.edit.rules == []


# ---------------------------------------------------------------------------
# repo_deps — read/glob/grep/ls are allowed with excludes
# ---------------------------------------------------------------------------


def test_repo_deps_read_allowed(tmp_path):
    """Read operations are allowed by default."""
    deps = repo_deps(tmp_path)
    assert deps.backend._permissions.read.default == "allow"


def test_repo_deps_grep_allowed(tmp_path):
    """Grep operations are allowed by default."""
    deps = repo_deps(tmp_path)
    assert deps.backend._permissions.grep.default == "allow"


def test_repo_deps_glob_allowed(tmp_path):
    """Glob operations are allowed by default."""
    deps = repo_deps(tmp_path)
    assert deps.backend._permissions.glob.default == "allow"


def test_repo_deps_ls_allowed(tmp_path):
    """Ls operations are allowed by default."""
    deps = repo_deps(tmp_path)
    assert deps.backend._permissions.ls.default == "allow"


def test_repo_deps_read_has_exclude_rules(tmp_path):
    """Read operations have exclude rules (for __pycache__, .git, etc.)."""
    deps = repo_deps(tmp_path)
    patterns = {r.pattern for r in deps.backend._permissions.read.rules}
    assert "**/__pycache__/**" in patterns
    assert "**/.git/**" in patterns
    assert "**/node_modules/**" in patterns


def test_repo_deps_grep_has_exclude_rules(tmp_path):
    """Grep operations have the same exclude rules as read."""
    deps = repo_deps(tmp_path)
    assert deps.backend._permissions.grep.rules == deps.backend._permissions.read.rules


def test_repo_deps_glob_has_exclude_rules(tmp_path):
    """Glob operations have the same exclude rules as read."""
    deps = repo_deps(tmp_path)
    assert deps.backend._permissions.glob.rules == deps.backend._permissions.read.rules


def test_repo_deps_ls_has_exclude_rules(tmp_path):
    """Ls operations have the same exclude rules as read."""
    deps = repo_deps(tmp_path)
    assert deps.backend._permissions.ls.rules == deps.backend._permissions.read.rules


# ---------------------------------------------------------------------------
# repo_deps — execute always denied
# ---------------------------------------------------------------------------


def test_repo_deps_execute_denied(tmp_path):
    """Execute operations are always denied regardless of write args."""
    deps = repo_deps(tmp_path, write_dirs=[tmp_path])
    assert deps.backend._permissions.execute.default == "deny"
    assert deps.backend._permissions.execute.rules == []


# ---------------------------------------------------------------------------
# repo_deps — identity / immutability
# ---------------------------------------------------------------------------


def test_repo_deps_write_dirs_defaults_to_empty(tmp_path):
    """When write_dirs is None, no write rules are created."""
    deps = repo_deps(tmp_path)
    assert deps.backend._permissions.write.rules == []


def test_repo_deps_write_globs_defaults_to_empty(tmp_path):
    """When write_globs is None, no write rules are created."""
    deps = repo_deps(tmp_path, write_dirs=[tmp_path])
    assert deps.backend._permissions.write.rules != []
