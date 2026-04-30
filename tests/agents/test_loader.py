import asyncio
from types import SimpleNamespace

import pytest
from pathlib import Path
from pydantic_ai.exceptions import ModelRetry

from cai.agents.loader import (
    EditFileGuardrailAsRetry,
    GrepGuardrailAsRetry,
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
    with pytest.raises(ModelRetry, match="Repeated zero-result grep"):
        _run(cap.after_tool_execute(
            None, call=_grep_call(), tool_def=None, args={},
            result="No matches for 'bar'",
        ))
    # counter resets after triggering so the next streak starts fresh
    assert cap._empty_grep_count == 0


def test_grep_guardrail_for_run_returns_fresh_instance():
    cap = GrepGuardrailAsRetry()
    cap._empty_grep_count = 5
    fresh = _run(cap.for_run(None))
    assert fresh is not cap
    assert fresh._empty_grep_count == 0


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
