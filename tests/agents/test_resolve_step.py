"""Tests for the resolve_step agent definition (resolve_step.md)."""
from __future__ import annotations

import pytest

from cai.agents.loader import parse_agent_md, resolve_agent_path


def test_resolve_step_agent_config():
    """Verify resolve_step frontmatter config matches expected values."""
    rs_file = resolve_agent_path("resolve_step")
    assert rs_file.exists(), "resolve_step.md must exist in AGENT_DIR"
    config, instructions = parse_agent_md(rs_file)

    # Name
    assert config["name"] == "resolve_step"

    # Model — must be the flash (non-reasoning) variant
    assert config["model"] == "deepseek/deepseek-v4-flash"

    # Tools
    tools = config.get("tools", [])
    assert "filesystem_read" in tools
    assert "conflict_list" in tools
    assert "conflict_resolve" in tools
    assert "conflict_cleanup" in tools

    # Description
    assert "description" in config
    assert "conflict" in config["description"].lower()
    assert "rebase" in config["description"].lower()

    # No write tools
    assert "filesystem_write" not in tools
    assert "filesystem" not in tools


def test_resolve_step_model_is_not_pro():
    """Regression test: resolve_step must NOT use the expensive reasoning model."""
    rs_file = resolve_agent_path("resolve_step")
    config, _ = parse_agent_md(rs_file)
    assert config["model"] != "deepseek/deepseek-v4-pro", (
        "resolve_step should use deepseek-v4-flash, not the expensive pro reasoning model"
    )


def test_resolve_step_tool_boundary_note_present():
    """resolve_step instructions include the Tool boundary note."""
    _, instructions = parse_agent_md(resolve_agent_path("resolve_step"))
    assert "**Tool boundary:**" in instructions


def test_resolve_step_tool_boundary_mentions_missing_tools():
    """The Tool boundary note explicitly states edit_file, write_file, execute are absent."""
    _, instructions = parse_agent_md(resolve_agent_path("resolve_step"))
    boundary_section = instructions[instructions.index("**Tool boundary:**"):]
    assert "edit_file" in boundary_section or "`edit_file`" in boundary_section
    assert "write_file" in boundary_section or "`write_file`" in boundary_section
    assert "execute" in boundary_section or "`execute`" in boundary_section


def test_resolve_step_tool_boundary_mentions_conflict_cleanup():
    """The Tool boundary note directs to conflict_cleanup for dead code debris."""
    _, instructions = parse_agent_md(resolve_agent_path("resolve_step"))
    boundary_section = instructions[instructions.index("**Tool boundary:**"):]
    assert "conflict_cleanup" in boundary_section
    assert "dead code" in boundary_section.lower() or "debris" in boundary_section


def test_resolve_step_no_execute_in_tools():
    """resolve_step must not have execute, bash, shell, or run tools."""
    config, _ = parse_agent_md(resolve_agent_path("resolve_step"))
    tools = config.get("tools", [])
    for banned in ("execute", "bash", "shell", "run"):
        assert banned not in tools, (
            f"resolve_step must not declare {banned!r} in its tool list"
        )


def test_resolve_step_no_write_file_or_edit_file_in_tools():
    """resolve_step must not have write_file or edit_file in its tool list."""
    config, _ = parse_agent_md(resolve_agent_path("resolve_step"))
    tools = config.get("tools", [])
    assert "write_file" not in tools
    assert "edit_file" not in tools


def test_resolve_step_prompt_includes_conflict_workflow():
    """Verify resolve_step.md contains the conflict resolution workflow steps."""
    _, instructions = parse_agent_md(resolve_agent_path("resolve_step"))
    assert "conflict_list" in instructions
    assert "conflict_resolve" in instructions
    assert "conflict_cleanup" in instructions
    assert "ours" in instructions
    assert "theirs" in instructions
    assert "custom merged content" in instructions


def test_resolve_step_prompt_warns_against_unrelated_changes():
    """resolve_step instructs not to touch files outside the conflict list."""
    _, instructions = parse_agent_md(resolve_agent_path("resolve_step"))
    assert "Do not touch any file not in the conflicted-files list" in instructions
    assert "Do not invent unrelated changes" in instructions


def test_resolve_step_prompt_includes_grep_truncation_warning():
    """resolve_step inherits the common grep truncation warning."""
    _, instructions = parse_agent_md(resolve_agent_path("resolve_step"))
    assert "grep truncation" in instructions
    assert "file_info" in instructions
