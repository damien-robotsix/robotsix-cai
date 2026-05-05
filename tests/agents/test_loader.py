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


    _get_arg,
    HistoryCompactorCapability,


    build_deep_agent,
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
    assert "Do NOT assume the edit succeeded" in msg
    assert "similar content" in msg
    assert "that content may be from a different" in msg
    assert "Re-read the file at the exact target location" in msg
    assert "read_file to confirm" in msg


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
    assert "Do NOT assume the edit succeeded" in msg
    assert "similar content" in msg
    assert "that content may be from a different" in msg
    assert "Re-read the file at the exact target location" in msg
    assert "read_file to confirm" in msg


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

    config = {"name": "test-agent", "model": "deepseek/deepseek-v4-pro"}
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


def _passthrough_handler(sentinel="__edit_called__"):
    """Return an async handler that records invocation and returns *sentinel*.

    Used to verify that wrap_tool_execute either calls the inner handler
    (and forwards its return value) or short-circuits without calling it.
    """
    calls: list = []

    async def handler(args):
        calls.append(args)
        return sentinel

    return handler, calls


def test_edit_file_guardrail_old_string_found_runs_handler(tmp_path):
    """old_string found in file → handler is called and its result returned."""
    cap = EditFileGuardrailAsRetry()
    fpath = _tmp_file(tmp_path, "a.py", "line1\nline2\nline3\n")
    args = {"path": fpath, "old_string": "line2", "new_string": "replacement"}
    handler, calls = _passthrough_handler()
    result = _run(cap.wrap_tool_execute(
        None, call=_edit_call(), tool_def=None, args=args, handler=handler,
    ))
    assert result == "__edit_called__"
    assert calls == [args]


def test_edit_file_guardrail_old_string_ambiguous_first_attempt_retries(tmp_path):
    """First failure: ambiguous old_string → ModelRetry with match count."""
    cap = EditFileGuardrailAsRetry()
    content = "line A\nduplicate line\nline B\nduplicate line\nline C\n"
    fpath = _tmp_file(tmp_path, "a.py", content)
    args = {"path": fpath, "old_string": "duplicate line\n", "new_string": "replacement"}
    handler, calls = _passthrough_handler()
    with pytest.raises(ModelRetry) as exc:
        _run(cap.wrap_tool_execute(
            None, call=_edit_call(), tool_def=None, args=args, handler=handler,
        ))
    msg = str(exc.value)
    assert "appears 2 times" in msg
    assert fpath in msg
    assert "above AND below" in msg
    assert "disambiguate" in msg
    assert calls == []


def test_edit_file_guardrail_old_string_unique_runs_handler(tmp_path):
    """Single-match old_string → handler runs (different shape than _found test)."""
    cap = EditFileGuardrailAsRetry()
    content = "header\nunique middle line\nfooter\nheader2\nunique middle other\nfooter2\n"
    fpath = _tmp_file(tmp_path, "b.py", content)
    args = {"path": fpath, "old_string": "unique middle line", "new_string": "replacement"}
    handler, calls = _passthrough_handler()
    result = _run(cap.wrap_tool_execute(
        None, call=_edit_call(), tool_def=None, args=args, handler=handler,
    ))
    assert result == "__edit_called__"
    assert calls == [args]


def test_edit_file_guardrail_old_string_not_found_first_attempt_retries(tmp_path):
    """First failure: old_string NOT in file → ModelRetry with path/diagnostic."""
    cap = EditFileGuardrailAsRetry()
    fpath = _tmp_file(tmp_path, "a.py", "line1\nline2\n")
    args = {"path": fpath, "old_string": "missing_line", "new_string": "replacement"}
    handler, calls = _passthrough_handler()
    with pytest.raises(ModelRetry) as exc:
        _run(cap.wrap_tool_execute(
            None, call=_edit_call(), tool_def=None, args=args, handler=handler,
        ))
    msg = str(exc.value)
    assert "old_string not found" in msg
    assert fpath in msg
    assert "read_file" in msg
    assert "Do not reconstruct from memory" in msg
    assert calls == []


def test_edit_file_guardrail_not_found_escalates_after_repeats(tmp_path):
    """2nd consecutive not-found failure → warning string with file content,
    no ModelRetry (so pydantic-ai's max_retries cap is not consumed)."""
    cap = EditFileGuardrailAsRetry()
    file_content = "actual line one\nactual line two\nactual line three\n"
    fpath = _tmp_file(tmp_path, "a.py", file_content)
    args = {"path": fpath, "old_string": "fake reconstruction", "new_string": "x"}
    handler, calls = _passthrough_handler()

    # 1st failure: standard ModelRetry
    with pytest.raises(ModelRetry):
        _run(cap.wrap_tool_execute(
            None, call=_edit_call(), tool_def=None, args=args, handler=handler,
        ))

    # 2nd failure on same path: warning string (no raise)
    result = _run(cap.wrap_tool_execute(
        None, call=_edit_call(), tool_def=None, args=args, handler=handler,
    ))
    assert isinstance(result, str)
    assert "consecutive" in result
    assert "old_string was not found" in result
    assert file_content in result  # actual file content embedded
    assert calls == []  # handler never invoked on either failure


def test_edit_file_guardrail_ambiguous_escalates_after_repeats(tmp_path):
    """2nd consecutive ambiguous failure → warning string with file content."""
    cap = EditFileGuardrailAsRetry()
    file_content = "X\nrepeat\nY\nrepeat\nZ\n"
    fpath = _tmp_file(tmp_path, "a.py", file_content)
    args = {"path": fpath, "old_string": "repeat\n", "new_string": "x"}
    handler, calls = _passthrough_handler()

    with pytest.raises(ModelRetry):
        _run(cap.wrap_tool_execute(
            None, call=_edit_call(), tool_def=None, args=args, handler=handler,
        ))
    result = _run(cap.wrap_tool_execute(
        None, call=_edit_call(), tool_def=None, args=args, handler=handler,
    ))
    assert isinstance(result, str)
    assert "consecutive" in result
    assert "matches 2 locations" in result
    assert file_content in result
    assert calls == []


def test_edit_file_guardrail_success_resets_failure_counter(tmp_path):
    """A successful edit on a path clears the failure counter so the next
    failure starts a fresh streak (raise ModelRetry, not warning string)."""
    cap = EditFileGuardrailAsRetry()
    file_content = "alpha\nbeta\ngamma\n"
    fpath = _tmp_file(tmp_path, "a.py", file_content)
    handler, _ = _passthrough_handler()

    # Failure 1 (raise)
    with pytest.raises(ModelRetry):
        _run(cap.wrap_tool_execute(
            None, call=_edit_call(), tool_def=None,
            args={"path": fpath, "old_string": "missing", "new_string": "x"},
            handler=handler,
        ))
    # Successful edit on the same path
    _run(cap.wrap_tool_execute(
        None, call=_edit_call(), tool_def=None,
        args={"path": fpath, "old_string": "beta", "new_string": "BETA"},
        handler=handler,
    ))
    # Next failure on the same path should raise ModelRetry again, not return warning
    with pytest.raises(ModelRetry):
        _run(cap.wrap_tool_execute(
            None, call=_edit_call(), tool_def=None,
            args={"path": fpath, "old_string": "still missing", "new_string": "x"},
            handler=handler,
        ))


def test_edit_file_guardrail_failure_counter_is_per_path(tmp_path):
    """Failures on path A do not escalate failures on path B."""
    cap = EditFileGuardrailAsRetry()
    fa = _tmp_file(tmp_path, "a.py", "content A\n")
    fb = _tmp_file(tmp_path, "b.py", "content B\n")
    handler, _ = _passthrough_handler()

    # Failure on path A
    with pytest.raises(ModelRetry):
        _run(cap.wrap_tool_execute(
            None, call=_edit_call(), tool_def=None,
            args={"path": fa, "old_string": "missing", "new_string": "x"},
            handler=handler,
        ))
    # First failure on path B should still be ModelRetry, not warning
    with pytest.raises(ModelRetry):
        _run(cap.wrap_tool_execute(
            None, call=_edit_call(), tool_def=None,
            args={"path": fb, "old_string": "also missing", "new_string": "x"},
            handler=handler,
        ))


def test_edit_file_guardrail_for_run_returns_fresh_state(tmp_path):
    """for_run returns a new instance with zeroed counter so concurrent runs
    don't share escalation state."""
    cap = EditFileGuardrailAsRetry()
    fpath = _tmp_file(tmp_path, "a.py", "content\n")
    handler, _ = _passthrough_handler()
    args = {"path": fpath, "old_string": "missing", "new_string": "x"}
    with pytest.raises(ModelRetry):
        _run(cap.wrap_tool_execute(
            None, call=_edit_call(), tool_def=None, args=args, handler=handler,
        ))

    fresh = _run(cap.for_run(None))
    assert fresh is not cap
    # Fresh instance: 1st failure must raise, not return warning
    with pytest.raises(ModelRetry):
        _run(fresh.wrap_tool_execute(
            None, call=_edit_call(), tool_def=None, args=args, handler=handler,
        ))


def test_edit_file_guardrail_preview_truncates_large_files(tmp_path):
    """File content >8000 chars is truncated in the preview to keep the
    warning readable in conversation history."""
    cap = EditFileGuardrailAsRetry()
    huge = "x" * 12000 + "\n"
    fpath = _tmp_file(tmp_path, "huge.py", huge)
    args = {"path": fpath, "old_string": "missing", "new_string": "x"}
    handler, _ = _passthrough_handler()
    with pytest.raises(ModelRetry):
        _run(cap.wrap_tool_execute(
            None, call=_edit_call(), tool_def=None, args=args, handler=handler,
        ))
    result = _run(cap.wrap_tool_execute(
        None, call=_edit_call(), tool_def=None, args=args, handler=handler,
    ))
    assert isinstance(result, str)
    assert "file truncated" in result
    assert "8000" in result
    assert "12001" in result  # total chars (12000 + newline)


def test_edit_file_guardrail_with_blank_lines_runs_handler(tmp_path):
    """old_string with exact blank-line count must match when file has them."""
    cap = EditFileGuardrailAsRetry()
    content = "def foo():\n    pass\n\n\ndef bar():\n    pass\n"
    fpath = _tmp_file(tmp_path, "a.py", content)
    args = {"path": fpath, "old_string": "    pass\n\n\ndef bar():", "new_string": "x"}
    handler, calls = _passthrough_handler()
    result = _run(cap.wrap_tool_execute(
        None, call=_edit_call(), tool_def=None, args=args, handler=handler,
    ))
    assert result == "__edit_called__"
    assert calls == [args]


def test_edit_file_guardrail_wrong_blank_line_count_first_attempt_retries(tmp_path):
    """One blank line instead of two → ModelRetry (doesn't match file content)."""
    cap = EditFileGuardrailAsRetry()
    content = "def foo():\n    pass\n\n\ndef bar():\n    pass\n"
    fpath = _tmp_file(tmp_path, "a.py", content)
    args = {"path": fpath, "old_string": "    pass\n\ndef bar():", "new_string": "x"}
    handler, _ = _passthrough_handler()
    with pytest.raises(ModelRetry) as exc:
        _run(cap.wrap_tool_execute(
            None, call=_edit_call(), tool_def=None, args=args, handler=handler,
        ))
    assert "old_string not found" in str(exc.value)


def test_edit_file_guardrail_non_edit_file_passthrough():
    """Non-edit_file tools pass through to the handler unchanged."""
    cap = EditFileGuardrailAsRetry()
    args = {"pattern": "something"}
    handler, calls = _passthrough_handler()
    result = _run(cap.wrap_tool_execute(
        None, call=_grep_call("grep"), tool_def=None, args=args, handler=handler,
    ))
    assert result == "__edit_called__"
    assert calls == [args]


def test_edit_file_guardrail_missing_old_string_passes_to_handler():
    """Missing old_string → handler runs (let the real tool handle it)."""
    cap = EditFileGuardrailAsRetry()
    args = {"path": "somefile.py", "new_string": "replacement"}
    handler, calls = _passthrough_handler()
    result = _run(cap.wrap_tool_execute(
        None, call=_edit_call(), tool_def=None, args=args, handler=handler,
    ))
    assert result == "__edit_called__"
    assert calls == [args]


def test_edit_file_guardrail_empty_old_string_passes_to_handler():
    """Empty old_string → handler runs (let the real tool handle it)."""
    cap = EditFileGuardrailAsRetry()
    args = {"path": "somefile.py", "old_string": "", "new_string": "replacement"}
    handler, calls = _passthrough_handler()
    result = _run(cap.wrap_tool_execute(
        None, call=_edit_call(), tool_def=None, args=args, handler=handler,
    ))
    assert result == "__edit_called__"
    assert calls == [args]


def test_edit_file_guardrail_missing_path_passes_to_handler():
    """Missing path arg → handler runs (let the real tool handle it)."""
    cap = EditFileGuardrailAsRetry()
    args = {"old_string": "something", "new_string": "replacement"}
    handler, calls = _passthrough_handler()
    result = _run(cap.wrap_tool_execute(
        None, call=_edit_call(), tool_def=None, args=args, handler=handler,
    ))
    assert result == "__edit_called__"
    assert calls == [args]


def test_edit_file_guardrail_file_not_found_passes_to_handler(tmp_path):
    """FileNotFoundError → handler runs (let the real tool handle it)."""
    cap = EditFileGuardrailAsRetry()
    args = {"path": str(tmp_path / "nonexistent.py"), "old_string": "x", "new_string": "y"}
    handler, calls = _passthrough_handler()
    result = _run(cap.wrap_tool_execute(
        None, call=_edit_call(), tool_def=None, args=args, handler=handler,
    ))
    assert result == "__edit_called__"
    assert calls == [args]


def test_edit_file_guardrail_object_args(tmp_path):
    """Object-style args (not dict) should work for extraction."""
    cap = EditFileGuardrailAsRetry()
    fpath = _tmp_file(tmp_path, "a.py", "hello world\n")
    args = SimpleNamespace(path=fpath, old_string="hello world", new_string="hi")
    handler, calls = _passthrough_handler()
    result = _run(cap.wrap_tool_execute(
        None, call=_edit_call(), tool_def=None, args=args, handler=handler,
    ))
    assert result == "__edit_called__"
    assert calls == [args]


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


def test_parse_agent_md_valid(monkeypatch, tmp_path):
    monkeypatch.setattr("cai.agents.loader.AGENT_DIR", tmp_path)
    md_path = tmp_path / "test_agent.md"
    md_path.write_text(
        "---\n"
        "name: test-agent\n"
        "model: deepseek/deepseek-v4-pro\n"
        "---\n"
        "## System prompt body\n\n"
        "This is the system prompt.\n"
        "It spans multiple lines.\n"
    )
    config, system_prompt = parse_agent_md(str(md_path))
    assert config["name"] == "test-agent"
    assert config["model"] == "deepseek/deepseek-v4-pro"
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
        "model: deepseek/deepseek-v4-pro\n"
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
        'model: deepseek/deepseek-v4-pro\n'
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
        'model: deepseek/deepseek-v4-pro\n'
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
# Backslash / repr guidance in agent system prompts
# ---------------------------------------------------------------------------

BACKSLASH_REPR_GUIDANCE_TEXT = (
    "**Backslash escapes in `old_string`:** When `old_string` contains "
    "regex patterns with backslash escapes"
)

AGENTS_WITH_BACKSLASH_REPR_GUIDANCE = [
    "implement",
    "docs",
]


@pytest.mark.parametrize("agent_name", AGENTS_WITH_BACKSLASH_REPR_GUIDANCE)
def test_agent_prompt_includes_backslash_repr_guidance(agent_name):
    """Ensure implement and docs agent system prompts include the backslash
    escape / repr() diagnostic guidance bullet."""
    path = resolve_agent_path(agent_name)
    _, system_prompt = parse_agent_md(path)
    assert BACKSLASH_REPR_GUIDANCE_TEXT in system_prompt, (
        f"Agent '{agent_name}' system prompt missing backslash/repr guidance.\n"
        f"Expected text: {BACKSLASH_REPR_GUIDANCE_TEXT!r}"
    )


# ---------------------------------------------------------------------------
# Avoid-re-reading guidance in agent system prompts
# ---------------------------------------------------------------------------

AVOID_REREADING_GUIDANCE_TEXTS: dict[str, str] = {
    "implement": "**Check conversation history before re-reading:**",
    "python_review": "**Avoid re-reading:**",
    "test_writer": "**Avoid re-reading:**",
    "refine": "**Avoid re-reading files you've already read.**",
}


@pytest.mark.parametrize(
    "agent_name,expected_text",
    [(name, text) for name, text in AVOID_REREADING_GUIDANCE_TEXTS.items()],
)
def test_agent_prompt_includes_avoid_rereading_guidance(
    agent_name: str, expected_text: str
):
    """Ensure each agent's system prompt contains avoid-re-reading guidance."""
    path = resolve_agent_path(agent_name)
    _, system_prompt = parse_agent_md(path)
    assert expected_text in system_prompt, (
        f"Agent '{agent_name}' system prompt missing avoid-re-reading guidance.\n"
        f"Expected text: {expected_text!r}"
    )


# ---------------------------------------------------------------------------
# Anti-hallucination guard in agent system prompts
# ---------------------------------------------------------------------------

ANTI_HALLUCINATION_TEXT = (
    "> **You do NOT have an `execute`, `bash`, `shell`, or `run` tool. "
    "You cannot run commands, tests, or scripts. "
    "Only the tools listed above are available to you.**\n"
    ">\n"
    "> **Parameter bleed warning:** Each tool accepts only its own documented "
    "parameters. Do not carry a parameter from one tool (e.g., `limit` from "
    "`read_file`) to another tool (e.g., `grep`). If a parameter isn't listed "
    "in the tool's documentation, it won't be accepted."
)


AGENTS_WITH_ANTI_HALLUCINATION = [
    "docs",
    "explore",
    "github_workflow_review",
    "implement",
    "parent_verifier",
    "pydantic_ai_review",
    "python_review",
    "refine",
    "test_writer",
    "trace_analyst",
]


@pytest.mark.parametrize("agent_name", AGENTS_WITH_ANTI_HALLUCINATION)
def test_agent_prompt_includes_anti_hallucination_guard(agent_name):
    """Each agent that lacks an execute tool must declare
    anti_hallucination_guard in its common: list."""
    path = resolve_agent_path(agent_name)
    config, _ = parse_agent_md(path)
    common = config.get("common", [])
    assert "anti_hallucination_guard" in common, (
        f"Agent '{agent_name}' missing 'anti_hallucination_guard' in common: list. "
        f"Got: {common}"
    )


@pytest.mark.parametrize("agent_name", AGENTS_WITH_ANTI_HALLUCINATION)
def test_anti_hallucination_guard_positioned_after_agent_header(agent_name, monkeypatch):
    """The anti-hallucination guard is injected after the title heading
    via the common: mechanism and does not remain in the raw body."""
    import cai.agents.loader as loader

    path = resolve_agent_path(agent_name)
    config, body = parse_agent_md(path)

    # After migration, the guard text must NOT be in the raw body.
    assert ANTI_HALLUCINATION_TEXT not in body, (
        f"Agent '{agent_name}' still has inlined anti-hallucination guard "
        f"in the raw body."
    )

    # The config must declare anti_hallucination_guard in common:.
    common = config.get("common", [])
    assert "anti_hallucination_guard" in common, (
        f"Agent '{agent_name}' missing 'anti_hallucination_guard' in common: list."
    )

    # Verify the merged build_deep_agent output includes the guard.
    fake_agent = object()
    monkeypatch.setattr(loader, "build_model", lambda c: None)
    monkeypatch.setattr(loader, "build_model_settings", lambda c: None)
    monkeypatch.setattr(loader, "build_deep_agent_kwargs", lambda c: {})
    monkeypatch.setattr(loader, "_prune_toolsets", lambda a, r: None)
    monkeypatch.setattr("cai.agents.loader._resolve_subagents", lambda c: [])

    captured_instructions = []

    def fake_create(*args, **kwargs):
        captured_instructions.append(kwargs.get("instructions", ""))
        return fake_agent

    monkeypatch.setattr("pydantic_deep.create_deep_agent", fake_create)

    loader.build_deep_agent(config, body)
    assert captured_instructions, "build_deep_agent did not call create_deep_agent"
    merged = captured_instructions[0]

    assert ANTI_HALLUCINATION_TEXT in merged, (
        f"Agent '{agent_name}' merged instructions missing anti-hallucination guard."
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
    "github_workflow_review",
    "implement",
    "docs",
    "pydantic_ai_review",
    "test_writer",
    "refine",
    "trace_analyst",
    "parent_verifier",
]


@pytest.mark.parametrize("agent_name", AGENTS_WITH_ANTIPATTERN_EXAMPLES)
def test_agent_prompt_includes_antipattern_examples(agent_name):
    """Each agent that needs anti-pattern examples must declare
    antipattern_examples in its common: list."""
    path = resolve_agent_path(agent_name)
    config, _ = parse_agent_md(path)
    common = config.get("common", [])
    assert "antipattern_examples" in common, (
        f"Agent '{agent_name}' missing 'antipattern_examples' in common: list. "
        f"Got: {common}"
    )


@pytest.mark.parametrize("agent_name", AGENTS_WITH_ANTIPATTERN_EXAMPLES)
def test_antipattern_examples_positioned_after_anti_hallucination_guard(
    agent_name, monkeypatch
):
    """Anti-pattern examples appear after the anti-hallucination blockquote
    in the merged build_deep_agent output."""
    import cai.agents.loader as loader

    path = resolve_agent_path(agent_name)
    config, body = parse_agent_md(path)
    common = config.get("common", [])
    assert "antipattern_examples" in common, (
        f"Agent '{agent_name}' missing 'antipattern_examples' in common: list."
    )
    # Antipattern examples are no longer inlined — verify via merged output.
    assert ANTIPATTERN_EXAMPLES_TEXT not in body, (
        f"Agent '{agent_name}' still has inlined antipattern examples "
        f"in body — should come via common:."
    )
    fake_agent = object()
    captured_instructions = []

    def fake_create(*args, **kwargs):
        captured_instructions.append(kwargs.get("instructions", ""))
        return fake_agent

    for k in ("model", "model_settings", "tools", "output_type", "deps_type"):
        config.pop(k, None)
    monkeypatch.setattr(loader, "build_model", lambda c: None)
    monkeypatch.setattr(loader, "build_deep_agent_kwargs", lambda c: {})
    monkeypatch.setattr(loader, "_prune_toolsets", lambda a, r: None)
    monkeypatch.setattr("cai.agents.loader._resolve_subagents", lambda c: [])
    monkeypatch.setattr("pydantic_deep.create_deep_agent", fake_create)
    loader.build_deep_agent(config, body)
    assert captured_instructions, "build_deep_agent did not call create_deep_agent"
    merged = captured_instructions[0]
    guard_idx = merged.index(ANTI_HALLUCINATION_TEXT)
    antipattern_idx = merged.index(ANTIPATTERN_EXAMPLES_TEXT)
    assert antipattern_idx > guard_idx, (
        f"Anti-pattern examples must come after the anti-hallucination "
        f"blockquote in {agent_name!r} merged prompt."
    )


def test_antipattern_examples_absent_from_explore():
    """Explore agent should NOT declare antipattern_examples in its
    common: list since it has no anti-pattern examples."""
    path = resolve_agent_path("explore")
    config, body = parse_agent_md(path)
    common = config.get("common", [])
    assert "antipattern_examples" not in common, (
        f"Explore agent should not have 'antipattern_examples' in common: list. "
        f"Got: {common}"
    )
    # Also verify it's not inlined in the raw body.
    assert ANTIPATTERN_EXAMPLES_TEXT not in body, (
        "Anti-pattern examples found unexpectedly in explore agent prompt."
    )


# ---------------------------------------------------------------------------
# raise_ticket tool in agent frontmatter
# ---------------------------------------------------------------------------

PRO_MODEL_AGENTS_WITH_RAISE_TICKET = [
    "spike",
    "duplication_auditor",
    "audit",
    "implement",
    "architecture_auditor",
    "refine",
    "deps_auditor",
    "security_auditor",
    "sourcing",
    "explore",
]

# Pro-model agents that intentionally do NOT have raise_ticket: they are
# reviewers that fix in place or return structured output, and have a
# history of filing spurious "no issues found" / "no docs changes
# needed" reports when given the tool (#1740, #1763).
REVIEW_AGENTS_NO_RAISE_TICKET = [
    "docs",
    "python_review",
]

FLASH_MODEL_AGENTS = [
    "trace_analyst",
    "issue_deduplicator",
    "merge_evaluator",
    "resolve_step",
    "memory_audit",
    "test_writer",
    "parent_verifier",
]


@pytest.mark.parametrize("agent_name", PRO_MODEL_AGENTS_WITH_RAISE_TICKET)
def test_pro_model_agent_has_raise_ticket_tool(agent_name: str):
    """Every pro-model agent that's expected to file blockers as tickets
    should list ``raise_ticket`` in its YAML frontmatter ``tools`` list."""
    path = resolve_agent_path(agent_name)
    config, _ = parse_agent_md(path)
    tools = config.get("tools", [])
    assert "raise_ticket" in tools, (
        f"{agent_name}.md is a pro-model blocker-filing agent but its "
        f"frontmatter tools list does not include 'raise_ticket'. "
        f"Found: {tools}"
    )
    # The legacy tool name must not linger.
    assert "raise_issue" not in tools, (
        f"{agent_name}.md still references the legacy 'raise_issue' tool. "
        f"Replace with 'raise_ticket'."
    )


@pytest.mark.parametrize("agent_name", REVIEW_AGENTS_NO_RAISE_TICKET)
def test_review_agent_does_not_have_raise_ticket(agent_name: str):
    """Review-style agents (docs, python_review) must not have
    raise_ticket: their job is to fix in place or return structured
    output, not to file tickets — empirically they file spurious
    'no issues found' reports when given the tool."""
    path = resolve_agent_path(agent_name)
    config, _ = parse_agent_md(path)
    tools = config.get("tools", [])
    assert "raise_ticket" not in tools, (
        f"{agent_name}.md is a review agent and must not have "
        f"'raise_ticket' in its tools list: {tools}"
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
        "ci_triage",
    ],
)
def test_agent_prompt_includes_task_tool_parameter_note(agent_name: str, monkeypatch):
    """Every agent that has subagents in tools: must have the task-tool
    parameter note in the merged build_deep_agent output (auto-inclusion
    based on subagents in tools:)."""
    import cai.agents.loader as loader

    path = resolve_agent_path(agent_name)
    config, body = parse_agent_md(path)

    # The config must have subagents in tools: for auto-inclusion.
    tools = config.get("tools", [])
    assert "subagents" in tools, (
        f"Agent '{agent_name}' expected to have 'subagents' in tools: "
        f"for task-tool-note auto-inclusion."
    )

    # Verify the merged build_deep_agent output includes the note.
    fake_agent = object()
    monkeypatch.setattr(loader, "build_model", lambda c: None)
    monkeypatch.setattr(loader, "build_model_settings", lambda c: None)
    monkeypatch.setattr(loader, "build_deep_agent_kwargs", lambda c: {})
    monkeypatch.setattr(loader, "_prune_toolsets", lambda a, r: None)
    monkeypatch.setattr("cai.agents.loader._resolve_subagents", lambda c: [])

    captured_instructions = []

    def fake_create(*args, **kwargs):
        captured_instructions.append(kwargs.get("instructions", ""))
        return fake_agent

    monkeypatch.setattr("pydantic_deep.create_deep_agent", fake_create)

    loader.build_deep_agent(config, body)
    assert captured_instructions, "build_deep_agent did not call create_deep_agent"
    merged = captured_instructions[0]

    assert _TASK_TOOL_PARAM_TEXT in merged, (
        f"{agent_name}.md merged instructions missing the task-tool parameter-name note. "
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
    assert "Warning: read_file" in result
    assert "is covered by a prior read_file" in result
    assert "file content has not changed" in result
    assert "DO NOT re-read this range" in result


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
    assert "Warning: read_file" in result
    assert "is covered by a prior read_file" in result
    assert "file content has not changed" in result
    assert "DO NOT re-read this range" in result


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


# ---------------------------------------------------------------------------
# HistoryCompactorCapability — overlap-aware short-circuit
# ---------------------------------------------------------------------------


def test_history_compactor_wrap_tool_execute_overlapping_subset():
    """Prior read offset=0 limit=500 fully contains new offset=200 limit=100 →
    short-circuit returns warning, handler not called."""
    cap = HistoryCompactorCapability()
    handler_called = False

    async def handler(args):
        nonlocal handler_called
        handler_called = True
        return "file content"

    prior_tc = ToolCallPart(tool_name="read_file", args={"path": "x.py", "offset": 0, "limit": 500}, tool_call_id="c1")
    prior_tr = ToolReturnPart(tool_name="read_file", content="big chunk", tool_call_id="c1")

    ctx = _make_ctx(
        messages=[
            ModelResponse(parts=[prior_tc]),
            ModelRequest(parts=[prior_tr]),
        ],
    )

    call = ToolCallPart(tool_name="read_file", args={"path": "x.py", "offset": 200, "limit": 100}, tool_call_id="c2")

    result = _run(cap.wrap_tool_execute(ctx, call=call, tool_def=None, args={}, handler=handler))

    assert not handler_called
    assert "Warning: read_file" in result
    assert "is covered by a prior read_file" in result
    assert "offset=200, limit=100" in result
    assert "offset=0, limit=500" in result


def test_history_compactor_wrap_tool_execute_disjoint_with_gap():
    """Prior offset=0 limit=200, new offset=300 limit=200 → disjoint ranges
    (gap at 200-299), handler called."""
    cap = HistoryCompactorCapability()
    handler_called = False

    async def handler(args):
        nonlocal handler_called
        handler_called = True
        return "page 2"

    prior_tc = ToolCallPart(tool_name="read_file", args={"path": "x.py", "offset": 0, "limit": 200}, tool_call_id="c1")
    prior_tr = ToolReturnPart(tool_name="read_file", content="page 1", tool_call_id="c1")

    ctx = _make_ctx(
        messages=[
            ModelResponse(parts=[prior_tc]),
            ModelRequest(parts=[prior_tr]),
        ],
    )

    call = ToolCallPart(tool_name="read_file", args={"path": "x.py", "offset": 300, "limit": 200}, tool_call_id="c2")

    result = _run(cap.wrap_tool_execute(ctx, call=call, tool_def=None, args={}, handler=handler))

    assert handler_called
    assert result == "page 2"


def test_history_compactor_wrap_tool_execute_partial_overlap_passes_through():
    """Prior offset=0 limit=200, new offset=100 limit=300 → new extends beyond
    prior, handler called."""
    cap = HistoryCompactorCapability()
    handler_called = False

    async def handler(args):
        nonlocal handler_called
        handler_called = True
        return "extended read"

    prior_tc = ToolCallPart(tool_name="read_file", args={"path": "x.py", "offset": 0, "limit": 200}, tool_call_id="c1")
    prior_tr = ToolReturnPart(tool_name="read_file", content="first 200", tool_call_id="c1")

    ctx = _make_ctx(
        messages=[
            ModelResponse(parts=[prior_tc]),
            ModelRequest(parts=[prior_tr]),
        ],
    )

    call = ToolCallPart(tool_name="read_file", args={"path": "x.py", "offset": 100, "limit": 300}, tool_call_id="c2")

    result = _run(cap.wrap_tool_execute(ctx, call=call, tool_def=None, args={}, handler=handler))

    assert handler_called
    assert result == "extended read"


def test_history_compactor_wrap_tool_execute_prior_no_limit_contains_any():
    """Prior read with no offset/limit (whole-file) does NOT block a bounded
    same-file read — only unbounded re-reads are blocked."""
    cap = HistoryCompactorCapability()
    handler_called = False

    async def handler(args):
        nonlocal handler_called
        handler_called = True
        return "file content"

    prior_tc = ToolCallPart(tool_name="read_file", args={"path": "x.py"}, tool_call_id="c1")
    prior_tr = ToolReturnPart(tool_name="read_file", content="whole file", tool_call_id="c1")

    ctx = _make_ctx(
        messages=[
            ModelResponse(parts=[prior_tc]),
            ModelRequest(parts=[prior_tr]),
        ],
    )

    call = ToolCallPart(tool_name="read_file", args={"path": "x.py", "offset": 100, "limit": 50}, tool_call_id="c2")

    result = _run(cap.wrap_tool_execute(ctx, call=call, tool_def=None, args={}, handler=handler))

    assert handler_called
    assert result == "file content"


def test_history_compactor_wrap_tool_execute_prior_no_limit_blocks_unbounded_re_read():
    """Prior whole-file read (no limit) still blocks a subsequent unbounded
    re-read (also no limit) on the same file."""
    cap = HistoryCompactorCapability()
    handler_called = False

    async def handler(args):
        nonlocal handler_called
        handler_called = True
        return "file content"

    prior_tc = ToolCallPart(tool_name="read_file", args={"path": "x.py"}, tool_call_id="c1")
    prior_tr = ToolReturnPart(tool_name="read_file", content="whole file", tool_call_id="c1")

    ctx = _make_ctx(
        messages=[
            ModelResponse(parts=[prior_tc]),
            ModelRequest(parts=[prior_tr]),
        ],
    )

    call = ToolCallPart(tool_name="read_file", args={"path": "x.py"}, tool_call_id="c2")

    result = _run(cap.wrap_tool_execute(ctx, call=call, tool_def=None, args={}, handler=handler))

    assert not handler_called
    assert "Warning: read_file" in result
    assert "covered by a prior read_file" in result


def test_history_compactor_wrap_tool_execute_current_no_limit_not_short_circuited():
    """Prior has offset=0 limit=200, new read has no limit → can't confirm EOF
    coverage, handler called."""
    cap = HistoryCompactorCapability()
    handler_called = False

    async def handler(args):
        nonlocal handler_called
        handler_called = True
        return "full re-read"

    prior_tc = ToolCallPart(tool_name="read_file", args={"path": "x.py", "offset": 0, "limit": 200}, tool_call_id="c1")
    prior_tr = ToolReturnPart(tool_name="read_file", content="first 200 lines", tool_call_id="c1")

    ctx = _make_ctx(
        messages=[
            ModelResponse(parts=[prior_tc]),
            ModelRequest(parts=[prior_tr]),
        ],
    )

    call = ToolCallPart(tool_name="read_file", args={"path": "x.py"}, tool_call_id="c2")

    result = _run(cap.wrap_tool_execute(ctx, call=call, tool_def=None, args={}, handler=handler))

    assert handler_called
    assert result == "full re-read"


def test_history_compactor_wrap_tool_execute_overlap_blocked_by_intervening_edit():
    """Prior offset=0 limit=500 contains new offset=100 limit=50, but an
    intervening write_file forces a re-read."""
    cap = HistoryCompactorCapability()
    handler_called = False

    async def handler(args):
        nonlocal handler_called
        handler_called = True
        return "re-read after edit"

    prior_tc = ToolCallPart(tool_name="read_file", args={"path": "x.py", "offset": 0, "limit": 500}, tool_call_id="c1")
    prior_tr = ToolReturnPart(tool_name="read_file", content="big chunk", tool_call_id="c1")
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

    call = ToolCallPart(tool_name="read_file", args={"path": "x.py", "offset": 100, "limit": 50}, tool_call_id="c3")

    result = _run(cap.wrap_tool_execute(ctx, call=call, tool_def=None, args={}, handler=handler))

    assert handler_called
    assert result == "re-read after edit"


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

    config = {"name": "test-agent", "model": "deepseek/deepseek-v4-pro"}
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
    for r in (r_a, r_b, r_c):
        assert "Warning: read_file" in r
        assert "is covered by a prior read_file" in r
        assert "file content has not changed" in r
        assert "DO NOT re-read this range" in r


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
    assert "Warning: read_file" in result
    assert "is covered by a prior read_file" in result
    assert "'orphan.py'" in result
    assert "file content has not changed" in result


def test_history_compactor_wrap_tool_execute_prior_offset_no_limit_covers_subset():
    """Prior read with offset=100 and no limit (reads to EOF) does NOT block a
    bounded read at offset=200 limit=100 — only unbounded re-reads are blocked."""
    cap = HistoryCompactorCapability()
    handler_called = False

    async def handler(args):
        nonlocal handler_called
        handler_called = True
        return "file content"

    prior_tc = ToolCallPart(tool_name="read_file", args={"path": "x.py", "offset": 100}, tool_call_id="c1")
    prior_tr = ToolReturnPart(tool_name="read_file", content="from offset 100 to EOF", tool_call_id="c1")

    ctx = _make_ctx(
        messages=[
            ModelResponse(parts=[prior_tc]),
            ModelRequest(parts=[prior_tr]),
        ],
    )

    call = ToolCallPart(tool_name="read_file", args={"path": "x.py", "offset": 200, "limit": 100}, tool_call_id="c2")

    result = _run(cap.wrap_tool_execute(ctx, call=call, tool_def=None, args={}, handler=handler))

    assert handler_called
    assert result == "file content"


def test_history_compactor_wrap_tool_execute_prior_offset_no_limit_not_covering_start():
    """Prior read from offset=100 to EOF does NOT contain current read starting
    at offset=50, because the prior doesn't cover lines 0-49 → handler called."""
    cap = HistoryCompactorCapability()
    handler_called = False

    async def handler(args):
        nonlocal handler_called
        handler_called = True
        return "full file content"

    prior_tc = ToolCallPart(tool_name="read_file", args={"path": "x.py", "offset": 100}, tool_call_id="c1")
    prior_tr = ToolReturnPart(tool_name="read_file", content="from offset 100 to EOF", tool_call_id="c1")

    ctx = _make_ctx(
        messages=[
            ModelResponse(parts=[prior_tc]),
            ModelRequest(parts=[prior_tr]),
        ],
    )

    call = ToolCallPart(tool_name="read_file", args={"path": "x.py", "offset": 50, "limit": 200}, tool_call_id="c2")

    result = _run(cap.wrap_tool_execute(ctx, call=call, tool_def=None, args={}, handler=handler))

    assert handler_called
    assert result == "full file content"


def test_history_compactor_wrap_tool_execute_prior_offset_nonzero_fully_contains_current():
    """Prior offset=200 limit=500 (end=700) fully contains current offset=300
    limit=100 (end=400) → short-circuit returns overlap warning with non-zero
    prior offset and both explicit limits."""
    cap = HistoryCompactorCapability()
    handler_called = False

    async def handler(args):
        nonlocal handler_called
        handler_called = True
        return "file content"

    prior_tc = ToolCallPart(
        tool_name="read_file",
        args={"path": "x.py", "offset": 200, "limit": 500},
        tool_call_id="c1",
    )
    prior_tr = ToolReturnPart(tool_name="read_file", content="big middle chunk", tool_call_id="c1")

    ctx = _make_ctx(
        messages=[
            ModelResponse(parts=[prior_tc]),
            ModelRequest(parts=[prior_tr]),
        ],
    )

    call = ToolCallPart(
        tool_name="read_file",
        args={"path": "x.py", "offset": 300, "limit": 100},
        tool_call_id="c2",
    )

    result = _run(cap.wrap_tool_execute(ctx, call=call, tool_def=None, args={}, handler=handler))

    assert not handler_called
    assert "Warning: read_file" in result
    assert "is covered by a prior read_file" in result
    assert "offset=300, limit=100" in result
    assert "offset=200, limit=500" in result


def test_history_compactor_for_run_returns_fresh_instance():
    """for_run returns a new HistoryCompactorCapability instance so
    concurrent runs of an lru_cache'd agent don't share state."""
    cap = HistoryCompactorCapability()
    fresh = _run(cap.for_run(None))
    assert fresh is not cap
    assert isinstance(fresh, HistoryCompactorCapability)


# ---------------------------------------------------------------------------
# HistoryCompactorCapability — grep short-circuit


def test_history_compactor_grep_short_circuit_on_full_read():
    """grep on a file that was fully read earlier (no limit) with no
    intervening edit returns a warning instead of executing."""
    cap = HistoryCompactorCapability()
    handler_called = False

    async def handler(args):
        nonlocal handler_called
        handler_called = True
        return "grep results"

    prior_tc = ToolCallPart(tool_name="read_file", args={"path": "x.py"}, tool_call_id="c1")
    prior_tr = ToolReturnPart(tool_name="read_file", content="full file content here", tool_call_id="c1")

    ctx = _make_ctx(
        messages=[
            ModelResponse(parts=[prior_tc]),
            ModelRequest(parts=[prior_tr]),
        ],
    )

    call = ToolCallPart(tool_name="grep", args={"path": "x.py", "pattern": "content"}, tool_call_id="c2")

    result = _run(cap.wrap_tool_execute(ctx, call=call, tool_def=None, args={"path": "x.py", "pattern": "content"}, handler=handler))

    assert not handler_called
    assert "Warning: grep" in result
    assert "x.py" in result
    assert "already read in full" in result
    assert "message index 0" in result
    assert "mentally" in result


def test_history_compactor_grep_on_partial_read_passthrough():
    """grep on a file that was only partially read (had a limit) executes
    normally since the full content is not in history."""
    cap = HistoryCompactorCapability()
    handler_called = False

    async def handler(args):
        nonlocal handler_called
        handler_called = True
        return "grep results"

    prior_tc = ToolCallPart(tool_name="read_file", args={"path": "x.py", "limit": 100}, tool_call_id="c1")
    prior_tr = ToolReturnPart(tool_name="read_file", content="first 100 lines only", tool_call_id="c1")

    ctx = _make_ctx(
        messages=[
            ModelResponse(parts=[prior_tc]),
            ModelRequest(parts=[prior_tr]),
        ],
    )

    call = ToolCallPart(tool_name="grep", args={"path": "x.py", "pattern": "content"}, tool_call_id="c2")

    result = _run(cap.wrap_tool_execute(ctx, call=call, tool_def=None, args={"path": "x.py", "pattern": "content"}, handler=handler))

    assert handler_called
    assert result == "grep results"


def test_history_compactor_grep_with_intervening_edit_passthrough():
    """grep on a file that was fully read but an edit_file happened since
    executes normally because the content may have changed."""
    cap = HistoryCompactorCapability()
    handler_called = False

    async def handler(args):
        nonlocal handler_called
        handler_called = True
        return "grep results after edit"

    prior_tc = ToolCallPart(tool_name="read_file", args={"path": "x.py"}, tool_call_id="c1")
    prior_tr = ToolReturnPart(tool_name="read_file", content="old content", tool_call_id="c1")
    edit_tc = ToolCallPart(tool_name="edit_file", args={"path": "x.py", "old_string": "old", "new_string": "new"}, tool_call_id="c2")
    edit_tr = ToolReturnPart(tool_name="edit_file", content="Edited file", tool_call_id="c2")

    ctx = _make_ctx(
        messages=[
            ModelResponse(parts=[prior_tc]),
            ModelRequest(parts=[prior_tr]),
            ModelResponse(parts=[edit_tc]),
            ModelRequest(parts=[edit_tr]),
        ],
    )

    call = ToolCallPart(tool_name="grep", args={"path": "x.py", "pattern": "content"}, tool_call_id="c3")

    result = _run(cap.wrap_tool_execute(ctx, call=call, tool_def=None, args={"path": "x.py", "pattern": "content"}, handler=handler))

    assert handler_called
    assert result == "grep results after edit"


def test_history_compactor_grep_no_path_passthrough():
    """grep with no path arg (glob_pattern only) executes normally."""
    cap = HistoryCompactorCapability()
    handler_called = False

    async def handler(args):
        nonlocal handler_called
        handler_called = True
        return "glob-only grep results"

    ctx = _make_ctx(messages=[])
    call = ToolCallPart(tool_name="grep", args={"pattern": "def test", "glob_pattern": "*.py"}, tool_call_id="c1")

    result = _run(cap.wrap_tool_execute(ctx, call=call, tool_def=None, args={"pattern": "def test", "glob_pattern": "*.py"}, handler=handler))

    assert handler_called
    assert result == "glob-only grep results"


def test_history_compactor_grep_empty_path_passthrough():
    """grep with empty string path executes normally."""
    cap = HistoryCompactorCapability()
    handler_called = False

    async def handler(args):
        nonlocal handler_called
        handler_called = True
        return "empty path grep results"

    ctx = _make_ctx(messages=[])
    call = ToolCallPart(tool_name="grep", args={"path": "", "pattern": "def"}, tool_call_id="c1")

    result = _run(cap.wrap_tool_execute(ctx, call=call, tool_def=None, args={"path": "", "pattern": "def"}, handler=handler))

    assert handler_called
    assert result == "empty path grep results"


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

    config = {"name": "test-agent", "model": "deepseek/deepseek-v4-pro"}
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


@pytest.mark.parametrize(
    "banned_tool",
    ["execute", "bash", "shell", "run"],
)
def _micro_read_call(tool_name="read_file"):
    return SimpleNamespace(tool_name=tool_name)


def test_inject_common_fragments_no_common_key():
    """No ``common`` key in config — anti_hallucination_guard still auto-injects."""
    from cai.agents.loader import _inject_common_fragments

    instructions = "# Test Agent\n\nSome body text."
    config = {"name": "test"}
    result = _inject_common_fragments(config, instructions)
    # anti_hallucination_guard is auto-included when no execute/bash/shell/run tools
    assert "You do NOT have an `execute`" in result
    assert "Some body text." in result


def test_inject_common_fragments_empty_common_list():
    """Empty ``common`` list — anti_hallucination_guard still auto-injects."""
    from cai.agents.loader import _inject_common_fragments

    instructions = "# Test Agent\n\nSome body text."
    config = {"name": "test", "common": []}
    result = _inject_common_fragments(config, instructions)
    # anti_hallucination_guard is auto-included when no execute/bash/shell/run tools
    assert "You do NOT have an `execute`" in result
    assert "Some body text." in result


def test_inject_common_fragments_unknown_name():
    """Unknown fragment names raise ValueError."""
    from cai.agents.loader import _inject_common_fragments
    import pytest

    instructions = "# Test Agent\n\nSome body text."
    config = {"name": "test", "common": ["nonexistent_fragment"]}
    with pytest.raises(ValueError, match="unknown common fragment"):
        _inject_common_fragments(config, instructions)


def test_inject_common_fragments_anti_hallucination_only():
    """Anti-hallucination guard is injected after the title heading."""
    from cai.agents.loader import _inject_common_fragments

    instructions = "# Test Agent\n\nSome body text."
    config = {"name": "test", "common": ["anti_hallucination_guard"]}
    result = _inject_common_fragments(config, instructions)

    assert "anti_hallucination_guard" not in result  # not the key name
    assert "You do NOT have an `execute`, `bash`, `shell`, or `run` tool." in result
    assert "Parameter bleed warning" in result
    assert "# Test Agent" in result
    # Fragment should be after the title heading
    parts = result.split("# Test Agent")
    assert len(parts) == 2
    assert "execute" in parts[1]


def test_inject_common_fragments_both_fragments():
    """Both common fragments are injected in order after the title."""
    from cai.agents.loader import _inject_common_fragments

    instructions = "# Test Agent\n\nSome body text."
    config = {"name": "test", "common": ["anti_hallucination_guard", "antipattern_examples"]}
    result = _inject_common_fragments(config, instructions)

    assert "run` tool" in result
    assert "Parameter bleed warning" in result
    assert "Anti-pattern examples" in result
    # anti-hallucination guard should come first, then antipattern examples
    assert result.index("run` tool") < result.index("Anti-pattern")


def test_inject_common_fragments_no_heading():
    """When no ``# Title`` heading exists, fragments are prepended."""
    from cai.agents.loader import _inject_common_fragments

    instructions = "Some body text without a title heading."
    config = {"name": "test", "common": ["anti_hallucination_guard"]}
    result = _inject_common_fragments(config, instructions)

    assert "You do NOT have an `execute`" in result
    assert "Parameter bleed warning" in result
    # Original text should still be present
    assert "without a title heading" in result


def test_inject_common_fragments_task_tool_note():
    """Task-tool-note is auto-injected when subagents is in tools."""
    from cai.agents.loader import _inject_common_fragments

    instructions = "# Test Agent\n\nBody."
    config = {"name": "test", "tools": ["subagents", "filesystem"], "common": []}
    result = _inject_common_fragments(config, instructions)

    assert "description=" in result
    assert "`task` tool has no `prompt` parameter" in result


def test_inject_common_fragments_no_task_tool_note_without_subagents():
    """Task-tool-note is NOT injected when subagents is not in tools."""
    from cai.agents.loader import _inject_common_fragments

    instructions = "# Test Agent\n\nBody."
    config = {"name": "test", "tools": ["filesystem"], "common": []}
    result = _inject_common_fragments(config, instructions)

    assert "description=" not in result


def test_inject_common_fragments_fragments_and_task_tool_note():
    """Common fragments and task tool note are both injected when applicable."""
    from cai.agents.loader import _inject_common_fragments

    instructions = "# Test Agent\n\nBody."
    config = {
        "name": "test",
        "tools": ["subagents"],
        "common": ["anti_hallucination_guard", "antipattern_examples"],
    }
    result = _inject_common_fragments(config, instructions)

    assert "execute" in result
    assert "Parameter bleed warning" in result
    assert "Anti-pattern examples" in result
    assert "description=" in result


def test_inject_common_fragments_common_not_a_list():
    """When ``common`` is not a list, it is ignored (but auto-inclusion still fires)."""
    from cai.agents.loader import _inject_common_fragments

    instructions = "# Test Agent\n\nBody."
    config = {"name": "test", "common": "anti_hallucination_guard"}
    result = _inject_common_fragments(config, instructions)
    # Non-list ``common`` is skipped, but anti_hallucination_guard is auto-included.
    assert "You do NOT have an `execute`" in result
    assert "# Test Agent" in result
    assert "Body." in result


def test_inject_common_fragments_tools_not_a_list():
    """When ``tools`` is not a list, task tool note is not injected."""
    from cai.agents.loader import _inject_common_fragments

    instructions = "# Test Agent\n\nBody."
    config = {"name": "test", "tools": "subagents", "common": []}
    result = _inject_common_fragments(config, instructions)
    assert result == instructions


def test_inject_common_fragments_blank_lines_around_insertion():
    """Inserted fragments are surrounded by blank lines."""
    from cai.agents.loader import _inject_common_fragments

    instructions = "# Test Agent\nSome body."
    config = {"name": "test", "common": ["anti_hallucination_guard"]}
    result = _inject_common_fragments(config, instructions)

    lines = result.splitlines()
    # Find the heading line
    heading_idx = next(i for i, l in enumerate(lines) if l.startswith("# "))
    # The line after heading should be blank
    assert lines[heading_idx + 1] == "", (
        f"Expected blank line after heading, got {lines[heading_idx + 1]!r}"
    )


# ── _BATCH_TOOL_CALLS_FRAGMENT auto-injection with filesystem tools ──


def test_inject_common_fragments_batch_tool_calls_with_filesystem():
    """_BATCH_TOOL_CALLS_FRAGMENT is injected when filesystem is in tools."""
    from cai.agents.loader import _inject_common_fragments

    instructions = "# Test Agent\n\nDo stuff."
    config = {"name": "test", "tools": ["filesystem"]}
    result = _inject_common_fragments(config, instructions)
    assert "Batch independent tool calls" in result
    assert "do them all in a single response" in result


def test_inject_common_fragments_batch_tool_calls_with_filesystem_read():
    """_BATCH_TOOL_CALLS_FRAGMENT is injected when filesystem_read is in tools."""
    from cai.agents.loader import _inject_common_fragments

    instructions = "# Test Agent\n\nDo stuff."
    config = {"name": "test", "tools": ["filesystem_read"]}
    result = _inject_common_fragments(config, instructions)
    assert "Batch independent tool calls" in result


def test_inject_common_fragments_batch_tool_calls_with_filesystem_write():
    """_BATCH_TOOL_CALLS_FRAGMENT is injected when filesystem_write is in tools."""
    from cai.agents.loader import _inject_common_fragments

    instructions = "# Test Agent\n\nDo stuff."
    config = {"name": "test", "tools": ["filesystem_write"]}
    result = _inject_common_fragments(config, instructions)
    assert "Batch independent tool calls" in result


def test_inject_common_fragments_no_batch_tool_calls_without_filesystem():
    """_BATCH_TOOL_CALLS_FRAGMENT is NOT injected when no filesystem tools."""
    from cai.agents.loader import _inject_common_fragments

    instructions = "# Test Agent\n\nDo stuff."
    config = {"name": "test", "tools": ["web_search", "web_fetch"]}
    result = _inject_common_fragments(config, instructions)
    assert "Batch independent tool calls" not in result


def test_inject_common_fragments_batch_and_task_tool_notes_together():
    """Both _BATCH_TOOL_CALLS_FRAGMENT and _TASK_TOOL_NOTE when both relevant."""
    from cai.agents.loader import _inject_common_fragments

    instructions = "# Test Agent\n\nDo stuff."
    config = {"name": "test", "tools": ["filesystem", "subagents"]}
    result = _inject_common_fragments(config, instructions)
    assert "Batch independent tool calls" in result
    assert "description=" in result
    assert "`task` tool has no `prompt` parameter" in result


def test_inject_common_fragments_batch_tool_calls_sits_after_task_tool_note():
    """_BATCH_TOOL_CALLS_FRAGMENT is appended after _TASK_TOOL_NOTE when both present."""
    from cai.agents.loader import _inject_common_fragments

    instructions = "# Test Agent\n\nDo stuff."
    config = {"name": "test", "tools": ["filesystem", "subagents"]}
    result = _inject_common_fragments(config, instructions)
    assert result.index("description=") < result.index("Batch independent tool calls")


def test_inject_common_fragments_batch_tool_calls_with_common_fragments():
    """_BATCH_TOOL_CALLS_FRAGMENT works alongside common: fragments."""
    from cai.agents.loader import _inject_common_fragments

    instructions = "# Test Agent\n\nDo stuff."
    config = {
        "name": "test",
        "tools": ["filesystem"],
        "common": ["anti_hallucination_guard"],
    }
    result = _inject_common_fragments(config, instructions)
    assert "Parameter bleed warning" in result
    assert "Batch independent tool calls" in result


# ── _load_common_fragment direct tests ─────────────────────────────


def test_load_common_fragment_existing_name():
    """_load_common_fragment returns content from the expected .md file."""
    from cai.agents.loader import _load_common_fragment

    content = _load_common_fragment("anti_hallucination_guard")
    assert isinstance(content, str)
    assert len(content) > 50
    assert "execute" in content
    assert "Parameter bleed warning" in content


def test_load_common_fragment_file_not_found():
    """_load_common_fragment raises FileNotFoundError for unknown names."""
    from cai.agents.loader import _load_common_fragment

    with pytest.raises(FileNotFoundError):
        _load_common_fragment("nonexistent_fragment")


def test_COMMON_DIR_is_relative_to_AGENT_DIR():
    """_COMMON_DIR must be AGENT_DIR / 'common'."""
    from cai.agents.loader import AGENT_DIR, _COMMON_DIR

    assert _COMMON_DIR == AGENT_DIR / "common"
    assert _COMMON_DIR.is_dir()


def test_load_common_fragment_task_tool_note():
    """_load_common_fragment loads task_tool_note correctly."""
    from cai.agents.loader import _load_common_fragment

    content = _load_common_fragment("task_tool_note")
    assert "description=" in content
    assert "`task` tool has no `prompt` parameter" in content


def test_load_common_fragment_antipattern_examples():
    """_load_common_fragment loads antipattern_examples correctly."""
    from cai.agents.loader import _load_common_fragment

    content = _load_common_fragment("antipattern_examples")
    assert "Anti-pattern examples" in content
    assert "BAD" in content


# ── _inject_common_fragments — idempotency, dedup, ordering ────────


def test_inject_common_fragments_idempotency():
    """Fragment text already present in instructions is skipped (no double-insert)."""
    from cai.agents.loader import _inject_common_fragments, _load_common_fragment

    guard_text = _load_common_fragment("anti_hallucination_guard").strip()
    instructions = f"# Test Agent\n\n{guard_text}\n\nBody text."
    config = {"name": "test", "common": ["anti_hallucination_guard"]}
    result = _inject_common_fragments(config, instructions)
    # anti_hallucination_guard text is already in instructions → skip
    assert result == instructions


def test_inject_common_fragments_no_anti_hallucination_with_execute():
    """anti_hallucination_guard is NOT auto-injected when agent has execute/bash/shell/run tools."""
    from cai.agents.loader import _inject_common_fragments

    instructions = "# Test Agent\n\nBody."
    config = {"name": "test", "tools": ["execute", "filesystem"]}
    result = _inject_common_fragments(config, instructions)
    assert "do NOT have an `execute`" not in result
    assert "Parameter bleed warning" not in result


def test_inject_common_fragments_no_anti_hallucination_with_bash():
    """anti_hallucination_guard is NOT auto-injected when agent has bash tool."""
    from cai.agents.loader import _inject_common_fragments

    instructions = "# Test Agent\n\nBody."
    config = {"name": "test", "tools": ["bash"]}
    result = _inject_common_fragments(config, instructions)
    assert "do NOT have an `execute`" not in result


def test_inject_common_fragments_no_anti_hallucination_with_shell():
    """anti_hallucination_guard is NOT auto-injected when agent has shell tool."""
    from cai.agents.loader import _inject_common_fragments

    instructions = "# Test Agent\n\nBody."
    config = {"name": "test", "tools": ["shell", "filesystem_read"]}
    result = _inject_common_fragments(config, instructions)
    assert "do NOT have an `execute`" not in result


def test_inject_common_fragments_no_anti_hallucination_with_run():
    """anti_hallucination_guard is NOT auto-injected when agent has run tool."""
    from cai.agents.loader import _inject_common_fragments

    instructions = "# Test Agent\n\nBody."
    config = {"name": "test", "tools": ["run", "web_search"]}
    result = _inject_common_fragments(config, instructions)
    assert "do NOT have an `execute`" not in result


def test_inject_common_fragments_deduplicates_explicit_and_auto():
    """Explicitly listing an auto-included fragment does not duplicate it."""
    from cai.agents.loader import _inject_common_fragments

    instructions = "# Test Agent\n\nBody."
    # anti_hallucination_guard is both explicitly listed AND auto-included
    config = {"name": "test", "common": ["anti_hallucination_guard"], "tools": []}
    result = _inject_common_fragments(config, instructions)
    count = result.count("do NOT have an `execute`")
    assert count == 1, f"Expected 1 occurrence, got {count}"


def test_inject_common_fragments_explicit_before_auto():
    """Explicit common: entries appear before auto-included fragments."""
    from cai.agents.loader import _inject_common_fragments

    instructions = "# Test Agent\n\nBody."
    config = {"name": "test", "common": ["antipattern_examples"], "tools": ["subagents"]}
    result = _inject_common_fragments(config, instructions)
    # antipattern_examples is explicit → should appear before auto-included task_tool_note
    assert "Anti-pattern examples" in result
    assert "description=" in result
    assert result.index("Anti-pattern") < result.index("description=")


def test_inject_common_fragments_auto_included_sorted():
    """Multiple auto-included fragments appear in deterministic (sorted) order."""
    from cai.agents.loader import _inject_common_fragments

    instructions = "# Test Agent\n\nBody."
    config = {"name": "test", "common": [], "tools": ["subagents"]}
    result = _inject_common_fragments(config, instructions)
    # With subagents but no execute tools, auto-included are:
    # anti_hallucination_guard, task_tool_note (sorted order)
    assert "do NOT have an `execute`" in result
    assert "description=" in result
    # anti_hallucination_guard (a...) comes before task_tool_note (t...)
    assert result.index("do NOT have") < result.index("description=")


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
def test_edit_file_guardrail_old_string_reports_accurate_count(
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
    handler, _ = _passthrough_handler()
    with pytest.raises(ModelRetry) as exc:
        _run(cap.wrap_tool_execute(
            None, call=_edit_call(), tool_def=None, args=args, handler=handler,
        ))
    msg = str(exc.value)
    assert f"appears {match_count} times" in msg, (
        f"Expected 'appears {match_count} times' in message, got: {msg}"
    )
    assert fpath in msg
    assert "above AND below" in msg
    assert "disambiguate" in msg


def test_edit_file_guardrail_multi_line_ambiguous(tmp_path):
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
    handler, _ = _passthrough_handler()
    with pytest.raises(ModelRetry) as exc:
        _run(cap.wrap_tool_execute(
            None, call=_edit_call(), tool_def=None, args=args, handler=handler,
        ))
    msg = str(exc.value)
    assert "appears 2 times" in msg
    assert "above AND below" in msg


def test_edit_file_guardrail_non_overlapping_count_only(tmp_path):
    """str.count does NOT count overlapping occurrences, so 'aaa'.count('aa') == 1.
    This edge case should still pass through (single match), not raise ModelRetry."""
    cap = EditFileGuardrailAsRetry()
    fpath = _tmp_file(tmp_path, "aaa.py", "aaa\n")
    args = {"path": fpath, "old_string": "aa", "new_string": "bb"}
    handler, calls = _passthrough_handler()
    result = _run(cap.wrap_tool_execute(
        None, call=_edit_call(), tool_def=None, args=args, handler=handler,
    ))
    assert result == "__edit_called__"
    assert calls == [args]


def test_edit_file_guardrail_closest_match_hint(tmp_path):
    """When old_string differs by one blank line, the ModelRetry message
    includes a closest-match hint with line numbers and similarity ratio."""
    cap = EditFileGuardrailAsRetry()
    content = "def foo():\n    pass\n\n\ndef bar():\n    pass\n"
    fpath = _tmp_file(tmp_path, "a.py", content)
    # One blank line instead of two — differs from "    pass\n\n\ndef bar():"
    args = {"path": fpath, "old_string": "    pass\n\ndef bar():", "new_string": "x"}
    handler, _ = _passthrough_handler()
    with pytest.raises(ModelRetry) as exc:
        _run(cap.wrap_tool_execute(
            None, call=_edit_call(), tool_def=None, args=args, handler=handler,
        ))
    msg = str(exc.value)
    assert "old_string not found" in msg
    assert "Closest match at lines" in msg
    assert "% similar" in msg
    assert "Diff:" in msg


def test_edit_file_guardrail_no_closest_match_for_short_string(tmp_path):
    """1-line old_string skips the closest-match hint entirely."""
    cap = EditFileGuardrailAsRetry()
    fpath = _tmp_file(tmp_path, "a.py", "line1\nline2\n")
    args = {"path": fpath, "old_string": "missing", "new_string": "x"}
    handler, _ = _passthrough_handler()
    with pytest.raises(ModelRetry) as exc:
        _run(cap.wrap_tool_execute(
            None, call=_edit_call(), tool_def=None, args=args, handler=handler,
        ))
    msg = str(exc.value)
    assert "old_string not found" in msg
    assert "Closest match" not in msg


def test_default_capabilities_match_between_load_and_build_deep(monkeypatch):
    """The two construction paths (load_agent_from_md, build_deep_agent)
    inject identical capability type-name sets."""
    import cai.agents.loader as loader

    # ── capture load_agent_from_md capabilities ──────────────────────
    caps_from_load = None

    class FakeAgent:
        def __init__(self, model, **kwargs):
            nonlocal caps_from_load
            caps_from_load = kwargs.get("capabilities")

    monkeypatch.setattr(loader, "Agent", FakeAgent)
    monkeypatch.setattr(loader, "build_model", lambda config: object())
    monkeypatch.setattr(loader, "parse_agent_md", lambda path: (
        {"name": "test", "model": "x"}, "instructions",
    ))

    loader.load_agent_from_md("dummy.md")
    load_cap_names = [type(c).__name__ for c in caps_from_load]

    # ── capture build_deep_agent capabilities ───────────────────────
    caps_from_build = None

    def fake_create_deep_agent(model, **kwargs):
        nonlocal caps_from_build
        caps_from_build = kwargs.get("capabilities")
        return object()

    monkeypatch.setattr(
        "pydantic_deep.create_deep_agent", fake_create_deep_agent,
    )

    build_deep_agent({"name": "test", "model": "x"}, "instructions")
    build_cap_names = [type(c).__name__ for c in (caps_from_build or [])]

    assert load_cap_names == build_cap_names, (
        f"Capability lists differ:\n"
        f"  load_agent_from_md: {load_cap_names}\n"
        f"  build_deep_agent:   {build_cap_names}"
    )


# ---------------------------------------------------------------------------
# EditFileGuardrailAsRetry — new_string already present (batch-mate skip)
# ---------------------------------------------------------------------------


def test_edit_file_guardrail_new_string_already_present_returns_skip(tmp_path):
    """When old_string is not found but new_string is already in the file,
    the guardrail returns a skip message instead of raising ModelRetry."""
    fpath = str(tmp_path / "test.py")
    Path(fpath).write_text("existing content\nreplacement_text\n")
    cap = EditFileGuardrailAsRetry()
    args = {"path": fpath, "old_string": "old content", "new_string": "replacement_text"}
    handler, calls = _passthrough_handler()
    result = _run(cap.wrap_tool_execute(
        None, call=_edit_call(), tool_def=None, args=args, handler=handler,
    ))
    assert "change already present in file" in result
    assert "skipping" in result
    assert calls == []  # handler not invoked


def test_edit_file_guardrail_new_string_present_resets_failure_counter(tmp_path):
    """When new_string is already present, the per-path failure counter
    is cleared so a subsequent unrelated failure starts fresh."""
    fpath = str(tmp_path / "test.py")
    Path(fpath).write_text("alpha\nbeta\nreplacement_text\n")
    cap = EditFileGuardrailAsRetry()
    # First call: old_string not found, but new_string present → skip, counter cleared
    args = {"path": fpath, "old_string": "gamma", "new_string": "replacement_text"}
    handler, calls = _passthrough_handler()
    result = _run(cap.wrap_tool_execute(
        None, call=_edit_call(), tool_def=None, args=args, handler=handler,
    ))
    assert "change already present in file" in result
    # Second call: different old_string, not found, new_string not present → ModelRetry (first attempt)
    args2 = {"path": fpath, "old_string": "missing", "new_string": "other_new"}
    handler2, calls2 = _passthrough_handler()
    with pytest.raises(ModelRetry) as exc:
        _run(cap.wrap_tool_execute(
            None, call=_edit_call(), tool_def=None, args=args2, handler=handler2,
        ))
    assert "old_string not found" in str(exc.value)


def test_edit_file_guardrail_new_string_none_falls_through_to_retry(tmp_path):
    """When new_string is None, the guardrail falls through to the normal
    ModelRetry path for not_found."""
    fpath = str(tmp_path / "test.py")
    Path(fpath).write_text("existing content\n")
    cap = EditFileGuardrailAsRetry()
    args = {"path": fpath, "old_string": "missing_string"}
    handler, calls = _passthrough_handler()
    with pytest.raises(ModelRetry) as exc:
        _run(cap.wrap_tool_execute(
            None, call=_edit_call(), tool_def=None, args=args, handler=handler,
        ))
    assert "old_string not found" in str(exc.value)
    assert calls == []


def test_edit_file_guardrail_new_string_empty_falls_through_to_retry(tmp_path):
    """When new_string is an empty string, the guardrail falls through to
    the normal ModelRetry path for not_found."""
    fpath = str(tmp_path / "test.py")
    Path(fpath).write_text("existing content\n")
    cap = EditFileGuardrailAsRetry()
    args = {"path": fpath, "old_string": "missing_string", "new_string": ""}
    handler, calls = _passthrough_handler()
    with pytest.raises(ModelRetry) as exc:
        _run(cap.wrap_tool_execute(
            None, call=_edit_call(), tool_def=None, args=args, handler=handler,
        ))
    assert "old_string not found" in str(exc.value)
    assert calls == []


def test_edit_file_guardrail_new_string_present_with_object_args(tmp_path):
    """When args is an object (not dict), new_string present still returns
    the skip message."""
    fpath = str(tmp_path / "test.py")
    Path(fpath).write_text("new code here\n")
    cap = EditFileGuardrailAsRetry()
    args = SimpleNamespace(
        path=fpath, old_string="old code", new_string="new code here",
    )
    handler, calls = _passthrough_handler()
    result = _run(cap.wrap_tool_execute(
        None, call=_edit_call(), tool_def=None, args=args, handler=handler,
    ))
    assert "change already present in file" in result
    assert "skipping" in result
    assert calls == []


class TestEditFileGuardrailReprDiagnostic:
    """Tests for ``repr(old_string)`` in "not found" error messages."""

    def test_repr_shows_non_printable_characters(self, tmp_path):
        """When old_string contains a non-printable character (backspace \\x08),
        the ModelRetry message includes repr(old_string) so the corruption is
        visible."""
        fpath = str(tmp_path / "test.py")
        Path(fpath).write_text("def foo():\n    pass\n")
        cap = EditFileGuardrailAsRetry()
        # Simulate JSON-level corruption: \b in a regex pattern becomes U+0008
        old_string = "def foo():\n    pass\x08"
        args = {"path": fpath, "old_string": old_string, "new_string": "x"}
        handler, calls = _passthrough_handler()
        with pytest.raises(ModelRetry) as exc:
            _run(cap.wrap_tool_execute(
                None, call=_edit_call(), tool_def=None, args=args, handler=handler,
            ))
        msg = str(exc.value)
        assert "old_string repr:" in msg
        assert r"\x08" in msg
        assert calls == []

    def test_repr_in_escalation_warning(self, tmp_path):
        """After ESCALATE_AT repeats, the warning string also includes
        repr(old_string)."""
        fpath = str(tmp_path / "test.py")
        Path(fpath).write_text("def foo():\n    pass\n")
        cap = EditFileGuardrailAsRetry()
        old_string = "not in file\x00"
        args = {"path": fpath, "old_string": old_string, "new_string": "x"}
        handler, calls = _passthrough_handler()

        # First call → ModelRetry
        with pytest.raises(ModelRetry):
            _run(cap.wrap_tool_execute(
                None, call=_edit_call(), tool_def=None, args=args, handler=handler,
            ))
        # Second call → warning (ESCALATE_AT = 2)
        result = _run(cap.wrap_tool_execute(
            None, call=_edit_call(), tool_def=None, args=args, handler=handler,
        ))
        assert "old_string repr:" in result
        assert r"\x00" in result
        assert calls == []


class TestEditFileGuardrailTruncationHint:
    """Tests for mid-word truncation detection in "not found" errors."""

    def test_truncation_hint_for_mid_word_ending(self, tmp_path):
        """When old_string ends mid-word (last char is lowercase letter),
        the error message includes a truncation hint."""
        fpath = str(tmp_path / "test.py")
        Path(fpath).write_text('assert sub_meta.labels == ["bug", "critical"]\n')
        cap = EditFileGuardrailAsRetry()
        old_string = 'assert sub_meta.labels == ["bug", "priorit'
        args = {"path": fpath, "old_string": old_string, "new_string": "x"}
        handler, calls = _passthrough_handler()
        with pytest.raises(ModelRetry) as exc:
            _run(cap.wrap_tool_execute(
                None, call=_edit_call(), tool_def=None, args=args, handler=handler,
            ))
        msg = str(exc.value)
        assert "old_string repr:" in msg
        assert "appears truncated" in msg
        assert "ends mid-word" in msg
        assert calls == []

    def test_no_truncation_hint_when_ends_at_line_boundary(self, tmp_path):
        """When old_string ends with a newline or punctuation, no truncation
        hint is produced."""
        fpath = str(tmp_path / "test.py")
        Path(fpath).write_text("def foo():\n    pass\n")
        cap = EditFileGuardrailAsRetry()
        old_string = "def bar():\n    return True\n"
        args = {"path": fpath, "old_string": old_string, "new_string": "x"}
        handler, calls = _passthrough_handler()
        with pytest.raises(ModelRetry) as exc:
            _run(cap.wrap_tool_execute(
                None, call=_edit_call(), tool_def=None, args=args, handler=handler,
            ))
        msg = str(exc.value)
        assert "appears truncated" not in msg


class TestEditFileGuardrailWhitespaceNormalizationHint:
    """Tests for whitespace-normalization diagnostic in "not found" errors."""

    def test_whitespace_normalization_hint(self, tmp_path):
        """When old_string has extra leading spaces but matches after stripping
        leading whitespace from every line, the error includes a normalization
        hint and does NOT auto-apply the edit."""
        fpath = str(tmp_path / "test.py")
        # Content has 4-space indent
        Path(fpath).write_text("def foo():\n    pass\n")
        cap = EditFileGuardrailAsRetry()
        # old_string has 8-space indent instead of 4-space
        old_string = "def foo():\n        pass\n"
        args = {"path": fpath, "old_string": old_string, "new_string": "x"}
        handler, calls = _passthrough_handler()
        with pytest.raises(ModelRetry) as exc:
            _run(cap.wrap_tool_execute(
                None, call=_edit_call(), tool_def=None, args=args, handler=handler,
            ))
        msg = str(exc.value)
        assert "whitespace normalization" in msg
        assert "check your indentation" in msg
        # The handler must NOT have been called (no auto-apply)
        assert calls == []

    def test_no_whitespace_hint_when_normalized_also_fails(self, tmp_path):
        """When the old_string doesn't match even after whitespace normalization,
        no normalization hint is included."""
        fpath = str(tmp_path / "test.py")
        Path(fpath).write_text("def foo():\n    pass\n")
        cap = EditFileGuardrailAsRetry()
        old_string = "completely different content\n"
        args = {"path": fpath, "old_string": old_string, "new_string": "x"}
        handler, calls = _passthrough_handler()
        with pytest.raises(ModelRetry) as exc:
            _run(cap.wrap_tool_execute(
                None, call=_edit_call(), tool_def=None, args=args, handler=handler,
            ))
        msg = str(exc.value)
        assert "whitespace normalization" not in msg


class TestEditFileGuardrailNewStringCountGuard:
    """Tests for the ``content.count(new_string) <= 3`` guard added to the
    batch-mate detection in ``EditFileGuardrailAsRetry.wrap_tool_execute``.

    When ``new_string`` appears more than 3 times in the file content, it is
    likely a trivial/common substring (e.g. ``"x"`` in a file of repeated
    ``x``'s) rather than having been inserted by a prior batch-mate edit.
    In that case the guardrail must NOT skip the edit — it must fall through
    to the normal ``ModelRetry`` / escalation path instead.
    """

    def test_new_string_too_common_does_not_skip(self, tmp_path):
        """When new_string is a common substring that appears >3 times, the
        batch-mate skip must NOT trigger — ModelRetry is raised."""
        fpath = str(tmp_path / "common.txt")
        Path(fpath).write_text("x\nx\nx\nx\nx\n")
        cap = EditFileGuardrailAsRetry()
        args = {"path": fpath, "old_string": "nonexistent", "new_string": "x"}
        handler, calls = _passthrough_handler()
        with pytest.raises(ModelRetry) as exc:
            _run(cap.wrap_tool_execute(
                None, call=_edit_call(), tool_def=None, args=args, handler=handler,
            ))
        assert "old_string not found" in str(exc.value)
        assert calls == []

    def test_new_string_present_once_skips(self, tmp_path):
        """When new_string appears exactly once, the batch-mate skip SHOULD
        trigger (count <= 3 and new_string in content)."""
        fpath = str(tmp_path / "single.txt")
        Path(fpath).write_text("unique_new_content\n")
        cap = EditFileGuardrailAsRetry()
        args = {"path": fpath, "old_string": "nonexistent", "new_string": "unique_new_content"}
        handler, calls = _passthrough_handler()
        result = _run(cap.wrap_tool_execute(
            None, call=_edit_call(), tool_def=None, args=args, handler=handler,
        ))
        assert "skipping" in result
        assert calls == []

    def test_new_string_present_exactly_three_times_skips(self, tmp_path):
        """When new_string appears exactly 3 times (the <=3 boundary), the
        batch-mate skip SHOULD trigger."""
        fpath = str(tmp_path / "three.txt")
        Path(fpath).write_text("abc\nabc\nabc\n")
        cap = EditFileGuardrailAsRetry()
        args = {"path": fpath, "old_string": "nonexistent", "new_string": "abc"}
        handler, calls = _passthrough_handler()
        result = _run(cap.wrap_tool_execute(
            None, call=_edit_call(), tool_def=None, args=args, handler=handler,
        ))
        assert "skipping" in result
        assert calls == []

    def test_new_string_present_exactly_four_times_does_not_skip(self, tmp_path):
        """When new_string appears exactly 4 times (just past the <=3 boundary),
        the batch-mate skip must NOT trigger — ModelRetry is raised."""
        fpath = str(tmp_path / "four.txt")
        Path(fpath).write_text("abc\nabc\nabc\nabc\n")
        cap = EditFileGuardrailAsRetry()
        args = {"path": fpath, "old_string": "nonexistent", "new_string": "abc"}
        handler, calls = _passthrough_handler()
        with pytest.raises(ModelRetry) as exc:
            _run(cap.wrap_tool_execute(
                None, call=_edit_call(), tool_def=None, args=args, handler=handler,
            ))
        assert "old_string not found" in str(exc.value)
        assert calls == []

    def test_new_string_not_a_string_does_not_skip(self, tmp_path):
        """When new_string is not a string (e.g. an integer), the isinstance
        check fails and the guardrail falls through to ModelRetry."""
        fpath = str(tmp_path / "int.txt")
        Path(fpath).write_text("abc\n")
        cap = EditFileGuardrailAsRetry()
        args = {"path": fpath, "old_string": "nonexistent", "new_string": 42}
        handler, calls = _passthrough_handler()
        with pytest.raises(ModelRetry) as exc:
            _run(cap.wrap_tool_execute(
                None, call=_edit_call(), tool_def=None, args=args, handler=handler,
            ))
        assert "old_string not found" in str(exc.value)
        assert calls == []


# ---------------------------------------------------------------------------
# Explore agent — Search-then-read strategy prompt sections
# ---------------------------------------------------------------------------


SEARCH_THEN_READ_HEADING = "## Search then read"

PHASE_1_TEXT = "- **Phase 1 — Search:** Before any `read_file`, extract key symbols, function names, file paths, and patterns from the issue. Run `grep` and `glob` for those patterns **in parallel** in a single round-trip. Cast a wide net: search for class names, function definitions, import paths, and distinctive strings mentioned in the issue."

PHASE_2_TEXT = "- **Phase 2 — Read:** Only after search results come back, `read_file` on files that matched. Read only the files that had hits — skip files with zero matches."

STOP_AT_RELEVANCE_TEXT = "- **Stop at relevance:** Do not chase transitive imports, call sites, or infrastructure files (`loader.py`, `state.py`, `refine.py`, `solve.py`, etc.) unless a grep result directly implicates them. If a file you read does not answer the question, stop exploring that direction."

NEVER_RE_READ_TEXT = "**Never re-read a file you have already read.**"

RELEVANCE_GATE_TEXT = "- **Relevance gate:** After reading a file, verify it answered the specific question from the issue. If it did not, stop exploring that direction. Do not follow imports or call sites transitively unless they appear in a grep match for the issue's key symbols."

HOW_TO_WORK_HEADING = "## How to work"

READ_FILES_WHOLE_BULLET = "- **Read files whole:** Prefer reading entire files by omitting `offset` and `limit`."


def test_explore_agent_prompt_includes_search_then_read_section():
    """The explore agent's system prompt must contain the '## Search then read'
    section heading."""
    path = resolve_agent_path("explore")
    _, system_prompt = parse_agent_md(path)
    assert SEARCH_THEN_READ_HEADING in system_prompt, (
        "Explore agent missing '## Search then read' heading."
    )


def test_explore_agent_prompt_includes_phase_1_search():
    """Phase 1 — Search bullet must be present under Search then read."""
    path = resolve_agent_path("explore")
    _, system_prompt = parse_agent_md(path)
    assert PHASE_1_TEXT in system_prompt, (
        "Explore agent missing Phase 1 — Search bullet."
    )


def test_explore_agent_prompt_includes_phase_2_read():
    """Phase 2 — Read bullet must be present under Search then read."""
    path = resolve_agent_path("explore")
    _, system_prompt = parse_agent_md(path)
    assert PHASE_2_TEXT in system_prompt, (
        "Explore agent missing Phase 2 — Read bullet."
    )


def test_explore_agent_prompt_includes_stop_at_relevance_bullet():
    """Stop at relevance bullet must be present under Search then read."""
    path = resolve_agent_path("explore")
    _, system_prompt = parse_agent_md(path)
    assert STOP_AT_RELEVANCE_TEXT in system_prompt, (
        "Explore agent missing 'Stop at relevance' bullet."
    )


def test_explore_agent_prompt_includes_never_re_read_directive():
    """The strengthened 'Never re-read a file you have already read' directive
    must be present under the Read files whole bullet."""
    path = resolve_agent_path("explore")
    _, system_prompt = parse_agent_md(path)
    assert NEVER_RE_READ_TEXT in system_prompt, (
        "Explore agent missing 'Never re-read a file you have already read' directive."
    )


def test_explore_agent_prompt_includes_relevance_gate_bullet():
    """Relevance gate bullet must be present under How to work."""
    path = resolve_agent_path("explore")
    _, system_prompt = parse_agent_md(path)
    assert RELEVANCE_GATE_TEXT in system_prompt, (
        "Explore agent missing 'Relevance gate' bullet."
    )


def test_explore_agent_search_then_read_before_how_to_work():
    """The Search then read section must appear before the How to work section
    in the explore agent's system prompt."""
    path = resolve_agent_path("explore")
    _, system_prompt = parse_agent_md(path)
    search_idx = system_prompt.index(SEARCH_THEN_READ_HEADING)
    how_to_idx = system_prompt.index(HOW_TO_WORK_HEADING)
    assert search_idx < how_to_idx, (
        "Search then read section must appear before How to work section."
    )


def test_explore_agent_never_re_read_under_read_files_whole():
    """The 'Never re-read' directive must appear on the same line or within the
    Read files whole bullet, not as a standalone bullet."""
    path = resolve_agent_path("explore")
    _, system_prompt = parse_agent_md(path)
    never_idx = system_prompt.index(NEVER_RE_READ_TEXT)
    # The Read files whole bullet should appear before (on the same bullet line
    # or very close to) the never-re-read text.
    read_whole_idx = system_prompt.index(READ_FILES_WHOLE_BULLET)
    assert read_whole_idx < never_idx, (
        "Never re-read directive must appear after the Read files whole bullet."
    )
    # The gap between the bullet start and the directive should be within
    # reasonable proximity (same or next line).
    gap = never_idx - (read_whole_idx + len(READ_FILES_WHOLE_BULLET))
    assert 0 <= gap < 120, (
        f"Never re-read directive is too far from the Read files whole bullet "
        f"(gap={gap} chars). It should be part of that bullet."
    )


def test_history_compactor_grep_non_modifying_tools_preserve_short_circuit():
    """Non-file-modifying tools (ls, glob, another grep) between
    a full read_file and a grep on that same file do not block the
    short-circuit — only file-modifying tools should."""
    cap = HistoryCompactorCapability()
    handler_called = False

    async def handler(args):
        nonlocal handler_called
        handler_called = True
        return "grep results"

    prior_tc = ToolCallPart(tool_name="read_file", args={"path": "x.py"}, tool_call_id="c1")
    prior_tr = ToolReturnPart(tool_name="read_file", content="full content", tool_call_id="c1")
    ls_tc = ToolCallPart(tool_name="ls", args={"path": "."}, tool_call_id="c2")
    ls_tr = ToolReturnPart(tool_name="ls", content="foo.py  bar.py", tool_call_id="c2")
    glob_tc = ToolCallPart(tool_name="glob", args={"pattern": "*.py"}, tool_call_id="c3")
    glob_tr = ToolReturnPart(tool_name="glob", content="x.py", tool_call_id="c3")

    ctx = _make_ctx(
        messages=[
            ModelResponse(parts=[prior_tc]),
            ModelRequest(parts=[prior_tr]),
            ModelResponse(parts=[ls_tc, glob_tc]),
            ModelRequest(parts=[ls_tr, glob_tr]),
        ],
    )

    call = ToolCallPart(tool_name="grep", args={"path": "x.py", "pattern": "content"}, tool_call_id="c4")

    result = _run(cap.wrap_tool_execute(ctx, call=call, tool_def=None, args={"path": "x.py", "pattern": "content"}, handler=handler))

    assert not handler_called
    assert "Warning: grep" in result
    assert "already read in full" in result

