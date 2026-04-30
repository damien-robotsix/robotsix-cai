import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic_ai.exceptions import ModelRetry

from cai.agents.loader import (
    EditFileGuardrailAsRetry,
    GrepGuardrailAsRetry,
    _get_arg,
    parse_agent_md,
    resolve_agent_path,
)

def test_resolve_agent_path_finds_file(monkeypatch, tmp_path):
    monkeypatch.setattr("cai.agents.loader.AGENT_DIR", tmp_path)
    
    # Create test agent file
    agent_file = tmp_path / "my_agent.md"
    agent_file.write_text("dummy")
    
    assert resolve_agent_path("my_agent") == agent_file

def test_resolve_agent_path_nested(monkeypatch, tmp_path):
    monkeypatch.setattr("cai.agents.loader.AGENT_DIR", tmp_path)
    
    # Create test agent file in a nested dir
    nested_dir = tmp_path / "subfolder" / "deep"
    nested_dir.mkdir(parents=True)
    agent_file = nested_dir / "my_agent.md"
    agent_file.write_text("dummy")
    
    assert resolve_agent_path("my_agent") == agent_file

def test_resolve_agent_path_not_found(monkeypatch, tmp_path):
    monkeypatch.setattr("cai.agents.loader.AGENT_DIR", tmp_path)
    
    with pytest.raises(FileNotFoundError, match="agent definition not found: missing_agent"):
        resolve_agent_path("missing_agent")

def test_resolve_agent_path_ambiguous(monkeypatch, tmp_path):
    monkeypatch.setattr("cai.agents.loader.AGENT_DIR", tmp_path)
    
    agent_file1 = tmp_path / "my_agent.md"
    agent_file1.write_text("dummy")
    
    nested_dir = tmp_path / "subfolder"
    nested_dir.mkdir(parents=True)
    agent_file2 = nested_dir / "my_agent.md"
    agent_file2.write_text("dummy")
    
    with pytest.raises(ValueError, match="ambiguous agent name: my_agent"):
        resolve_agent_path("my_agent")

def test_resolve_agent_path_exported():
    import cai.agents.loader as loader
    assert "resolve_agent_path" in loader.__all__


def _grep_call(name="grep"):
    return SimpleNamespace(tool_name=name)


def _edit_call(name="edit_file"):
    return SimpleNamespace(tool_name=name)


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# EditFileGuardrailAsRetry
# ---------------------------------------------------------------------------


def test_edit_file_guardrail_passes_through_non_model_retry():
    """Non-ModelRetry errors are not consumed by this guardrail."""
    cap = EditFileGuardrailAsRetry()
    # Returns None -> error passes to next capability handler
    result = _run(cap.on_tool_execute_error(
        None,
        call=_edit_call(),
        tool_def=None,
        args={},
        error=ValueError("something went wrong"),
    ))
    assert result is None


def test_edit_file_guardrail_re_raises_non_edit_file():
    """ModelRetry from a non-edit_file tool is re-raised unchanged."""
    cap = EditFileGuardrailAsRetry()
    original = ModelRetry("tool crashed")
    with pytest.raises(ModelRetry) as exc:
        _run(cap.on_tool_execute_error(
            None,
            call=_edit_call("read_file"),
            tool_def=None,
            args={},
            error=original,
        ))
    assert exc.value is original
    assert str(exc.value) == "tool crashed"


def test_edit_file_guardrail_re_raises_without_same_result():
    """ModelRetry from edit_file without 'same result' passes through unchanged."""
    cap = EditFileGuardrailAsRetry()
    original = ModelRetry("old_string not found")
    with pytest.raises(ModelRetry) as exc:
        _run(cap.on_tool_execute_error(
            None,
            call=_edit_call(),
            tool_def=None,
            args={},
            error=original,
        ))
    assert exc.value is original
    assert str(exc.value) == "old_string not found"


def test_edit_file_guardrail_enriches_same_result_message():
    """ModelRetry with 'same result' gets a disambiguation hint appended."""
    cap = EditFileGuardrailAsRetry()
    original = ModelRetry(
        "edit_file returned the same result 3 times in a row."
    )
    with pytest.raises(ModelRetry) as exc:
        _run(cap.on_tool_execute_error(
            None,
            call=_edit_call(),
            tool_def=None,
            args={},
            error=original,
        ))
    msg = str(exc.value)
    assert "edit_file returned the same result 3 times in a row." in msg
    assert "old_string may match multiple locations" in msg
    assert "unique line above or below" in msg


def test_edit_file_guardrail_enriches_same_result_partial():
    """The 'same result' substring match works on any variant phrasing."""
    cap = EditFileGuardrailAsRetry()
    original = ModelRetry(
        "The tool edit_file produced the same result after several attempts."
    )
    with pytest.raises(ModelRetry) as exc:
        _run(cap.on_tool_execute_error(
            None,
            call=_edit_call(),
            tool_def=None,
            args={},
            error=original,
        ))
    msg = str(exc.value)
    assert "same result" in msg
    assert "old_string may match multiple locations" in msg


def test_edit_file_guardrail_wired_into_build_deep_agent_capabilities(monkeypatch):
    """EditFileGuardrailAsRetry is registered before ToolErrorAsRetry."""
    import cai.agents.loader as loader

    captured: dict = {}

    def fake_create_deep_agent(model, **kwargs):
        captured["capabilities"] = kwargs.get("capabilities")
        return object()

    monkeypatch.setattr(
        "pydantic_deep.create_deep_agent", fake_create_deep_agent
    )
    monkeypatch.setattr(loader, "build_model", lambda config: object())
    monkeypatch.setattr(loader, "_prune_toolsets", lambda agent, requested: None)

    config = {"name": "test-agent", "model": "anthropic/claude-sonnet-4-6"}
    loader.build_deep_agent(config, "instructions")

    cap_types = [type(c).__name__ for c in captured["capabilities"]]
    assert "EditFileGuardrailAsRetry" in cap_types
    # Must appear before ToolErrorAsRetry so it sees ModelRetry first
    edit_idx = cap_types.index("EditFileGuardrailAsRetry")
    tool_err_idx = cap_types.index("ToolErrorAsRetry")
    assert edit_idx < tool_err_idx, (
        "EditFileGuardrailAsRetry must be before ToolErrorAsRetry"
    )


def test_grep_guardrail_passes_through_non_grep_tool():
    cap = GrepGuardrailAsRetry()
    result = _run(cap.after_tool_execute(
        None, call=_grep_call("read_file"), tool_def=None, args={}, result="x",
    ))
    assert result == "x"
    assert cap._empty_grep_count == 0


def test_grep_guardrail_increments_on_empty_result():
    cap = GrepGuardrailAsRetry()
    _run(cap.after_tool_execute(
        None, call=_grep_call(), tool_def=None, args={},
        result="No matches for 'foo'",
    ))
    assert cap._empty_grep_count == 1


def test_grep_guardrail_resets_on_match():
    cap = GrepGuardrailAsRetry()
    cap._empty_grep_count = 2
    _run(cap.after_tool_execute(
        None, call=_grep_call(), tool_def=None, args={},
        result="Files containing 'foo':\n  a.py",
    ))
    assert cap._empty_grep_count == 0


def test_grep_guardrail_raises_at_threshold():
    cap = GrepGuardrailAsRetry()
    for _ in range(GrepGuardrailAsRetry._THRESHOLD - 1):
        _run(cap.after_tool_execute(
            None, call=_grep_call(), tool_def=None, args={},
            result="No matches for 'foo'",
        ))
    with pytest.raises(ModelRetry, match="Multiple zero-result grep"):
        _run(cap.after_tool_execute(
            None, call=_grep_call(), tool_def=None, args={},
            result="No matches for 'bar'",
        ))
    # counter resets after triggering so the next streak starts fresh
    assert cap._empty_grep_count == 0


def test_grep_guardrail_for_run_returns_fresh_instance():
    cap = GrepGuardrailAsRetry()
    cap._empty_grep_count = 5
    cap._recently_removed.add("old_stuff")
    fresh = _run(cap.for_run(None))
    assert fresh is not cap
    assert fresh._empty_grep_count == 0
    assert fresh._recently_removed == set()


def test_grep_guardrail_edit_file_tracks_old_string():
    cap = GrepGuardrailAsRetry()
    _run(cap.after_tool_execute(
        None,
        call=SimpleNamespace(tool_name="edit_file"),
        tool_def=None,
        args={"old_string": "pytest.raises(Exception)"},
        result="ok",
    ))
    assert "pytest.raises(Exception)" in cap._recently_removed
    assert cap._empty_grep_count == 0


def test_grep_guardrail_verification_grep_not_counted():
    """An empty grep whose pattern contains a recently-removed old_string
    is a verification — it must NOT increment the counter or reset it."""
    cap = GrepGuardrailAsRetry()
    # First, simulate an edit_file that removed something.
    _run(cap.after_tool_execute(
        None,
        call=SimpleNamespace(tool_name="edit_file"),
        tool_def=None,
        args={"old_string": "pytest.raises(Exception)"},
        result="ok",
    ))
    # Pre-set counter to 1 to verify it's neither incremented nor reset.
    cap._empty_grep_count = 1
    _run(cap.after_tool_execute(
        None,
        call=_grep_call(),
        tool_def=None,
        args={"pattern": r"pytest\.raises\(Exception\)"},
        result="No matches for 'pytest.raises(Exception)'",
    ))
    # Counter stays at 1 — verification grep is invisible.
    assert cap._empty_grep_count == 1


def test_grep_guardrail_non_verification_grep_still_increments():
    """A grep that does NOT match any recently-removed old_string must
    still increment the counter normally."""
    cap = GrepGuardrailAsRetry()
    # Record an edit.
    _run(cap.after_tool_execute(
        None,
        call=SimpleNamespace(tool_name="edit_file"),
        tool_def=None,
        args={"old_string": "pytest.raises(Exception)"},
        result="ok",
    ))
    # Now grep for something unrelated.
    _run(cap.after_tool_execute(
        None,
        call=_grep_call(),
        tool_def=None,
        args={"pattern": "some_unrelated_thing"},
        result="No matches for 'some_unrelated_thing'",
    ))
    assert cap._empty_grep_count == 1


# ---------------------------------------------------------------------------
# GrepGuardrailAsRetry — re.escape exemption path
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "old_string, grep_pattern, description",
    [
        # Dot — re.escape produces \.
        ("foo.bar", r"foo\.bar", "dot metacharacter"),
        # Parentheses — re.escape produces \( and \)
        ("func(arg)", r"func\(arg\)", "parentheses"),
        # Asterisk — re.escape produces \*
        ("import *", r"import \*", "asterisk"),
        # Plus — re.escape produces \+
        ("a+b", r"a\+b", "plus"),
        # Question mark — re.escape produces \?
        ("maybe?", r"maybe\?", "question mark"),
        # Square brackets — re.escape produces \[ and \]
        ("arr[0]", r"arr\[0\]", "square brackets"),
        # Curly braces — re.escape produces \{ and \}
        ("x{1,3}", r"x\{1,3\}", "curly braces"),
        # Caret — re.escape produces \^
        ("^start", r"\^start", "caret"),
        # Dollar — re.escape produces \$
        ("end$", r"end\$", "dollar"),
        # Pipe — re.escape produces \|
        ("a|b", r"a\|b", "pipe"),
        # Backslash — re.escape produces \\
        (r"c:\path", r"c:\\path", "backslash"),
        # Multiple metacharacters combined
        ("pytest.raises(Exception)", r"pytest\.raises\(Exception\)", "multiple metacharacters"),
    ],
)
def test_grep_guardrail_verification_exempts_regex_escaped_pattern(
    old_string, grep_pattern, description
):
    """The re.escape path exempts zero-result greps whose pattern is the
    regex-escaped form of a recently-removed old_string."""
    cap = GrepGuardrailAsRetry()
    _run(cap.after_tool_execute(
        None,
        call=SimpleNamespace(tool_name="edit_file"),
        tool_def=None,
        args={"old_string": old_string},
        result="ok",
    ))
    cap._empty_grep_count = 2
    _run(cap.after_tool_execute(
        None,
        call=_grep_call(),
        tool_def=None,
        args={"pattern": grep_pattern},
        result=f"No matches for '{grep_pattern}'",
    ))
    assert cap._empty_grep_count == 2, (
        f"Verification grep should be exempt for {description}"
    )


def test_grep_guardrail_raw_substring_path_still_works():
    """The original raw-substring check (removed in pattern) must still
    exempt greps where the pattern literally contains the old_string."""
    cap = GrepGuardrailAsRetry()
    _run(cap.after_tool_execute(
        None,
        call=SimpleNamespace(tool_name="edit_file"),
        tool_def=None,
        args={"old_string": "needle"},
        result="ok",
    ))
    cap._empty_grep_count = 2
    _run(cap.after_tool_execute(
        None,
        call=_grep_call(),
        tool_def=None,
        args={"pattern": "searching for needle here"},
        result="No matches for 'searching for needle here'",
    ))
    assert cap._empty_grep_count == 2


def test_grep_guardrail_multiple_removed_one_matches_via_escape():
    """When multiple old_strings are tracked, an exemption is granted if
    ANY one of them matches via either the raw-substring or re.escape path."""
    cap = GrepGuardrailAsRetry()
    _run(cap.after_tool_execute(
        None,
        call=SimpleNamespace(tool_name="edit_file"),
        tool_def=None,
        args={"old_string": "unrelated stuff"},
        result="ok",
    ))
    _run(cap.after_tool_execute(
        None,
        call=SimpleNamespace(tool_name="edit_file"),
        tool_def=None,
        args={"old_string": "pytest.raises(Exception)"},
        result="ok",
    ))
    cap._empty_grep_count = 2
    _run(cap.after_tool_execute(
        None,
        call=_grep_call(),
        tool_def=None,
        args={"pattern": r"pytest\.raises\(Exception\)"},
        result="No matches for 'pytest.raises(Exception)'",
    ))
    assert cap._empty_grep_count == 2


def test_grep_guardrail_verification_exempts_via_re_search_fallback():
    """When neither raw-substring nor re.escape checks match, the
    re.search fallback exempts a verification grep whose regex matches
    a recently-removed string directly.

    This handles version-dependent re.escape differences (e.g. whether
    spaces are escaped).  ``re.search(r"import \*", "import *")``
    succeeds even when ``re.escape("import *")`` does not appear in the
    pattern."""
    cap = GrepGuardrailAsRetry()
    _run(cap.after_tool_execute(
        None,
        call=SimpleNamespace(tool_name="edit_file"),
        tool_def=None,
        args={"old_string": "import *"},
        result="ok",
    ))
    cap._empty_grep_count = 2
    # Pattern only escapes the asterisk, not the space — so the raw
    # substring check fails ("import *" not in "import \*") and the
    # re.escape check may or may not pass depending on Python version.
    _run(cap.after_tool_execute(
        None,
        call=_grep_call(),
        tool_def=None,
        args={"pattern": r"import \*"},
        result="No matches for 'import \\*'",
    ))
    assert cap._empty_grep_count == 2, (
        "re.search fallback should exempt verification grep"
    )


def test_grep_guardrail_re_search_fallback_handles_invalid_regex():
    """When the grep pattern is an invalid regex, re.search raises
    re.error which is caught silently — the exemption is not granted
    and the grep is counted normally."""
    cap = GrepGuardrailAsRetry()
    _run(cap.after_tool_execute(
        None,
        call=SimpleNamespace(tool_name="edit_file"),
        tool_def=None,
        args={"old_string": "some.text"},
        result="ok",
    ))
    cap._empty_grep_count = 1
    _run(cap.after_tool_execute(
        None,
        call=_grep_call(),
        tool_def=None,
        args={"pattern": r"invalid[regex(unclosed"},
        result="No matches for 'invalid[regex(unclosed'",
    ))
    # Exemption not granted — counter increments normally.
    assert cap._empty_grep_count == 2


def test_grep_guardrail_verification_exempt_does_not_reset_counter():
    """A verification grep must leave an existing non-zero counter
    untouched — it neither increments nor resets it."""
    cap = GrepGuardrailAsRetry()
    _run(cap.after_tool_execute(
        None,
        call=SimpleNamespace(tool_name="edit_file"),
        tool_def=None,
        args={"old_string": "pytest.raises(Exception)"},
        result="ok",
    ))
    # Build up a real streak first.
    for _ in range(2):
        _run(cap.after_tool_execute(
            None,
            call=_grep_call(),
            tool_def=None,
            args={"pattern": "unrelated"},
            result="No matches for 'unrelated'",
        ))
    assert cap._empty_grep_count == 2
    # Verification grep — counter stays at 2, streak continues.
    _run(cap.after_tool_execute(
        None,
        call=_grep_call(),
        tool_def=None,
        args={"pattern": r"pytest\.raises\(Exception\)"},
        result="No matches for 'pytest.raises(Exception)'",
    ))
    assert cap._empty_grep_count == 2
    # Next non-exempt empty grep hits threshold and raises.
    with pytest.raises(ModelRetry, match="Multiple zero-result grep"):
        _run(cap.after_tool_execute(
            None,
            call=_grep_call(),
            tool_def=None,
            args={"pattern": "unrelated2"},
            result="No matches for 'unrelated2'",
        ))
    assert cap._empty_grep_count == 0


def test_grep_guardrail_wired_into_build_deep_agent_capabilities(monkeypatch):
    import cai.agents.loader as loader

    captured: dict = {}

    def fake_create_deep_agent(model, **kwargs):
        captured["capabilities"] = kwargs.get("capabilities")
        return object()

    monkeypatch.setattr(
        "pydantic_deep.create_deep_agent", fake_create_deep_agent
    )
    monkeypatch.setattr(loader, "build_model", lambda config: object())
    monkeypatch.setattr(loader, "_prune_toolsets", lambda agent, requested: None)

    config = {"name": "test-agent", "model": "anthropic/claude-sonnet-4-6"}
    loader.build_deep_agent(config, "instructions")

    cap_types = [type(c).__name__ for c in captured["capabilities"]]
    assert "GrepGuardrailAsRetry" in cap_types


# ---------------------------------------------------------------------------
# _get_arg
# ---------------------------------------------------------------------------


def test_get_arg_from_dict():
    assert _get_arg({"pattern": "foo"}, "pattern") == "foo"
    assert _get_arg({"old_string": "bar"}, "old_string") == "bar"


def test_get_arg_from_object():
    obj = SimpleNamespace(pattern="foo", old_string="bar")
    assert _get_arg(obj, "pattern") == "foo"
    assert _get_arg(obj, "old_string") == "bar"


def test_get_arg_missing_key_from_dict():
    assert _get_arg({"other": 1}, "pattern") is None


def test_get_arg_missing_attr_from_object():
    obj = SimpleNamespace(other=1)
    assert _get_arg(obj, "pattern") is None


def test_get_arg_from_none():
    assert _get_arg(None, "pattern") is None


# ---------------------------------------------------------------------------
# GrepGuardrailAsRetry — additional edge cases
# ---------------------------------------------------------------------------


def test_grep_guardrail_edit_file_ignores_empty_old_string():
    """Empty old_string values should not be added to _recently_removed."""
    cap = GrepGuardrailAsRetry()
    _run(cap.after_tool_execute(
        None,
        call=SimpleNamespace(tool_name="edit_file"),
        tool_def=None,
        args={"old_string": ""},
        result="ok",
    ))
    assert cap._recently_removed == set()


def test_grep_guardrail_edit_file_object_args():
    """edit_file with object-style args (not dict) should still track old_string."""
    cap = GrepGuardrailAsRetry()
    _run(cap.after_tool_execute(
        None,
        call=SimpleNamespace(tool_name="edit_file"),
        tool_def=None,
        args=SimpleNamespace(old_string="remove_me"),
        result="ok",
    ))
    assert "remove_me" in cap._recently_removed


def test_grep_guardrail_grep_object_args():
    """grep with object-style args should extract pattern for verification check."""
    cap = GrepGuardrailAsRetry()
    # Simulate an edit first.
    _run(cap.after_tool_execute(
        None,
        call=SimpleNamespace(tool_name="edit_file"),
        tool_def=None,
        args={"old_string": "needle"},
        result="ok",
    ))
    cap._empty_grep_count = 1
    _run(cap.after_tool_execute(
        None,
        call=_grep_call(),
        tool_def=None,
        args=SimpleNamespace(pattern="looking for needle here"),
        result="No matches for 'looking for needle here'",
    ))
    # Verification grep — counter unchanged.
    assert cap._empty_grep_count == 1


def test_grep_guardrail_empty_result_string_counts_as_empty():
    """A completely empty result string is treated as empty and increments."""
    cap = GrepGuardrailAsRetry()
    _run(cap.after_tool_execute(
        None,
        call=_grep_call(),
        tool_def=None,
        args={},
        result="",
    ))
    assert cap._empty_grep_count == 1


def test_grep_guardrail_whitespace_only_result_counts_as_empty():
    """A whitespace-only result string is treated as empty and increments."""
    cap = GrepGuardrailAsRetry()
    _run(cap.after_tool_execute(
        None,
        call=_grep_call(),
        tool_def=None,
        args={},
        result="   \n\t  ",
    ))
    assert cap._empty_grep_count == 1


def test_grep_guardrail_no_exemption_when_recently_removed_empty():
    """When _recently_removed is empty, no exemption logic runs at all."""
    cap = GrepGuardrailAsRetry()
    assert cap._recently_removed == set()
    _run(cap.after_tool_execute(
        None,
        call=_grep_call(),
        tool_def=None,
        args={"pattern": "something"},
        result="No matches for 'something'",
    ))
    assert cap._empty_grep_count == 1


# ---------------------------------------------------------------------------
# parse_agent_md
# ---------------------------------------------------------------------------


def test_parse_agent_md_valid(monkeypatch, tmp_path):
    monkeypatch.setattr("cai.agents.loader.AGENT_DIR", tmp_path)
    md_path = tmp_path / "test_agent.md"
    md_path.write_text(
        "---\n"
        "name: test-agent\n"
        "model: anthropic/claude-sonnet-4-6\n"
        "---\n"
        "## System prompt body\n\n"
        "This is the system prompt.\n"
        "It spans multiple lines.\n"
    )
    config, system_prompt = parse_agent_md(str(md_path))
    assert config["name"] == "test-agent"
    assert config["model"] == "anthropic/claude-sonnet-4-6"
    assert "## System prompt body" in system_prompt
    assert "This is the system prompt." in system_prompt


def test_parse_agent_md_missing_frontmatter(monkeypatch, tmp_path):
    monkeypatch.setattr("cai.agents.loader.AGENT_DIR", tmp_path)
    md_path = tmp_path / "no_frontmatter.md"
    md_path.write_text("No frontmatter here.\nJust some text.\n")
    with pytest.raises(ValueError, match="missing YAML frontmatter"):
        parse_agent_md(str(md_path))


def test_parse_agent_md_malformed_frontmatter(monkeypatch, tmp_path):
    monkeypatch.setattr("cai.agents.loader.AGENT_DIR", tmp_path)
    md_path = tmp_path / "malformed.md"
    md_path.write_text("---\nname: test-agent\n# no closing ---\n")
    with pytest.raises(ValueError, match="malformed frontmatter"):
        parse_agent_md(str(md_path))


def test_parse_agent_md_missing_name_field(monkeypatch, tmp_path):
    monkeypatch.setattr("cai.agents.loader.AGENT_DIR", tmp_path)
    md_path = tmp_path / "no_name.md"
    md_path.write_text(
        "---\n"
        "model: anthropic/claude-sonnet-4-6\n"
        "---\n"
        "System prompt without a name field.\n"
    )
    with pytest.raises(ValueError, match="missing required 'name' field"):
        parse_agent_md(str(md_path))


def test_parse_agent_md_empty_frontmatter(monkeypatch, tmp_path):
    monkeypatch.setattr("cai.agents.loader.AGENT_DIR", tmp_path)
    md_path = tmp_path / "empty_frontmatter.md"
    md_path.write_text("---\n---\nSystem prompt with empty frontmatter.\n")
    with pytest.raises(ValueError, match="missing required 'name' field"):
        parse_agent_md(str(md_path))


def test_parse_agent_md_dash_dash_dash_in_comment_not_closing_delimiter(monkeypatch, tmp_path):
    """--- inside a YAML comment must not be treated as the closing delimiter."""
    monkeypatch.setattr("cai.agents.loader.AGENT_DIR", tmp_path)
    md_path = tmp_path / "comment_dashes.md"
    md_path.write_text(
        "---\n"
        'name: test-agent\n'
        'model: anthropic/claude-sonnet-4-6\n'
        "# a comment with --- inside it\n"
        "---\n"
        "## Body after closing delimiter.\n"
    )
    config, system_prompt = parse_agent_md(str(md_path))
    assert config["name"] == "test-agent"
    assert "## Body after closing delimiter." in system_prompt


def test_parse_agent_md_dash_dash_dash_in_body_not_confused(monkeypatch, tmp_path):
    """--- in body text (not a standalone line) must remain part of the body."""
    monkeypatch.setattr("cai.agents.loader.AGENT_DIR", tmp_path)
    md_path = tmp_path / "body_dashes.md"
    md_path.write_text(
        "---\n"
        'name: test-agent\n'
        'model: anthropic/claude-sonnet-4-6\n'
        "---\n"
        "Here is a --- separator in the body text.\n"
        "It should not break parsing.\n"
    )
    config, system_prompt = parse_agent_md(str(md_path))
    assert config["name"] == "test-agent"
    assert "--- separator in the body text" in system_prompt
    assert system_prompt.startswith("Here is a --- separator")


# ---------------------------------------------------------------------------
# Pagination guidance in agent system prompts
# ---------------------------------------------------------------------------

PAGINATION_TEXT = "Paginate large files"


@pytest.mark.parametrize(
    "agent_name",
    [
        "explore",
        "implement",
        "refine",
    ],
)
def test_agent_prompt_includes_pagination_guidance(agent_name):
    """Ensure each agent's system prompt contains read_file pagination guidance."""
    path = resolve_agent_path(agent_name)
    _, system_prompt = parse_agent_md(path)
    assert PAGINATION_TEXT in system_prompt, (
        f"Agent '{agent_name}' system prompt missing pagination guidance.\n"
        f"Expected text: '{PAGINATION_TEXT}'"
    )


# ---------------------------------------------------------------------------
# Anti-hallucination guard in agent system prompts
# ---------------------------------------------------------------------------

ANTI_HALLUCINATION_TEXT = (
    "> **You do NOT have an `execute`, `bash`, `shell`, or `run` tool. "
    "You cannot run commands, tests, or scripts. "
    "Only the tools listed above are available to you.**"
)


AGENTS_WITH_ANTI_HALLUCINATION = [
    "docs",
    "implement",
    "python_review",
    "refine",
    "test_writer",
]


@pytest.mark.parametrize("agent_name", AGENTS_WITH_ANTI_HALLUCINATION)
def test_agent_prompt_includes_anti_hallucination_guard(agent_name):
    """Each of the five agents that lack an execute tool must carry the
    defensive anti-hallucination blockquote in their system prompt."""
    path = resolve_agent_path(agent_name)
    _, system_prompt = parse_agent_md(path)
    assert ANTI_HALLUCINATION_TEXT in system_prompt, (
        f"Agent '{agent_name}' system prompt missing anti-hallucination guard.\n"
        f"Expected text:\n{ANTI_HALLUCINATION_TEXT}"
    )


@pytest.mark.parametrize("agent_name", AGENTS_WITH_ANTI_HALLUCINATION)
def test_anti_hallucination_guard_positioned_after_agent_header(agent_name):
    """The anti-hallucination blockquote must appear after the agent title
    heading (# Agent Name) so it's the first instruction the model sees."""
    path = resolve_agent_path(agent_name)
    _, system_prompt = parse_agent_md(path)

    # The guard must be present ...
    guard_idx = system_prompt.index(ANTI_HALLUCINATION_TEXT)
    # ... and must appear after the `# ` heading that starts the body.
    heading_end = system_prompt.index("\n")
    assert guard_idx > heading_end, (
        f"Agent '{agent_name}': anti-hallucination guard must appear "
        f"after the title heading, but was found before it."
    )


AGENTS_WITHOUT_EXECUTE = AGENTS_WITH_ANTI_HALLUCINATION


@pytest.mark.parametrize("agent_name", AGENTS_WITHOUT_EXECUTE)
def test_agents_without_execute_tool_dont_declare_it(agent_name):
    """Agents carrying the anti-hallucination guard must not list execute,
    bash, shell, or run in their frontmatter tools."""
    path = resolve_agent_path(agent_name)
    config, _ = parse_agent_md(path)
    tools = config.get("tools", [])
    forbidden = {"execute", "bash", "shell", "run"}
    intersection = set(tools) & forbidden
    assert not intersection, (
        f"Agent '{agent_name}' declares {sorted(intersection)} in tools "
        f"but also carries the anti-hallucination guard — remove the guard "
        f"or add the tool."
    )


def test_anti_hallucination_guard_absent_from_explore():
    """Explore agent (which has no execute tool either) should NOT
    contain the guard unless explicitly added."""
    path = resolve_agent_path("explore")
    _, system_prompt = parse_agent_md(path)
    assert ANTI_HALLUCINATION_TEXT not in system_prompt, (
        "Anti-hallucination guard found unexpectedly in explore agent prompt."
    )
