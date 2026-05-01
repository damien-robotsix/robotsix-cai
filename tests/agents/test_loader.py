import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic_ai.exceptions import ModelRetry
from pydantic_ai.messages import ModelRequest, ModelResponse, ToolCallPart, ToolReturnPart
from pydantic_ai.models import ModelRequestContext

from cai.agents.loader import (
    EditFileGuardrailAsRetry,
    GlobPatternSanitizer,
    ToolErrorAsRetry,
    GrepGuardrailAsRetry,
    _get_arg,
    HistoryCompactorCapability,
    MicroReadGuardCapability,
    UnknownToolRetry,
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


def test_edit_file_guardrail_passes_through_non_edit_file_model_retry():
    """ModelRetry from a non-edit_file tool passes through to downstream
    capabilities (e.g. UnknownToolRetry) instead of being re-raised."""
    cap = EditFileGuardrailAsRetry()
    original = ModelRetry("tool crashed")
    result = _run(cap.on_tool_execute_error(
        None,
        call=_edit_call("read_file"),
        tool_def=None,
        args={},
        error=original,
    ))
    assert result is None


def test_edit_file_guardrail_passes_through_without_same_result():
    """ModelRetry from edit_file without 'same result' passes through
    to downstream capabilities unchanged."""
    cap = EditFileGuardrailAsRetry()
    original = ModelRetry("old_string not found")
    result = _run(cap.on_tool_execute_error(
        None,
        call=_edit_call(),
        tool_def=None,
        args={},
        error=original,
    ))
    assert result is None


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
    assert "already been applied" in msg
    assert "the edit may have already been applied" in msg
    assert "the text is already present" in msg
    assert "unique line above or below" in msg
    assert "disambiguate" in msg


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
    assert "already been applied" in msg
    assert "the edit may have already been applied" in msg
    assert "the text is already present" in msg
    assert "disambiguate" in msg


def test_edit_file_guardrail_docstring_mentions_already_applied():
    """The EditFileGuardrailAsRetry docstring must mention both possible
    causes of the 'same result' error — disambiguation failure and
    already-applied edit."""
    doc = EditFileGuardrailAsRetry.__doc__
    assert doc is not None
    assert "already successfully applied" in doc or "already present" in doc
    assert "already been applied" in doc
    assert "disambiguate" in doc, "Docstring must mention disambiguation as one possible cause"


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


# ---------------------------------------------------------------------------
# EditFileGuardrailAsRetry — before_tool_execute old_string pre-verification
# ---------------------------------------------------------------------------


def _tmp_file(tmp_path, name, content):
    """Write *content* to *name* under tmp_path and return its string path."""
    f = tmp_path / name
    f.write_text(content)
    return str(f)


def test_edit_file_guardrail_before_execute_old_string_found(tmp_path):
    """old_string found in file → args returned unchanged, no ModelRetry."""
    cap = EditFileGuardrailAsRetry()
    fpath = _tmp_file(tmp_path, "a.py", "line1\nline2\nline3\n")
    args = {"path": fpath, "old_string": "line2", "new_string": "replacement"}
    result = _run(cap.before_tool_execute(
        None, call=_edit_call(), tool_def=None, args=args,
    ))
    assert result is args


def test_edit_file_guardrail_before_execute_old_string_ambiguous(tmp_path):
    """old_string matches multiple locations → ModelRetry with match count."""
    cap = EditFileGuardrailAsRetry()
    content = "line A\nduplicate line\nline B\nduplicate line\nline C\n"
    fpath = _tmp_file(tmp_path, "a.py", content)
    args = {"path": fpath, "old_string": "duplicate line\n", "new_string": "replacement"}
    with pytest.raises(ModelRetry) as exc:
        _run(cap.before_tool_execute(
            None, call=_edit_call(), tool_def=None, args=args,
        ))
    msg = str(exc.value)
    assert "appears 2 times" in msg
    assert fpath in msg
    assert "above AND below" in msg
    assert "disambiguate" in msg


def test_edit_file_guardrail_before_execute_old_string_unique(tmp_path):
    """Single-match old_string passes through normally (different shape than _found test)."""
    cap = EditFileGuardrailAsRetry()
    content = "header\nunique middle line\nfooter\nheader2\nunique middle other\nfooter2\n"
    fpath = _tmp_file(tmp_path, "b.py", content)
    args = {"path": fpath, "old_string": "unique middle line", "new_string": "replacement"}
    result = _run(cap.before_tool_execute(
        None, call=_edit_call(), tool_def=None, args=args,
    ))
    assert result is args


def test_edit_file_guardrail_before_execute_old_string_not_found(tmp_path):
    """old_string NOT in file → ModelRetry with path and diagnostic message."""
    cap = EditFileGuardrailAsRetry()
    fpath = _tmp_file(tmp_path, "a.py", "line1\nline2\n")
    args = {"path": fpath, "old_string": "missing_line", "new_string": "replacement"}
    with pytest.raises(ModelRetry) as exc:
        _run(cap.before_tool_execute(
            None, call=_edit_call(), tool_def=None, args=args,
        ))
    msg = str(exc.value)
    assert "old_string not found" in msg
    assert fpath in msg
    assert "read_file" in msg
    assert "Do not reconstruct from memory" in msg


def test_edit_file_guardrail_before_execute_old_string_with_blank_lines(tmp_path):
    """old_string with exact blank-line count must match when file has them."""
    cap = EditFileGuardrailAsRetry()
    content = "def foo():\n    pass\n\n\ndef bar():\n    pass\n"
    fpath = _tmp_file(tmp_path, "a.py", content)
    # Exact substring — two blank lines before def bar.
    args = {"path": fpath, "old_string": "    pass\n\n\ndef bar():", "new_string": "x"}
    result = _run(cap.before_tool_execute(
        None, call=_edit_call(), tool_def=None, args=args,
    ))
    assert result is args


def test_edit_file_guardrail_before_execute_wrong_blank_line_count(tmp_path):
    """One blank line instead of two → ModelRetry (doesn't match file content)."""
    cap = EditFileGuardrailAsRetry()
    content = "def foo():\n    pass\n\n\ndef bar():\n    pass\n"
    fpath = _tmp_file(tmp_path, "a.py", content)
    # Wrong: only one blank line where file has two.
    args = {"path": fpath, "old_string": "    pass\n\ndef bar():", "new_string": "x"}
    with pytest.raises(ModelRetry) as exc:
        _run(cap.before_tool_execute(
            None, call=_edit_call(), tool_def=None, args=args,
        ))
    assert "old_string not found" in str(exc.value)


def test_edit_file_guardrail_before_execute_non_edit_file_passthrough():
    """Non-edit_file tools pass through without reading anything."""
    cap = EditFileGuardrailAsRetry()
    args = {"pattern": "something"}
    result = _run(cap.before_tool_execute(
        None, call=_grep_call("grep"), tool_def=None, args=args,
    ))
    assert result is args


def test_edit_file_guardrail_before_execute_missing_old_string():
    """Missing old_string → pass through (let the real tool handle it)."""
    cap = EditFileGuardrailAsRetry()
    args = {"path": "somefile.py", "new_string": "replacement"}
    result = _run(cap.before_tool_execute(
        None, call=_edit_call(), tool_def=None, args=args,
    ))
    assert result is args


def test_edit_file_guardrail_before_execute_empty_old_string():
    """Empty old_string → pass through (let the real tool handle it)."""
    cap = EditFileGuardrailAsRetry()
    args = {"path": "somefile.py", "old_string": "", "new_string": "replacement"}
    result = _run(cap.before_tool_execute(
        None, call=_edit_call(), tool_def=None, args=args,
    ))
    assert result is args


def test_edit_file_guardrail_before_execute_missing_path():
    """Missing path arg → pass through (let the real tool handle it)."""
    cap = EditFileGuardrailAsRetry()
    args = {"old_string": "something", "new_string": "replacement"}
    result = _run(cap.before_tool_execute(
        None, call=_edit_call(), tool_def=None, args=args,
    ))
    assert result is args


def test_edit_file_guardrail_before_execute_file_not_found(tmp_path):
    """FileNotFoundError → pass through (let the real tool handle it)."""
    cap = EditFileGuardrailAsRetry()
    args = {"path": str(tmp_path / "nonexistent.py"), "old_string": "x", "new_string": "y"}
    result = _run(cap.before_tool_execute(
        None, call=_edit_call(), tool_def=None, args=args,
    ))
    assert result is args


def test_edit_file_guardrail_before_execute_object_args(tmp_path):
    """Object-style args (not dict) should work for extraction."""
    cap = EditFileGuardrailAsRetry()
    fpath = _tmp_file(tmp_path, "a.py", "hello world\n")
    args = SimpleNamespace(path=fpath, old_string="hello world", new_string="hi")
    result = _run(cap.before_tool_execute(
        None, call=_edit_call(), tool_def=None, args=args,
    ))
    assert result is args


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


def test_grep_guardrail_recovery_message_suggests_read_file():
    """The ModelRetry raised at threshold must suggest read_file as an alternative."""
    cap = GrepGuardrailAsRetry()
    for _ in range(GrepGuardrailAsRetry._THRESHOLD - 1):
        _run(cap.after_tool_execute(
            None, call=_grep_call(), tool_def=None, args={},
            result="No matches for 'foo'",
        ))
    with pytest.raises(ModelRetry) as exc:
        _run(cap.after_tool_execute(
            None, call=_grep_call(), tool_def=None, args={},
            result="No matches for 'bar'",
        ))
    msg = str(exc.value)
    assert "read_file" in msg
    assert "ls/glob" in msg


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
# Read-whole guidance in agent system prompts
# ---------------------------------------------------------------------------

READ_WHOLE_TEXT = "Read files whole"


@pytest.mark.parametrize(
    "agent_name",
    [
        "explore",
        "implement",
        "refine",
    ],
)
def test_agent_prompt_includes_read_whole_guidance(agent_name):
    """Ensure each agent's system prompt contains read-whole guidance."""
    path = resolve_agent_path(agent_name)
    _, system_prompt = parse_agent_md(path)
    assert READ_WHOLE_TEXT in system_prompt, (
        f"Agent '{agent_name}' system prompt missing read-whole guidance.\n"
        f"Expected text: '{READ_WHOLE_TEXT}'"
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
    "explore",
    "implement",
    "python_review",
    "refine",
    "test_writer",
    "trace_analyst",
]


@pytest.mark.parametrize("agent_name", AGENTS_WITH_ANTI_HALLUCINATION)
def test_agent_prompt_includes_anti_hallucination_guard(agent_name):
    """Each agent that lacks an execute tool must carry the
    defensive anti-hallucination blockquote in its system prompt."""
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



ANTIPATTERN_EXAMPLES_TEXT = (
    "> **Anti-pattern examples:**\n"
    "> - **BAD:** `execute('git log')` or `bash('ls')`"
    " — you do not have these tools.\n"
    "> - **GOOD:** use `read_file`, `grep`, `glob`,"
    " or `ls` to discover what changed."
)

AGENTS_WITH_ANTIPATTERN_EXAMPLES = [
    "python_review",
    "implement",
    "docs",
    "test_writer",
    "refine",
    "trace_analyst",
]


@pytest.mark.parametrize("agent_name", AGENTS_WITH_ANTIPATTERN_EXAMPLES)
def test_agent_prompt_includes_antipattern_examples(agent_name):
    """Anti-pattern BAD/GOOD examples are present in every agent
    that lacks execute/bash/shell/run tools."""
    path = resolve_agent_path(agent_name)
    _, system_prompt = parse_agent_md(path)
    assert ANTIPATTERN_EXAMPLES_TEXT in system_prompt, (
        f"Anti-pattern examples missing from {agent_name!r} prompt.\n"
        f"Expected text:\n{ANTIPATTERN_EXAMPLES_TEXT}"
    )


@pytest.mark.parametrize("agent_name", AGENTS_WITH_ANTIPATTERN_EXAMPLES)
def test_antipattern_examples_positioned_after_anti_hallucination_guard(
    agent_name,
):
    """Anti-pattern examples appear after the anti-hallucination blockquote."""
    path = resolve_agent_path(agent_name)
    _, system_prompt = parse_agent_md(path)
    guard_idx = system_prompt.index(ANTI_HALLUCINATION_TEXT)
    antipattern_idx = system_prompt.index(ANTIPATTERN_EXAMPLES_TEXT)
    assert antipattern_idx > guard_idx, (
        f"Anti-pattern examples must come after the anti-hallucination "
        f"blockquote in {agent_name!r} prompt."
    )


def test_antipattern_examples_absent_from_explore():
    """Explore agent should NOT contain anti-pattern examples since it
    is read-only and its description already states its constraints."""
    path = resolve_agent_path("explore")
    _, system_prompt = parse_agent_md(path)
    assert ANTIPATTERN_EXAMPLES_TEXT not in system_prompt, (
        "Anti-pattern examples found unexpectedly in explore agent prompt."
    )


# ---------------------------------------------------------------------------
# raise_issue tool in agent frontmatter
# ---------------------------------------------------------------------------

PRO_MODEL_AGENTS = [
    "docs",
    "spike",
    "duplication_auditor",
    "audit",
    "implement",
    "python_review",
    "architecture_auditor",
    "refine",
    "deps_auditor",
    "security_auditor",
    "sourcing",
    "explore",
]

FLASH_MODEL_AGENTS = [
    "trace_analyst",
    "issue_deduplicator",
    "merge_evaluator",
    "resolve_step",
    "memory_audit",
    "test_writer",
]


@pytest.mark.parametrize("agent_name", PRO_MODEL_AGENTS)
def test_pro_model_agent_has_raise_issue_tool(agent_name: str):
    """Every pro-model agent definition should list ``raise_issue`` in
    its YAML frontmatter ``tools`` list."""
    path = resolve_agent_path(agent_name)
    config, _ = parse_agent_md(path)
    tools = config.get("tools", [])
    assert "raise_issue" in tools, (
        f"{agent_name}.md is a pro-model agent but its frontmatter "
        f"tools list does not include 'raise_issue'. Found: {tools}"
    )


@pytest.mark.parametrize("agent_name", FLASH_MODEL_AGENTS)
def test_flash_model_agent_does_not_have_raise_issue(agent_name: str):
    """Flash-model agents should NOT list ``raise_issue`` in their
    frontmatter ``tools`` list — only pro-model agents get it."""
    path = resolve_agent_path(agent_name)
    config, _ = parse_agent_md(path)
    tools = config.get("tools", [])
    assert "raise_issue" not in tools, (
        f"{agent_name}.md is a flash-model agent but unexpectedly "
        f"has 'raise_issue' in its tools list: {tools}"
    )


# ---------------------------------------------------------------------------
# task tool parameter-name note
# ---------------------------------------------------------------------------

_TASK_TOOL_PARAM_TEXT = (
    "When calling the `task` tool, pass the subagent instructions as "
    "`description=`, not `prompt=`. The `task` tool has no `prompt` parameter."
)


@pytest.mark.parametrize(
    "agent_name",
    [
        "refine",
        "audit",
        "security_auditor",
        "deps_auditor",
        "architecture_auditor",
        "sourcing",
    ],
)
def test_agent_prompt_includes_task_tool_parameter_note(agent_name: str):
    """Every agent that uses subagents must warn the model to pass
    instructions as ``description=``, not ``prompt=``, since the ``task``
    tool has no ``prompt`` parameter."""
    path = resolve_agent_path(agent_name)
    _, system_prompt = parse_agent_md(path)
    assert _TASK_TOOL_PARAM_TEXT in system_prompt, (
        f"{agent_name}.md system prompt missing the task-tool parameter-name note. "
        f"Expected note:\n{_TASK_TOOL_PARAM_TEXT}"
    )


# ---------------------------------------------------------------------------
# HistoryCompactorCapability
# ---------------------------------------------------------------------------


def _make_ctx(*, messages):
    """Build a mock RunContext carrying the given messages list."""
    return SimpleNamespace(messages=messages)


def _make_request_context(*, messages):
    """Build a minimal ModelRequestContext via direct construction."""
    return ModelRequestContext(
        model=None,
        messages=messages,
        model_settings=None,
        model_request_parameters=None,
    )


def test_history_compactor_before_model_request_read_file():
    """First read_file return is compacted when a newer read on the same
    path supersedes it."""
    cap = HistoryCompactorCapability()

    tc1 = ToolCallPart(tool_name="read_file", args={"path": "a.py"}, tool_call_id="c1")
    tr1 = ToolReturnPart(tool_name="read_file", content="old content", tool_call_id="c1")
    tc2 = ToolCallPart(tool_name="read_file", args={"path": "a.py"}, tool_call_id="c2")
    tr2 = ToolReturnPart(tool_name="read_file", content="new content", tool_call_id="c2")

    rc = _make_request_context(
        messages=[
            ModelResponse(parts=[tc1]),
            ModelRequest(parts=[tr1]),
            ModelResponse(parts=[tc2]),
            ModelRequest(parts=[tr2]),
        ],
    )

    result = _run(cap.before_model_request(None, rc))
    msgs = result.messages

    # First return (index 1) should be compacted.
    parts1 = msgs[1].parts
    assert len(parts1) == 1
    assert isinstance(parts1[0], ToolReturnPart)
    assert parts1[0].content.startswith("[Content omitted")
    assert "a.py" in parts1[0].content

    # Second return (index 3) should be untouched.
    parts3 = msgs[3].parts
    assert len(parts3) == 1
    assert isinstance(parts3[0], ToolReturnPart)
    assert parts3[0].content == "new content"


def test_history_compactor_before_model_request_ls():
    """Older ls return is compacted when superseded by a newer ls on same path."""
    cap = HistoryCompactorCapability()

    tc1 = ToolCallPart(tool_name="ls", args={"path": "dir"}, tool_call_id="c1")
    tr1 = ToolReturnPart(tool_name="ls", content="old listing", tool_call_id="c1")
    tc2 = ToolCallPart(tool_name="ls", args={"path": "dir"}, tool_call_id="c2")
    tr2 = ToolReturnPart(tool_name="ls", content="new listing", tool_call_id="c2")

    rc = _make_request_context(
        messages=[
            ModelResponse(parts=[tc1]),
            ModelRequest(parts=[tr1]),
            ModelResponse(parts=[tc2]),
            ModelRequest(parts=[tr2]),
        ],
    )

    result = _run(cap.before_model_request(None, rc))

    assert result.messages[1].parts[0].content.startswith("[Content omitted")
    assert result.messages[3].parts[0].content == "new listing"


def test_history_compactor_wrap_tool_execute_short_circuit():
    """Duplicate read_file with identical args and no intervening file edit
    returns the cached content from the prior ToolReturnPart without calling
    the handler."""
    cap = HistoryCompactorCapability()
    handler_called = False

    async def handler(args):
        nonlocal handler_called
        handler_called = True
        return "file content"

    prior_tc = ToolCallPart(tool_name="read_file", args={"path": "x.py", "offset": 0, "limit": 200}, tool_call_id="c1")
    prior_tr = ToolReturnPart(tool_name="read_file", content="old file content", tool_call_id="c1")

    ctx = _make_ctx(
        messages=[
            ModelResponse(parts=[prior_tc]),
            ModelRequest(parts=[prior_tr]),
        ],
    )

    call = ToolCallPart(tool_name="read_file", args={"path": "x.py", "offset": 0, "limit": 200}, tool_call_id="c2")

    result = _run(cap.wrap_tool_execute(ctx, call=call, tool_def=None, args={}, handler=handler))

    assert not handler_called
    assert result == "old file content"


def test_history_compactor_wrap_tool_execute_non_matching():
    """Different args (different offset) must pass through to the handler."""
    cap = HistoryCompactorCapability()
    handler_called = False

    async def handler(args):
        nonlocal handler_called
        handler_called = True
        return "file content page 2"

    prior_tc = ToolCallPart(tool_name="read_file", args={"path": "x.py", "offset": 0, "limit": 200}, tool_call_id="c1")
    prior_tr = ToolReturnPart(tool_name="read_file", content="old content", tool_call_id="c1")

    ctx = _make_ctx(
        messages=[
            ModelResponse(parts=[prior_tc]),
            ModelRequest(parts=[prior_tr]),
        ],
    )

    call = ToolCallPart(tool_name="read_file", args={"path": "x.py", "offset": 200, "limit": 200}, tool_call_id="c2")

    result = _run(cap.wrap_tool_execute(ctx, call=call, tool_def=None, args={}, handler=handler))

    assert handler_called
    assert result == "file content page 2"


def test_history_compactor_wrap_tool_execute_with_intervening_edit():
    """When a file-modifying tool call occurred between two identical
    read_file calls, the handler must be invoked."""
    cap = HistoryCompactorCapability()
    handler_called = False

    async def handler(args):
        nonlocal handler_called
        handler_called = True
        return "re-read content"

    prior_tc = ToolCallPart(tool_name="read_file", args={"path": "x.py"}, tool_call_id="c1")
    prior_tr = ToolReturnPart(tool_name="read_file", content="old content", tool_call_id="c1")
    write_tc = ToolCallPart(tool_name="write_file", args={"path": "x.py", "content": "new"}, tool_call_id="c2")
    write_tr = ToolReturnPart(tool_name="write_file", content="Wrote file", tool_call_id="c2")

    ctx = _make_ctx(
        messages=[
            ModelResponse(parts=[prior_tc]),
            ModelRequest(parts=[prior_tr]),
            ModelResponse(parts=[write_tc]),
            ModelRequest(parts=[write_tr]),
        ],
    )

    call = ToolCallPart(tool_name="read_file", args={"path": "x.py"}, tool_call_id="c3")

    result = _run(cap.wrap_tool_execute(ctx, call=call, tool_def=None, args={}, handler=handler))

    assert handler_called
    assert result == "re-read content"


def test_history_compactor_non_read_file_passthrough():
    """Non-read_file tools always pass through to the handler."""
    cap = HistoryCompactorCapability()
    handler_called = False

    async def handler(args):
        nonlocal handler_called
        handler_called = True
        return "ls output"

    ctx = _make_ctx(messages=[])
    call = ToolCallPart(tool_name="ls", args={"path": "."}, tool_call_id="c1")

    result = _run(cap.wrap_tool_execute(ctx, call=call, tool_def=None, args={}, handler=handler))

    assert handler_called
    assert result == "ls output"


def test_history_compactor_before_model_request_glob():
    """Older glob return is compacted when superseded by a newer glob on the
    same (pattern, path)."""
    cap = HistoryCompactorCapability()

    tc1 = ToolCallPart(tool_name="glob", args={"pattern": "*.py", "path": "src"}, tool_call_id="c1")
    tr1 = ToolReturnPart(tool_name="glob", content="old glob results", tool_call_id="c1")
    tc2 = ToolCallPart(tool_name="glob", args={"pattern": "*.py", "path": "src"}, tool_call_id="c2")
    tr2 = ToolReturnPart(tool_name="glob", content="new glob results", tool_call_id="c2")

    rc = _make_request_context(
        messages=[
            ModelResponse(parts=[tc1]),
            ModelRequest(parts=[tr1]),
            ModelResponse(parts=[tc2]),
            ModelRequest(parts=[tr2]),
        ],
    )

    result = _run(cap.before_model_request(None, rc))

    assert result.messages[1].parts[0].content.startswith("[Content omitted")
    assert "pattern='*.py'" in result.messages[1].parts[0].content
    assert "path='src'" in result.messages[1].parts[0].content
    assert result.messages[3].parts[0].content == "new glob results"


def test_history_compactor_before_model_request_grep():
    """Older grep return is compacted when superseded by a newer grep on the
    same (pattern, path, glob_pattern)."""
    cap = HistoryCompactorCapability()

    tc1 = ToolCallPart(
        tool_name="grep",
        args={"pattern": "TODO", "path": "src", "glob_pattern": "*.py"},
        tool_call_id="c1",
    )
    tr1 = ToolReturnPart(tool_name="grep", content="old grep results", tool_call_id="c1")
    tc2 = ToolCallPart(
        tool_name="grep",
        args={"pattern": "TODO", "path": "src", "glob_pattern": "*.py"},
        tool_call_id="c2",
    )
    tr2 = ToolReturnPart(tool_name="grep", content="new grep results", tool_call_id="c2")

    rc = _make_request_context(
        messages=[
            ModelResponse(parts=[tc1]),
            ModelRequest(parts=[tr1]),
            ModelResponse(parts=[tc2]),
            ModelRequest(parts=[tr2]),
        ],
    )

    result = _run(cap.before_model_request(None, rc))

    assert result.messages[1].parts[0].content.startswith("[Content omitted")
    assert "pattern='TODO'" in result.messages[1].parts[0].content
    assert "glob_pattern='*.py'" in result.messages[1].parts[0].content
    assert result.messages[3].parts[0].content == "new grep results"


def test_history_compactor_before_model_request_non_compactable_ignored():
    """Non-compactable tool returns (e.g. write_file) are left untouched."""
    cap = HistoryCompactorCapability()

    tc1 = ToolCallPart(tool_name="write_file", args={"path": "a.py"}, tool_call_id="c1")
    tr1 = ToolReturnPart(tool_name="write_file", content="Wrote file", tool_call_id="c1")
    tc2 = ToolCallPart(tool_name="write_file", args={"path": "a.py"}, tool_call_id="c2")
    tr2 = ToolReturnPart(tool_name="write_file", content="Wrote file again", tool_call_id="c2")

    rc = _make_request_context(
        messages=[
            ModelResponse(parts=[tc1]),
            ModelRequest(parts=[tr1]),
            ModelResponse(parts=[tc2]),
            ModelRequest(parts=[tr2]),
        ],
    )

    result = _run(cap.before_model_request(None, rc))

    # Neither return should be compacted — write_file is not compactable.
    assert result.messages[1].parts[0].content == "Wrote file"
    assert result.messages[3].parts[0].content == "Wrote file again"


def test_history_compactor_before_model_request_multiple_superseded():
    """When three calls target the same file, the first two are compacted and
    only the last survives intact."""
    cap = HistoryCompactorCapability()

    tc1 = ToolCallPart(tool_name="read_file", args={"path": "a.py"}, tool_call_id="c1")
    tr1 = ToolReturnPart(tool_name="read_file", content="v1", tool_call_id="c1")
    tc2 = ToolCallPart(tool_name="read_file", args={"path": "a.py"}, tool_call_id="c2")
    tr2 = ToolReturnPart(tool_name="read_file", content="v2", tool_call_id="c2")
    tc3 = ToolCallPart(tool_name="read_file", args={"path": "a.py"}, tool_call_id="c3")
    tr3 = ToolReturnPart(tool_name="read_file", content="v3", tool_call_id="c3")

    rc = _make_request_context(
        messages=[
            ModelResponse(parts=[tc1]),
            ModelRequest(parts=[tr1]),
            ModelResponse(parts=[tc2]),
            ModelRequest(parts=[tr2]),
            ModelResponse(parts=[tc3]),
            ModelRequest(parts=[tr3]),
        ],
    )

    result = _run(cap.before_model_request(None, rc))

    # First two returns (indices 1 and 3) compacted.
    assert result.messages[1].parts[0].content.startswith("[Content omitted")
    assert result.messages[3].parts[0].content.startswith("[Content omitted")
    # Last return (index 5) intact.
    assert result.messages[5].parts[0].content == "v3"


def test_history_compactor_before_model_request_mixed_tools():
    """Compactable and non-compactable returns can coexist; only compactable
    ones are affected."""
    cap = HistoryCompactorCapability()

    tc_r1 = ToolCallPart(tool_name="read_file", args={"path": "a.py"}, tool_call_id="c1")
    tr_r1 = ToolReturnPart(tool_name="read_file", content="r1", tool_call_id="c1")
    tc_w = ToolCallPart(tool_name="write_file", args={"path": "b.py"}, tool_call_id="c2")
    tr_w = ToolReturnPart(tool_name="write_file", content="w1", tool_call_id="c2")
    tc_r2 = ToolCallPart(tool_name="read_file", args={"path": "a.py"}, tool_call_id="c3")
    tr_r2 = ToolReturnPart(tool_name="read_file", content="r2", tool_call_id="c3")

    rc = _make_request_context(
        messages=[
            ModelResponse(parts=[tc_r1]),
            ModelRequest(parts=[tr_r1]),
            ModelResponse(parts=[tc_w]),
            ModelRequest(parts=[tr_w]),
            ModelResponse(parts=[tc_r2]),
            ModelRequest(parts=[tr_r2]),
        ],
    )

    result = _run(cap.before_model_request(None, rc))

    # First read_file compacted.
    assert result.messages[1].parts[0].content.startswith("[Content omitted")
    # write_file untouched (non-compactable).
    assert result.messages[3].parts[0].content == "w1"
    # Last read_file intact.
    assert result.messages[5].parts[0].content == "r2"


@pytest.mark.parametrize("edit_tool_name", [
    "edit_file",
    "move_file",
    "delete_file",
    "batch_move",
    "batch_delete",
])
def test_history_compactor_wrap_tool_execute_intervening_edits(edit_tool_name):
    """Each file-modifying tool between identical reads forces a re-read."""
    cap = HistoryCompactorCapability()
    handler_called = False

    async def handler(args):
        nonlocal handler_called
        handler_called = True
        return "re-read content"

    prior_tc = ToolCallPart(tool_name="read_file", args={"path": "x.py"}, tool_call_id="c1")
    prior_tr = ToolReturnPart(tool_name="read_file", content="old", tool_call_id="c1")
    edit_tc = ToolCallPart(tool_name=edit_tool_name, args={"path": "x.py"}, tool_call_id="c2")
    edit_tr = ToolReturnPart(tool_name=edit_tool_name, content="ok", tool_call_id="c2")

    ctx = _make_ctx(
        messages=[
            ModelResponse(parts=[prior_tc]),
            ModelRequest(parts=[prior_tr]),
            ModelResponse(parts=[edit_tc]),
            ModelRequest(parts=[edit_tr]),
        ],
    )

    call = ToolCallPart(tool_name="read_file", args={"path": "x.py"}, tool_call_id="c3")

    result = _run(cap.wrap_tool_execute(ctx, call=call, tool_def=None, args={}, handler=handler))

    assert handler_called
    assert result == "re-read content"


def test_history_compactor_wrap_tool_execute_non_edit_tools_preserve_short_circuit():
    """Non-file-modifying tools (ls, glob, grep) between identical reads do
    NOT count as intervening edits, so short-circuit is preserved."""
    cap = HistoryCompactorCapability()
    handler_called = False

    async def handler(args):
        nonlocal handler_called
        handler_called = True
        return "file content"

    prior_tc = ToolCallPart(tool_name="read_file", args={"path": "x.py"}, tool_call_id="c1")
    prior_tr = ToolReturnPart(tool_name="read_file", content="old", tool_call_id="c1")
    ls_tc = ToolCallPart(tool_name="ls", args={"path": "."}, tool_call_id="c2")
    ls_tr = ToolReturnPart(tool_name="ls", content="dir listing", tool_call_id="c2")
    glob_tc = ToolCallPart(tool_name="glob", args={"pattern": "*.py"}, tool_call_id="c3")
    glob_tr = ToolReturnPart(tool_name="glob", content="glob results", tool_call_id="c3")

    ctx = _make_ctx(
        messages=[
            ModelResponse(parts=[prior_tc]),
            ModelRequest(parts=[prior_tr]),
            ModelResponse(parts=[ls_tc]),
            ModelRequest(parts=[ls_tr]),
            ModelResponse(parts=[glob_tc]),
            ModelRequest(parts=[glob_tr]),
        ],
    )

    call = ToolCallPart(tool_name="read_file", args={"path": "x.py"}, tool_call_id="c4")

    result = _run(cap.wrap_tool_execute(ctx, call=call, tool_def=None, args={}, handler=handler))

    assert not handler_called
    assert result == "old"


def test_history_compactor_wrap_tool_execute_no_prior_matching_read():
    """When no prior identical read_file call exists in the message history,
    the handler is invoked normally."""
    cap = HistoryCompactorCapability()
    handler_called = False

    async def handler(args):
        nonlocal handler_called
        handler_called = True
        return "first read"

    # Message history has a read_file on a different path.
    prior_tc = ToolCallPart(tool_name="read_file", args={"path": "other.py"}, tool_call_id="c1")
    prior_tr = ToolReturnPart(tool_name="read_file", content="other content", tool_call_id="c1")

    ctx = _make_ctx(
        messages=[
            ModelResponse(parts=[prior_tc]),
            ModelRequest(parts=[prior_tr]),
        ],
    )

    call = ToolCallPart(tool_name="read_file", args={"path": "x.py"}, tool_call_id="c2")

    result = _run(cap.wrap_tool_execute(ctx, call=call, tool_def=None, args={}, handler=handler))

    assert handler_called
    assert result == "first read"


def test_history_compactor_wrap_tool_execute_skips_self_match():
    """The current ToolCallPart sits inside ctx.messages by the time
    wrap_tool_execute fires (especially for parallel tool calls in one
    ModelResponse). The scan must skip it by tool_call_id, otherwise every
    read_file would self-match and short-circuit to the warning string."""
    cap = HistoryCompactorCapability()
    handler_called = False

    async def handler(args):
        nonlocal handler_called
        handler_called = True
        return "file content"

    # Simulate the runtime state: the model emitted three parallel read_file
    # calls and the ModelResponse holding all three is already in ctx.messages.
    call_a = ToolCallPart(tool_name="read_file", args={"path": "a.py"}, tool_call_id="c1")
    call_b = ToolCallPart(tool_name="read_file", args={"path": "b.py"}, tool_call_id="c2")
    call_c = ToolCallPart(tool_name="read_file", args={"path": "c.py"}, tool_call_id="c3")

    ctx = _make_ctx(messages=[ModelResponse(parts=[call_a, call_b, call_c])])

    result = _run(cap.wrap_tool_execute(ctx, call=call_a, tool_def=None, args={}, handler=handler))

    assert handler_called
    assert result == "file content"


def test_history_compactor_wired_into_build_deep_agent_capabilities(monkeypatch):
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
    assert "HistoryCompactorCapability" in cap_types


def test_history_compactor_wrap_tool_execute_three_different_cached_files():
    """Three different cached files in sequence all succeed — each returns
    its respective cached content, so the 'same result 3 times' counter
    does not block genuinely new reads."""
    cap = HistoryCompactorCapability()
    handler_called = [False]

    async def handler(_args):
        handler_called[0] = True
        return "should-not-reach"

    # Prior reads for files A, B, C.
    tc_a = ToolCallPart(tool_name="read_file", args={"path": "a.py"}, tool_call_id="c1")
    tr_a = ToolReturnPart(tool_name="read_file", content="content-A", tool_call_id="c1")
    tc_b = ToolCallPart(tool_name="read_file", args={"path": "b.py"}, tool_call_id="c2")
    tr_b = ToolReturnPart(tool_name="read_file", content="content-B", tool_call_id="c2")
    tc_c = ToolCallPart(tool_name="read_file", args={"path": "c.py"}, tool_call_id="c3")
    tr_c = ToolReturnPart(tool_name="read_file", content="content-C", tool_call_id="c3")

    ctx = _make_ctx(
        messages=[
            ModelResponse(parts=[tc_a]),
            ModelRequest(parts=[tr_a]),
            ModelResponse(parts=[tc_b]),
            ModelRequest(parts=[tr_b]),
            ModelResponse(parts=[tc_c]),
            ModelRequest(parts=[tr_c]),
        ],
    )

    call_a = ToolCallPart(tool_name="read_file", args={"path": "a.py"}, tool_call_id="c4")
    call_b = ToolCallPart(tool_name="read_file", args={"path": "b.py"}, tool_call_id="c5")
    call_c = ToolCallPart(tool_name="read_file", args={"path": "c.py"}, tool_call_id="c6")

    r_a = _run(cap.wrap_tool_execute(ctx, call=call_a, tool_def=None, args={}, handler=handler))
    r_b = _run(cap.wrap_tool_execute(ctx, call=call_b, tool_def=None, args={}, handler=handler))
    r_c = _run(cap.wrap_tool_execute(ctx, call=call_c, tool_def=None, args={}, handler=handler))

    assert not any(handler_called)
    assert r_a == "content-A"
    assert r_b == "content-B"
    assert r_c == "content-C"


def test_history_compactor_wrap_tool_execute_warning_fallback_when_no_tool_return():
    """When a prior identical read_file call exists but its ToolReturnPart
    cannot be found (defensive scenario), the warning includes the file path
    so different files produce different strings."""
    cap = HistoryCompactorCapability()
    handler_called = [False]

    async def handler(_args):
        handler_called[0] = True
        return "should-not-reach"

    # Prior call present but its ToolReturnPart is missing from messages.
    prior_tc = ToolCallPart(tool_name="read_file", args={"path": "orphan.py"}, tool_call_id="c1")

    ctx = _make_ctx(
        messages=[
            ModelResponse(parts=[prior_tc]),
            # ModelRequest with matching ToolReturnPart is deliberately absent.
        ],
    )

    call = ToolCallPart(tool_name="read_file", args={"path": "orphan.py"}, tool_call_id="c2")

    result = _run(cap.wrap_tool_execute(ctx, call=call, tool_def=None, args={}, handler=handler))

    assert not any(handler_called)
    assert "Warning: identical read_file" in result
    assert "'orphan.py'" in result
    assert "file content has not changed" in result


def test_history_compactor_for_run_returns_fresh_instance():
    """for_run returns a new HistoryCompactorCapability instance so
    concurrent runs of an lru_cache'd agent don't share state."""
    cap = HistoryCompactorCapability()
    fresh = _run(cap.for_run(None))
    assert fresh is not cap
    assert isinstance(fresh, HistoryCompactorCapability)


# ---------------------------------------------------------------------------
# GlobPatternSanitizer
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [
        # The exact failing pattern from issue #1463.
        ("**/.github/issues**", "**/.github/issues*"),
        # Trailing-segment ** mixed with extension.
        ("src/**.py", "src/*.py"),
        # Leading-segment ** mixed with prefix.
        ("**foo/bar", "*foo/bar"),
        # Multiple offending segments.
        ("a**/b**c/d", "a*/b*c/d"),
        # Pure ** components are preserved (recursive intent intact).
        ("**", "**"),
        ("**/foo/**", "**/foo/**"),
        ("src/**/test_*.py", "src/**/test_*.py"),
        # Patterns without ** are untouched.
        ("src/*.py", "src/*.py"),
    ],
)
def test_glob_sanitizer_rewrites_only_offending_segments(raw, expected):
    assert GlobPatternSanitizer._sanitize(raw) == expected


def test_glob_sanitizer_rewrites_glob_pattern_arg():
    cap = GlobPatternSanitizer()
    args = {"pattern": "**/.github/issues**", "path": "."}
    out = _run(cap.before_tool_execute(
        None, call=_grep_call("glob"), tool_def=None, args=args,
    ))
    assert out["pattern"] == "**/.github/issues*"
    assert args["pattern"] == "**/.github/issues*"


def test_glob_sanitizer_rewrites_grep_glob_pattern_arg():
    """grep's glob_pattern field is also sanitized — same pathlib rule applies."""
    cap = GlobPatternSanitizer()
    args = {"pattern": "TODO", "glob_pattern": "src/**.py"}
    out = _run(cap.before_tool_execute(
        None, call=_grep_call("grep"), tool_def=None, args=args,
    ))
    assert out["glob_pattern"] == "src/*.py"


def test_glob_sanitizer_passes_through_other_tools():
    cap = GlobPatternSanitizer()
    args = {"path": "**/foo**"}
    out = _run(cap.before_tool_execute(
        None, call=_grep_call("read_file"), tool_def=None, args=args,
    ))
    assert out is args
    assert args == {"path": "**/foo**"}


def test_glob_sanitizer_leaves_valid_pattern_unchanged():
    cap = GlobPatternSanitizer()
    args = {"pattern": "**/*.py", "path": "."}
    out = _run(cap.before_tool_execute(
        None, call=_grep_call("glob"), tool_def=None, args=args,
    ))
    assert out["pattern"] == "**/*.py"


def test_glob_sanitizer_wired_into_build_deep_agent_capabilities(monkeypatch):
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
    assert "GlobPatternSanitizer" in cap_types


# ToolErrorAsRetry

def test_tool_error_as_retry_converts_valueerror_to_model_retry():
    """A ValueError (e.g. from an invalid glob pattern) is converted to ModelRetry."""
    cap = ToolErrorAsRetry()
    call = _grep_call("glob")
    with pytest.raises(ModelRetry) as exc_info:
        _run(cap.on_tool_execute_error(
            ctx=None, call=call, tool_def=None,
            args={"pattern": "**/.github/issues**"},
            error=ValueError("'**' can only be an entire path component"),
        ))
    assert "glob" in str(exc_info.value)
    assert "ValueError" in str(exc_info.value)
    assert "'**' can only be an entire path component" in str(exc_info.value)


def test_tool_error_as_retry_re_raises_model_retry_untouched():
    """ModelRetry passes through unchanged so existing retry machinery still works."""
    cap = ToolErrorAsRetry()
    call = _grep_call("glob")
    original = ModelRetry("Custom retry message")
    with pytest.raises(ModelRetry) as exc_info:
        _run(cap.on_tool_execute_error(
            ctx=None, call=call, tool_def=None, args={},
            error=original,
        ))
    assert exc_info.value is original
    assert str(exc_info.value) == "Custom retry message"


def test_tool_error_as_retry_includes_tool_name_and_error_details():
    """The retry message tells the model which tool failed and what the error was."""
    cap = ToolErrorAsRetry()
    call = _grep_call("grep")
    with pytest.raises(ModelRetry) as exc_info:
        _run(cap.on_tool_execute_error(
            ctx=None, call=call, tool_def=None, args={},
            error=PermissionError("Permission denied: /root/.ssh"),
        ))
    message = str(exc_info.value)
    assert "'grep'" in message
    assert "PermissionError" in message
    assert "Permission denied: /root/.ssh" in message
    assert "Adjust the arguments and try again." in message


def test_tool_error_as_retry_converts_generic_exception():
    """Any exception type is converted to ModelRetry, not just ValueError."""
    cap = ToolErrorAsRetry()
    call = _grep_call("edit_file")
    with pytest.raises(ModelRetry) as exc_info:
        _run(cap.on_tool_execute_error(
            ctx=None, call=call, tool_def=None, args={},
            error=RuntimeError("Unexpected failure"),
        ))
    assert "edit_file" in str(exc_info.value)
    assert "RuntimeError" in str(exc_info.value)
    assert "Unexpected failure" in str(exc_info.value)


# ---------------------------------------------------------------------------
# UnknownToolRetry
# ---------------------------------------------------------------------------


def test_unknown_tool_retry_enriches_message():
    """ModelRetry starting with 'Unknown tool name:' gets enriched guidance."""
    cap = UnknownToolRetry()
    original = ModelRetry(
        "Unknown tool name: 'execute'. "
        "Available tools: ls, read_file, write_file, edit_file, glob, grep."
    )
    with pytest.raises(ModelRetry) as exc:
        _run(cap.on_tool_execute_error(
            None,
            call=SimpleNamespace(tool_name="execute"),
            tool_def=None,
            args={},
            error=original,
        ))
    msg = str(exc.value)
    assert "Unknown tool name: 'execute'" in msg
    assert "The tool you called does not exist" in msg
    assert "Available tools: ls, read_file, write_file, edit_file, glob, grep" in msg
    assert "You cannot run shell commands" in msg
    assert "Use read_file or grep to inspect files instead" in msg


def test_unknown_tool_retry_passes_through_other_errors():
    """Non-ModelRetry errors are not consumed by this guardrail."""
    cap = UnknownToolRetry()
    result = _run(cap.on_tool_execute_error(
        None,
        call=SimpleNamespace(tool_name="execute"),
        tool_def=None,
        args={},
        error=ValueError("something went wrong"),
    ))
    assert result is None


def test_unknown_tool_retry_passes_through_other_model_retry():
    """ModelRetry without 'Unknown tool name:' prefix is re-raised unchanged."""
    cap = UnknownToolRetry()
    original = ModelRetry("Some other retry message")
    with pytest.raises(ModelRetry) as exc:
        _run(cap.on_tool_execute_error(
            None,
            call=SimpleNamespace(tool_name="edit_file"),
            tool_def=None,
            args={},
            error=original,
        ))
    assert exc.value is original
    assert str(exc.value) == "Some other retry message"


def test_unknown_tool_retry_wired_into_build_deep_agent_capabilities(monkeypatch):
    """UnknownToolRetry is registered in the build_deep_agent capabilities list."""
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
    assert "UnknownToolRetry" in cap_types


def test_unknown_tool_retry_re_raises_on_non_matching_format():
    """When ``Unknown tool name:`` is the prefix but the rest of the
    message doesn't match the expected pydantic_ai format, the original
    error is re-raised unchanged so downstream retry machinery still works."""
    import cai.agents.loader as loader

    cap = loader.UnknownToolRetry()
    call = SimpleNamespace(tool_name="invalid_tool")
    tool_def = object()
    args = {}
    original = ModelRetry(
        "Unknown tool name: 'some_tool' (unexpected format)."
    )

    with pytest.raises(ModelRetry) as exc_info:
        asyncio.run(
            cap.on_tool_execute_error(
                None, call=call, tool_def=tool_def, args=args, error=original,
            )
        )
    assert str(exc_info.value) == str(original)


# ---------------------------------------------------------------------------
# MicroReadGuardCapability
# ---------------------------------------------------------------------------


def _micro_read_call(tool_name="read_file"):
    return SimpleNamespace(tool_name=tool_name)


def test_micro_read_guard_extends_limit_below_minimum():
    """limit below 200 is bumped to 200."""
    cap = MicroReadGuardCapability()
    args = {"path": "a.py", "limit": 15}
    out = _run(cap.before_tool_execute(
        None, call=_micro_read_call(), tool_def=None, args=args,
    ))
    assert out["limit"] == 200


def test_micro_read_guard_leaves_limit_at_or_above_minimum():
    """limit >= 200 is left unchanged."""
    cap = MicroReadGuardCapability()
    args = {"path": "a.py", "limit": 200}
    out = _run(cap.before_tool_execute(
        None, call=_micro_read_call(), tool_def=None, args=args,
    ))
    assert out["limit"] == 200

    args = {"path": "a.py", "limit": 500}
    out = _run(cap.before_tool_execute(
        None, call=_micro_read_call(), tool_def=None, args=args,
    ))
    assert out["limit"] == 500


def test_micro_read_guard_leaves_absent_limit_alone():
    """When limit is not set, the args are passed through unchanged."""
    cap = MicroReadGuardCapability()
    args = {"path": "a.py"}
    out = _run(cap.before_tool_execute(
        None, call=_micro_read_call(), tool_def=None, args=args,
    ))
    assert out is args
    assert "limit" not in out


def test_micro_read_guard_passes_through_non_read_file():
    """Non-read_file tools are passed through unchanged."""
    cap = MicroReadGuardCapability()
    args = {"path": ".", "limit": 15}
    out = _run(cap.before_tool_execute(
        None, call=_micro_read_call("ls"), tool_def=None, args=args,
    ))
    assert out is args
    assert out["limit"] == 15


def test_micro_read_guard_object_args():
    """Object-style args with limit below minimum are bumped."""
    cap = MicroReadGuardCapability()
    args = SimpleNamespace(path="a.py", limit=60)
    out = _run(cap.before_tool_execute(
        None, call=_micro_read_call(), tool_def=None, args=args,
    ))
    assert out is args
    assert out.limit == 200


def test_micro_read_guard_wired_into_build_deep_agent_capabilities(monkeypatch):
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
    assert "MicroReadGuardCapability" in cap_types
    # Must appear before HistoryCompactorCapability so modified args reach the compactor
    micro_idx = cap_types.index("MicroReadGuardCapability")
    hist_idx = cap_types.index("HistoryCompactorCapability")
    assert micro_idx < hist_idx, (
        "MicroReadGuardCapability must be before HistoryCompactorCapability"
    )


# ---------------------------------------------------------------------------
# EditFileGuardrailAsRetry — multi-match detection edge cases
# ---------------------------------------------------------------------------


def test_edit_file_guardrail_docstring_mentions_proactive_rejection():
    """The EditFileGuardrailAsRetry docstring must describe the proactive
    multi-match rejection that fires before tool execution."""
    doc = EditFileGuardrailAsRetry.__doc__
    assert doc is not None
    assert "pre-verifies" in doc or "before the tool executes" in doc
    assert "not ambiguous" in doc or "more than once" in doc or "matching multiple" in doc
    assert "rejects" in doc
    assert "match count" in doc
    assert "disambiguation" in doc


@pytest.mark.parametrize("match_count", [2, 3, 5])
def test_edit_file_guardrail_before_execute_old_string_reports_accurate_count(
    tmp_path, match_count
):
    """The ModelRetry message must report the correct count for 2, 3, 5+ matches."""
    cap = EditFileGuardrailAsRetry()
    # Build content where old_string repeats exactly *match_count* times.
    unique_footer = "\nfooter that disambiguates nothing\n"
    lines = ["irrelevant\n"] * match_count
    content = "".join(lines) + unique_footer
    old_string = "irrelevant"
    fpath = _tmp_file(tmp_path, "multi_match.py", content)
    args = {"path": fpath, "old_string": old_string, "new_string": "replacement"}
    with pytest.raises(ModelRetry) as exc:
        _run(cap.before_tool_execute(
            None, call=_edit_call(), tool_def=None, args=args,
        ))
    msg = str(exc.value)
    assert f"appears {match_count} times" in msg, (
        f"Expected 'appears {match_count} times' in message, got: {msg}"
    )
    assert fpath in msg
    assert "above AND below" in msg
    assert "disambiguate" in msg


def test_edit_file_guardrail_before_execute_multi_line_ambiguous(tmp_path):
    """Multi-line old_string that appears multiple times is rejected."""
    cap = EditFileGuardrailAsRetry()
    content = (
        "# header\n"
        "    def repeated(self):\n"
        "        pass\n"
        "# middle\n"
        "    def repeated(self):\n"
        "        pass\n"
        "# footer\n"
    )
    old_string = "    def repeated(self):\n        pass\n"
    fpath = _tmp_file(tmp_path, "multi_line.py", content)
    args = {"path": fpath, "old_string": old_string, "new_string": "    def new_method(self):\n        pass\n"}
    with pytest.raises(ModelRetry) as exc:
        _run(cap.before_tool_execute(
            None, call=_edit_call(), tool_def=None, args=args,
        ))
    msg = str(exc.value)
    assert "appears 2 times" in msg
    assert "above AND below" in msg


def test_edit_file_guardrail_before_execute_non_overlapping_count_only(tmp_path):
    """str.count does NOT count overlapping occurrences, so 'aaa'.count('aa') == 1.
    This edge case should still pass through (single match), not raise ModelRetry."""
    cap = EditFileGuardrailAsRetry()
    fpath = _tmp_file(tmp_path, "aaa.py", "aaa\n")
    args = {"path": fpath, "old_string": "aa", "new_string": "bb"}
    result = _run(cap.before_tool_execute(
        None, call=_edit_call(), tool_def=None, args=args,
    ))
    assert result is args