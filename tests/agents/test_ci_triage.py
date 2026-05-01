"""Tests for the ci_triage agent definition (ci_triage.md)."""

from cai.agents.loader import parse_agent_md, resolve_agent_path


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

    # Assert expected tools
    tools = config.get("tools", [])
    assert "filesystem_read" in tools
    assert "raise_issue" in tools
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
    tools = config.get("tools", [])

    # The agent reads files and files issues — no filesystem write
    assert "filesystem_write" not in tools
    assert "filesystem" not in tools  # explicit read-only variant used instead
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
