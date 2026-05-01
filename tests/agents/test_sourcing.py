import pytest
from cai.agents.loader import parse_agent_md, resolve_agent_path


def test_sourcing_agent_config():
    """The sourcing agent definition must exist and parse correctly."""
    sourcing_file = resolve_agent_path("sourcing")
    assert sourcing_file.exists(), "sourcing.md must exist in AGENT_DIR"
    config, instructions = parse_agent_md(sourcing_file)

    # Assert basics
    assert config["name"] == "sourcing"
    assert config["model"] == "deepseek/deepseek-v4-pro"

    # Assert expected tools
    tools = config.get("tools", [])
    assert "web_search" in tools
    assert "web_fetch" in tools

    # Assert subagents
    subagents = config.get("subagents", [])
    assert "issue_deduplicator" in subagents

    # Assert description
    assert "description" in config
    assert "open-source" in config["description"].lower()


def test_sourcing_agent_instructions_structure():
    """The sourcing system prompt must include expected sections."""
    path = resolve_agent_path("sourcing")
    _, instructions = parse_agent_md(path)

    # Required sections
    assert "# Sourcing Agent" in instructions
    assert "## How to work" in instructions
    assert "## Confidence rubric" in instructions
    assert "## Guidelines" in instructions


def test_sourcing_agent_subagent_usage_section():
    """The sourcing agent must include a ## Subagent usage section
    explaining when to delegate to issue_deduplicator."""
    path = resolve_agent_path("sourcing")
    _, instructions = parse_agent_md(path)

    assert "## Subagent usage" in instructions, (
        "sourcing.md system prompt missing the '## Subagent usage' section."
    )
    assert "issue_deduplicator" in instructions


def test_sourcing_agent_task_tool_parameter_note():
    """The sourcing agent must include the task tool parameter-name note
    in its system prompt."""
    path = resolve_agent_path("sourcing")
    _, instructions = parse_agent_md(path)

    expected = (
        "When calling the `task` tool, pass the subagent instructions as "
        "`description=`, not `prompt=`. The `task` tool has no `prompt` parameter."
    )
    assert expected in instructions, (
        "sourcing.md system prompt missing the task-tool parameter-name note."
    )


def test_sourcing_agent_subagent_usage_mentions_deduplication():
    """The ## Subagent usage section must explain the deduplication purpose."""
    path = resolve_agent_path("sourcing")
    _, instructions = parse_agent_md(path)

    # Find the subagent usage section
    section_start = instructions.find("## Subagent usage")
    assert section_start >= 0
    section_end = instructions.find("##", section_start + 1)
    if section_end == -1:
        section_end = len(instructions)
    section_text = instructions[section_start:section_end]

    assert (
        "delegate" in section_text.lower()
        or "check whether" in section_text.lower()
    ), (
        "The ## Subagent usage section should explain when to delegate "
        "to issue_deduplicator."
    )
