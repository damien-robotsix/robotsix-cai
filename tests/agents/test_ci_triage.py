"""Tests for the ci_triage agent definition (ci_triage.md)."""

from cai.agents.loader import parse_agent_md, resolve_agent_path, build_deep_agent


def test_ci_triage_agent_config():
    """ci_triage.md exists, parses, and has correct frontmatter."""
    ct_file = resolve_agent_path("ci_triage")
    assert ct_file.exists(), "ci_triage.md must exist in AGENT_DIR"
    config, instructions = parse_agent_md(ct_file)

    # Assert basics
    assert config["name"] == "ci_triage"
    assert config["model"] == "deepseek/deepseek-v4-pro"

    # Assert description
    assert "description" in config
    desc = config["description"]
    assert "CI" in desc
    assert "failure" in desc.lower()
    # Description used to mention 'issue' — now triage files tickets, but
    # the description may still phrase it as filing an issue/ticket; tolerate either.
    assert ("issue" in desc.lower()) or ("ticket" in desc.lower())

    # Assert expected tools
    tools = config.get("tools", [])
    assert "filesystem_read" in tools
    assert "raise_ticket" in tools
    assert "web_fetch" in tools
    assert "traces_show" in tools, (
        "ci_triage must list traces_show to investigate prior CAI runs"
    )
    assert "traces_failures" in tools, (
        "ci_triage must list traces_failures to find failed CAI traces"
    )


def test_ci_triage_instructions_content():
    """The system prompt must contain expected how-to sections."""
    path = resolve_agent_path("ci_triage")
    _, instructions = parse_agent_md(path)

    # Required sections
    assert "# CI Triage Agent" in instructions
    assert "## How to work" in instructions

    # Core instructions
    assert "Read the provided job logs" in instructions
    assert "Identify the root cause" in instructions
    assert "grep" in instructions
    assert "raise_ticket" in instructions


def test_ci_triage_trace_tools_instructions():
    """Step 5 must guide the agent to use traces_failures and traces_show."""
    path = resolve_agent_path("ci_triage")
    _, instructions = parse_agent_md(path)

    assert "traces_failures" in instructions, (
        "Instructions must reference traces_failures tool"
    )
    assert "traces_show" in instructions, (
        "Instructions must reference traces_show tool"
    )
    assert "prior CAI run" in instructions or "previous solve" in instructions.lower(), (
        "Instructions must mention investigating prior CAI runs"
    )
    assert "Langfuse" in instructions, (
        "Instructions must mention Langfuse traces"
    )


def test_ci_triage_no_execute_tools():
    """The agent must not have write or execute tools."""
    path = resolve_agent_path("ci_triage")
    config, instructions = parse_agent_md(path)
    tools = config.get("tools", [])

    # The agent reads files and files issues — no filesystem write
    assert "filesystem_write" not in tools
    assert "filesystem" not in tools  # explicit read-only variant used instead
    assert "execute" not in instructions.lower() or "Do not execute" in instructions


def test_ci_triage_raise_ticket_instructions():
    """The instructions must explain how to call raise_ticket."""
    path = resolve_agent_path("ci_triage")
    _, instructions = parse_agent_md(path)

    assert "raise_ticket" in instructions
    # CI triage files tickets at status=Ready so the solve cron auto-picks
    # them up; assert the lifecycle hook is documented.
    assert "Ready" in instructions
    assert "code-change" in instructions
    assert "The failed job and step" in instructions
    assert "The error summary" in instructions
    assert "The root cause analysis" in instructions
    assert "The affected files" in instructions


def test_ci_triage_has_subagents_tool():
    """ci_triage must list 'subagents' in its tools to enable subagent dispatch."""
    path = resolve_agent_path("ci_triage")
    config, _instructions = parse_agent_md(path)
    tools = config.get("tools", [])
    assert "subagents" in tools, (
        "ci_triage must list subagents in tools to delegate to trace_analyst"
    )


def test_ci_triage_has_trace_analyst_subagent():
    """ci_triage must declare trace_analyst as a subagent."""
    path = resolve_agent_path("ci_triage")
    config, _instructions = parse_agent_md(path)
    subagents = config.get("subagents", [])
    assert "trace_analyst" in subagents, (
        "ci_triage must list trace_analyst as a subagent for deep trace analysis"
    )


def test_ci_triage_trace_analyst_instructions(monkeypatch):
    """Instructions must describe delegating deep trace analysis to trace_analyst."""
    path = resolve_agent_path("ci_triage")
    config, instructions = parse_agent_md(path)

    assert "trace_analyst" in instructions, (
        "Instructions must reference the trace_analyst subagent"
    )
    assert "delegate" in instructions.lower(), (
        "Instructions must tell the agent to delegate to the subagent"
    )
    assert "trace ID" in instructions, (
        "Instructions must mention passing a specific trace ID to the subagent"
    )

    # Task-tool-note is auto-injected by build_deep_agent — verify via merged output.
    captured_instructions = []

    def fake_create(model, *, name, instructions, **kwargs):
        captured_instructions.append(instructions)
        return object()

    monkeypatch.setattr("pydantic_deep.create_deep_agent", fake_create)
    monkeypatch.setattr("cai.agents.loader._resolve_subagents", lambda c: [])
    monkeypatch.setattr("cai.agents.loader.build_model", lambda c: None)
    monkeypatch.setattr("cai.agents.loader.build_deep_agent_kwargs", lambda c: {})
    monkeypatch.setattr("cai.agents.loader._prune_toolsets", lambda a, r: None)
    build_deep_agent(config, instructions)
    assert captured_instructions, "build_deep_agent did not call create_deep_agent"
    merged = captured_instructions[0]

    assert "description=" in merged or "description =" in merged, (
        "Merged instructions must contain the task-tool-note about passing "
        "description=, not prompt="
    )
