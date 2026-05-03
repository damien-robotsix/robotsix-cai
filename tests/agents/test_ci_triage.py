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
    assert "issue" in desc.lower()

    # Assert expected skills (TOOL_FLAGS — read-only skill tools)
    skills = config.get("skills", [])
    assert "filesystem_read" in skills
    assert "web_fetch" in skills

    # Assert expected commands (TOOL_FACTORIES — code-registered command tools)
    commands = config.get("commands", [])
    assert "raise_issue" in commands
    assert "traces_show" in commands, (
        "ci_triage must list traces_show to investigate prior CAI runs"
    )
    assert "traces_failures" in commands, (
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
    assert "raise_issue" in instructions


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
    skills = config.get("skills", [])
    commands = config.get("commands", [])
    all_tools = skills + commands

    # The agent reads files and files issues — no filesystem write
    assert "filesystem_write" not in all_tools
    assert "filesystem" not in all_tools  # explicit read-only variant used instead
    assert "execute" not in instructions.lower() or "Do not execute" in instructions


def test_ci_triage_raise_issue_instructions():
    """The instructions must explain how to call raise_issue."""
    path = resolve_agent_path("ci_triage")
    _, instructions = parse_agent_md(path)

    assert "raise_issue" in instructions
    assert "cai:raised" in instructions
    assert "labels" in instructions
    assert "The failed job and step" in instructions
    assert "The error summary" in instructions
    assert "The root cause analysis" in instructions
    assert "The affected files" in instructions


def test_ci_triage_has_subagents_tool():
    """ci_triage must list 'subagents' in its tools to enable subagent dispatch."""
    path = resolve_agent_path("ci_triage")
    config, _instructions = parse_agent_md(path)
    skills = config.get("skills", [])
    assert "subagents" in skills, (
        "ci_triage must list subagents in skills to delegate to trace_analyst"
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

    # The task-tool-note must be present directly in the raw instructions
    # (no longer relying solely on auto-injection by build_deep_agent).
    assert "task" in instructions.lower(), (
        "Instructions must reference the task tool for subagent delegation"
    )
    assert "description=" in instructions or "description =" in instructions, (
        "Raw instructions must contain the task-tool-note about passing "
        "description=, not prompt="
    )
    assert "no `prompt` parameter" in instructions.lower(), (
        "Instructions must clarify that the task tool has no prompt parameter"
    )

    # Also verify the note flows through to the final merged instructions
    # when build_deep_agent is called.
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
