"""Tests for spike_run tool and its pure helper functions."""
from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from cai.agents.spike_tool import (
    _OUTPUT_CAP,
    _PASSTHROUGH,
    _REDACT_ENV,
    _redact,
    _scratch_dir,
    _scrubbed_env,
    _truncate,
    SPIKE_RUN_TOOL,
)


# ---------------------------------------------------------------------------
# _scrubbed_env
# ---------------------------------------------------------------------------


def test_scrubbed_env_only_passthrough_keys():
    """Only vars in _PASSTHROUGH (plus HOME/TMPDIR/PYTHONUNBUFFERED) appear."""
    env_override = {
        "PATH": "/usr/bin",
        "LANG": "en_US.UTF-8",
        "LC_ALL": "C",
        "TZ": "UTC",
        "OPENROUTER_API_KEY": "sk-or-v1-abc123",
        "GH_TOKEN": "should-be-dropped",
        "GITHUB_TOKEN": "should-be-dropped",
        "AWS_ACCESS_KEY_ID": "should-be-dropped",
        "SECRET": "should-be-dropped",
    }
    scratch = Path("/tmp/spike-test")
    with patch.dict(os.environ, env_override, clear=True):
        env = _scrubbed_env(scratch)

    # passthrough keys preserved
    for key in _PASSTHROUGH:
        if key in env_override:
            assert env[key] == env_override[key], f"Missing passthrough key: {key}"

    # injected keys
    assert env["HOME"] == str(scratch)
    assert env["TMPDIR"] == str(scratch)
    assert env["PYTHONUNBUFFERED"] == "1"

    # no leaked keys
    for bad in ("GH_TOKEN", "GITHUB_TOKEN", "AWS_ACCESS_KEY_ID", "SECRET"):
        assert bad not in env, f"Leaked env var: {bad}"


def test_scrubbed_env_missing_optional_passthrough():
    """Passthrough keys that aren't in os.environ are simply skipped."""
    with patch.dict(os.environ, {"PATH": "/bin"}, clear=True):
        env = _scrubbed_env(Path("/tmp/scratch"))

    assert env["PATH"] == "/bin"
    # These shouldn't be present because we cleared the env
    assert "LANG" not in env
    assert "OPENROUTER_API_KEY" not in env


def test_scrubbed_env_injected_keys_always_present():
    """HOME, TMPDIR, and PYTHONUNBUFFERED are always set regardless of real env."""
    scratch = Path("/does/not/exist")
    with patch.dict(os.environ, {}, clear=True):
        env = _scrubbed_env(scratch)

    assert env["HOME"] == str(scratch)
    assert env["TMPDIR"] == str(scratch)
    assert env["PYTHONUNBUFFERED"] == "1"
    # No other keys
    assert set(env.keys()) == {"HOME", "TMPDIR", "PYTHONUNBUFFERED"}


# ---------------------------------------------------------------------------
# _redact
# ---------------------------------------------------------------------------


def test_redact_replaces_api_key_value():
    """Literal occurrences of the API key value are replaced with a placeholder."""
    key_value = "sk-or-v1-deadbeefcafebabe"
    output = f"Using key: {key_value}\nAlso seen: {key_value} again"
    with patch.dict(os.environ, {"OPENROUTER_API_KEY": key_value}):
        result = _redact(output)

    assert key_value not in result
    assert "***OPENROUTER_API_KEY***" in result
    # Check that both occurrences were replaced
    assert result.count("***OPENROUTER_API_KEY***") == 2


def test_redact_no_api_key_in_env_is_noop():
    """If the redacted env var is not set, output is unchanged."""
    output = "some ordinary output\nwith nothing sensitive"
    with patch.dict(os.environ, {}, clear=True):
        result = _redact(output)

    assert result == output


def test_redact_api_key_empty_string():
    """An empty API key value does not produce spurious replacements."""
    output = "plain text"
    with patch.dict(os.environ, {"OPENROUTER_API_KEY": ""}, clear=True):
        result = _redact(output)

    assert result == output
    assert "***OPENROUTER_API_KEY***" not in result


def test_redact_only_known_redaction_targets():
    """Only keys listed in _REDACT_ENV are redacted."""
    # OPENROUTER_API_KEY is the only entry in _REDACT_ENV
    key_value = "sk-or-v1-secret123"
    other_secret = "another-secret-value"
    output = f"key1={key_value}\nkey2={other_secret}"
    with patch.dict(os.environ, {"OPENROUTER_API_KEY": key_value}):
        result = _redact(output)

    assert key_value not in result
    assert other_secret in result  # Not in _REDACT_ENV, so untouched


def test_redact_key_value_is_substring_of_other_text():
    """Redaction handles the case where the key value appears as a substring."""
    key_value = "abc"
    output = f"token: {key_value} rest: xabcy prefixed suffix"
    with patch.dict(os.environ, {"OPENROUTER_API_KEY": key_value}):
        result = _redact(output)

    assert key_value not in result
    assert "x***OPENROUTER_API_KEY***y" in result


# ---------------------------------------------------------------------------
# _truncate
# ---------------------------------------------------------------------------


def test_truncate_short_output_unchanged():
    """Output under _OUTPUT_CAP is returned as-is with truncated=False."""
    output = "short output"
    result, truncated = _truncate(output)

    assert result == output
    assert truncated is False


def test_truncate_exactly_at_cap_unchanged():
    """Output exactly at _OUTPUT_CAP is not truncated."""
    output = "x" * _OUTPUT_CAP
    result, truncated = _truncate(output)

    assert result == output
    assert truncated is False


def test_truncate_over_cap():
    """Output exceeding _OUTPUT_CAP is truncated and flagged."""
    output = "a" * (_OUTPUT_CAP + 500)
    result, truncated = _truncate(output)

    assert len(result) == _OUTPUT_CAP
    assert result == output[:_OUTPUT_CAP]
    assert truncated is True


def test_truncate_empty_string():
    """Empty string passes through without truncation."""
    result, truncated = _truncate("")

    assert result == ""
    assert truncated is False


def test_truncate_by_one_byte():
    """Output one byte over cap is truncated by exactly one byte."""
    output = "x" * (_OUTPUT_CAP + 1)
    result, truncated = _truncate(output)

    assert len(result) == _OUTPUT_CAP
    assert truncated is True


# ---------------------------------------------------------------------------
# _OUTPUT_CAP constant
# ---------------------------------------------------------------------------


def test_output_cap_is_100k():
    """The output cap is exactly 100,000 bytes as documented."""
    assert _OUTPUT_CAP == 100_000


# ---------------------------------------------------------------------------
# _REDACT_ENV constant
# ---------------------------------------------------------------------------


def test_redact_env_contains_openrouter_key_only():
    """Only OPENROUTER_API_KEY is targeted for redaction as documented."""
    assert _REDACT_ENV == ("OPENROUTER_API_KEY",)


# ---------------------------------------------------------------------------
# _scratch_dir
# ---------------------------------------------------------------------------


def test_scratch_dir_derived_from_repo_root():
    """Scratch dir is <parent_of_repo_root>/spike."""
    ctx = MagicMock()
    ctx.deps.backend.root_dir = "/workspace/issue-42/repo"

    with patch("pathlib.Path.mkdir"):
        result = _scratch_dir(ctx)

    assert result == Path("/workspace/issue-42/spike")


def test_scratch_dir_creates_directory():
    """_scratch_dir creates the scratch directory if it doesn't exist."""
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        ctx = MagicMock()
        ctx.deps.backend.root_dir = str(Path(tmp) / "repo")
        (Path(tmp) / "repo").mkdir()

        result = _scratch_dir(ctx)
        expected = Path(tmp) / "spike"

        assert result == expected
        assert expected.is_dir()


def test_scratch_dir_idempotent():
    """Calling _scratch_dir twice does not error."""
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        ctx = MagicMock()
        ctx.deps.backend.root_dir = str(Path(tmp) / "repo")
        (Path(tmp) / "repo").mkdir()

        first = _scratch_dir(ctx)
        second = _scratch_dir(ctx)

        assert first == second
        assert first.is_dir()


def test_scratch_dir_mkdir_called_with_exist_ok_only():
    """mkdir is called with exist_ok=True and without parents=True.

    Regression guard: the parent of repo_root (the per-issue workspace
    directory) must already exist because it contains the cloned repo.
    Using parents=True would silently create missing ancestors, masking
    misconfiguration.
    """
    ctx = MagicMock()
    ctx.deps.backend.root_dir = "/workspace/issue-42/repo"

    with patch("pathlib.Path.mkdir") as mock_mkdir:
        _scratch_dir(ctx)

    mock_mkdir.assert_called_once_with(exist_ok=True)
    # parents must NOT be passed — not even as False
    call_kwargs = mock_mkdir.call_args.kwargs
    assert "parents" not in call_kwargs, (
        "parents= must not be passed to mkdir — the workspace dir "
        "(repo_root.parent) already exists"
    )


def test_scratch_dir_raises_when_parent_absent():
    """_scratch_dir raises when repo_root.parent does not exist.

    Without parents=True, mkdir(exist_ok=True) must fail if the parent
    directory is missing — this is intentional: the per-issue workspace
    directory must exist because it contains the cloned repo itself.
    """
    ctx = MagicMock()
    # repo_root.parent = /nonexistent/path — does not exist
    ctx.deps.backend.root_dir = "/nonexistent/path/repo"

    with pytest.raises(FileNotFoundError):
        _scratch_dir(ctx)


# ---------------------------------------------------------------------------
# SPIKE_RUN_TOOL constant
# ---------------------------------------------------------------------------


def test_spike_run_tool_is_tool_instance():
    """SPIKE_RUN_TOOL is a pydantic_ai Tool wrapping spike_run."""
    from pydantic_ai import Tool

    assert isinstance(SPIKE_RUN_TOOL, Tool)


def test_spike_run_tool_name():
    """The tool name matches the function name."""
    assert SPIKE_RUN_TOOL.name == "spike_run"


# ---------------------------------------------------------------------------
# Registration in TOOL_FACTORIES
# ---------------------------------------------------------------------------


def test_spike_run_registered_in_tool_factories():
    """The tool is registered under the key 'spike_run' in loader.py."""
    from cai.agents.loader import TOOL_FACTORIES

    assert "spike_run" in TOOL_FACTORIES
    assert TOOL_FACTORIES["spike_run"] == "cai.agents.spike_tool:SPIKE_RUN_TOOL"


def test_import_factory_resolves_spike_run():
    """The factory target string imports and returns the SPIKE_RUN_TOOL."""
    from cai.agents.loader import _import_factory, TOOL_FACTORIES

    tool = _import_factory(TOOL_FACTORIES["spike_run"])
    assert tool is SPIKE_RUN_TOOL


# ---------------------------------------------------------------------------
# Module docstring / verbatim-output guarantee
# ---------------------------------------------------------------------------


def test_module_docstring_exists():
    """The spike_tool module has a docstring describing the tool's purpose."""
    import cai.agents.spike_tool as st

    assert st.__doc__ is not None
    assert len(st.__doc__) > 0


def test_spike_run_docstring_declares_verbatim_output():
    """The spike_run docstring guarantees verbatim output to the caller."""
    import cai.agents.spike_tool as st

    doc = st.spike_run.__doc__
    assert doc is not None
    assert "verbatim" in doc.lower() or "unchanged" in doc or "exactly" in doc


def test_spike_run_docstring_mentions_no_interception():
    """The spike_run docstring states no interception/wrapping/modification."""
    import cai.agents.spike_tool as st

    doc = st.spike_run.__doc__
    assert doc is not None
    # The docstring says "No part of the toolchain intercepts, wraps, or
    # modifies" — the phrase may wrap across lines, so check key words.
    assert "intercepts" in doc.lower()
    assert "wraps" in doc.lower()
    assert "modifies" in doc.lower()
    assert "no part of the" in doc.lower()


def test_spike_run_docstring_mentions_100k_cap():
    """The spike_run docstring mentions the 100 KB output cap."""
    import cai.agents.spike_tool as st

    doc = st.spike_run.__doc__
    assert doc is not None
    # The doc should reference the size cap
    assert "100" in doc and ("KB" in doc or "kb" in doc or "cap" in doc.lower())


# ---------------------------------------------------------------------------
# spike.md prompt — verbatim-output guarantee
# ---------------------------------------------------------------------------


def test_spike_md_prompt_exists_and_readable():
    """The spike.md file exists and contains text."""
    import cai.agents

    spike_md = Path(cai.agents.__file__).parent / "spike.md"
    assert spike_md.exists()
    content = spike_md.read_text()
    assert len(content) > 0


def test_spike_md_verbatim_guarantee():
    """spike.md tells the model that output is returned verbatim/unchanged."""
    import cai.agents

    spike_md = Path(cai.agents.__file__).parent / "spike.md"
    content = spike_md.read_text()

    assert "verbatim" in content


def test_spike_md_no_interception():
    """spike.md states the tool does not wrap, intercept, or alter output."""
    import cai.agents

    spike_md = Path(cai.agents.__file__).parent / "spike.md"
    content = spike_md.read_text()

    # Should mention no wrapping/interception/alteration
    assert "does not wrap" in content or "does not intercept" in content or \
           "not intercept" in content


def test_spike_md_explicitly_forbids_workarounds():
    """spike.md tells the model not to write workarounds for imagined interception."""
    import cai.agents

    spike_md = Path(cai.agents.__file__).parent / "spike.md"
    content = spike_md.read_text()

    # The key instruction: don't work around imagined interception
    assert "workaround" in content.lower() or "imagined" in content.lower()


def test_spike_md_mentions_100k_cap():
    """spike.md mentions the 100 KB output size cap."""
    import cai.agents

    spike_md = Path(cai.agents.__file__).parent / "spike.md"
    content = spike_md.read_text()

    assert "100" in content and ("KB" in content or "kb" in content or "cap" in content.lower())


def test_spike_md_mentions_api_key_redaction():
    """spike.md acknowledges API key redaction in output."""
    import cai.agents

    spike_md = Path(cai.agents.__file__).parent / "spike.md"
    content = spike_md.read_text()

    assert "redaction" in content.lower() or "api key" in content.lower() or \
           "key" in content.lower()


# ---------------------------------------------------------------------------
# spike.md prompt — tool boundaries and common pitfalls
# ---------------------------------------------------------------------------


def test_spike_md_has_tool_boundaries_heading():
    """spike.md contains '## Tool boundaries' section."""
    import cai.agents

    spike_md = Path(cai.agents.__file__).parent / "spike.md"
    content = spike_md.read_text()

    assert "## Tool boundaries" in content


def test_spike_md_tool_boundaries_scoped_to_repo():
    """spike.md states that read_file/grep/glob/ls search only the cloned repo."""
    import cai.agents

    spike_md = Path(cai.agents.__file__).parent / "spike.md"
    content = spike_md.read_text()

    assert "cloned repo" in content
    assert "site-packages" in content or "installed packages" in content
    assert "cannot find installed" in content.lower() or "cannot see" in content.lower()


def test_spike_md_tool_boundaries_recommends_spike_run():
    """spike.md recommends using spike_run to discover installed-package paths."""
    import cai.agents

    spike_md = Path(cai.agents.__file__).parent / "spike.md"
    content = spike_md.read_text()

    # The section should mention using spike_run to locate installed package code
    assert "use `spike_run`" in content.lower() or "spike_run" in content


def test_spike_md_tool_boundaries_example_import():
    """spike.md gives an example of discovering an installed package path via spike_run."""
    import cai.agents

    spike_md = Path(cai.agents.__file__).parent / "spike.md"
    content = spike_md.read_text()

    assert "import " in content and "__file__" in content
    assert "print(" in content


def test_spike_md_tool_boundaries_discourages_grep_for_framework():
    """spike.md warns against grepping the repo for framework code strings."""
    import cai.agents

    spike_md = Path(cai.agents.__file__).parent / "spike.md"
    content = spike_md.read_text()

    assert "never grep" in content.lower() or "go straight to" in content.lower()


def test_spike_md_has_common_pitfalls_subsection():
    """spike.md contains '### Common pitfalls' subsection."""
    import cai.agents

    spike_md = Path(cai.agents.__file__).parent / "spike.md"
    content = spike_md.read_text()

    assert "### Common pitfalls" in content


def test_spike_md_pitfall_no_retry_grep():
    """spike.md warns against retrying grep with minor pattern variations."""
    import cai.agents

    spike_md = Path(cai.agents.__file__).parent / "spike.md"
    content = spike_md.read_text()

    assert "don't retry" in content.lower() or "do not retry" in content.lower()


def test_spike_md_pitfall_guardrail_messages():
    """spike.md warns that guardrail error messages containing a search term are not matches."""
    import cai.agents

    spike_md = Path(cai.agents.__file__).parent / "spike.md"
    content = spike_md.read_text()

    assert "guardrail" in content.lower()
    assert "not a match" in content.lower()
