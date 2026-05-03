"""Tests for the 'tools' audit mode in cai.workflows.audit."""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cai.workflows.audit import (
    _build_tools_prompt,
)


def test_build_tools_prompt_no_sessions():
    """_build_tools_prompt raises SystemExit when no solve sessions are found."""
    with patch("cai.workflows.audit._TRACES") as mock_traces:
        mock_traces.list_solve_sessions.return_value = []
        with pytest.raises(SystemExit):
            _build_tools_prompt([])


def test_build_tools_prompt_unknown_args():
    """Unknown CLI args are forwarded as 'Additional context' in the tools prompt."""
    fake_sessions = [{"session_id": "issue-1", "trace_ids": ["trace-a"]}]
    fake_trace = {"tool_counts": {"read_file": 1}}
    fake_config = {"name": "agent_a", "tools": ["read_file"], "subagents": []}
    mock_md_path = MagicMock()
    mock_md_path.stem = "agent_a"

    with patch("cai.workflows.audit._TRACES") as mock_traces:
        mock_traces.list_solve_sessions.return_value = fake_sessions
        mock_traces.show_trace.return_value = fake_trace
        with patch("cai.workflows.audit.parse_agent_md", return_value=(fake_config, "")):
            with patch.object(Path, "glob", return_value=[mock_md_path]):
                prompt = _build_tools_prompt(["--verbose", "--focus", "bash"])

    assert "Additional context: --verbose --focus bash" in prompt


def test_build_tools_prompt_declares_missing_tools():
    """Declared tools never observed in any trace are listed in a dedicated section."""
    fake_sessions = [
        {"session_id": "issue-1", "trace_ids": ["trace-a"]},
    ]
    fake_trace = {"tool_counts": {"read_file": 10, "edit_file": 5}}
    fake_config = {
        "name": "agent_a",
        "tools": ["read_file", "edit_file", "write_file", "grep"],
        "subagents": [],
    }
    mock_md_path = MagicMock()
    mock_md_path.stem = "agent_a"

    with patch("cai.workflows.audit._TRACES") as mock_traces:
        mock_traces.list_solve_sessions.return_value = fake_sessions
        mock_traces.show_trace.return_value = fake_trace
        with patch("cai.workflows.audit.parse_agent_md", return_value=(fake_config, "")):
            with patch.object(Path, "glob", return_value=[mock_md_path]):
                prompt = _build_tools_prompt([])

    # write_file and grep are declared but never observed
    never_observed_section = prompt.split("## Declared Tools Never Observed")[1]
    assert "**agent_a**: write_file, grep" in never_observed_section
    # The "all observed" message should NOT appear
    assert "(all declared tools were observed" not in never_observed_section


def test_build_tools_prompt_all_tools_observed():
    """When all declared tools appear in at least one trace, show the all-observed message."""
    fake_sessions = [
        {"session_id": "issue-1", "trace_ids": ["trace-a"]},
    ]
    fake_trace = {"tool_counts": {"read_file": 10, "grep": 3, "edit_file": 2, "write_file": 1}}
    fake_config = {
        "name": "agent_a",
        "tools": ["read_file", "grep", "edit_file", "write_file"],
        "subagents": [],
    }
    mock_md_path = MagicMock()
    mock_md_path.stem = "agent_a"

    with patch("cai.workflows.audit._TRACES") as mock_traces:
        mock_traces.list_solve_sessions.return_value = fake_sessions
        mock_traces.show_trace.return_value = fake_trace
        with patch("cai.workflows.audit.parse_agent_md", return_value=(fake_config, "")):
            with patch.object(Path, "glob", return_value=[mock_md_path]):
                prompt = _build_tools_prompt([])

    # The all-observed message should appear in the Declared Tools Never Observed section
    never_observed_section = prompt.split("## Declared Tools Never Observed")[1]
    assert "(all declared tools were observed in at least one sampled trace)" in never_observed_section


def test_build_tools_prompt_no_trace_ids_skips_session():
    """Sessions with empty trace_ids are skipped (no show_trace call for them)."""
    fake_sessions = [
        {"session_id": "issue-empty", "trace_ids": []},
        {"session_id": "issue-full", "trace_ids": ["trace-b"]},
    ]
    fake_trace = {"tool_counts": {"read_file": 1}}
    fake_config = {"name": "agent_a", "tools": ["read_file"], "subagents": []}
    mock_md_path = MagicMock()
    mock_md_path.stem = "agent_a"

    with patch("cai.workflows.audit._TRACES") as mock_traces:
        mock_traces.list_solve_sessions.return_value = fake_sessions
        mock_traces.show_trace.return_value = fake_trace
        with patch("cai.workflows.audit.parse_agent_md", return_value=(fake_config, "")):
            with patch.object(Path, "glob", return_value=[mock_md_path]):
                prompt = _build_tools_prompt([])

    # Only the session with trace_ids appears
    assert "issue-full" in prompt
    assert "trace-b" in prompt
    # The empty session should not appear in observed tool usage
    assert "issue-empty" not in prompt


def test_build_tools_prompt_no_tool_counts_shows_placeholder():
    """When a trace has no tool_counts, show '(no tool counts)'."""
    fake_sessions = [
        {"session_id": "issue-1", "trace_ids": ["trace-a"]},
    ]
    fake_trace = {"tool_counts": {}}
    fake_config = {"name": "agent_a", "tools": ["read_file"], "subagents": []}
    mock_md_path = MagicMock()
    mock_md_path.stem = "agent_a"

    with patch("cai.workflows.audit._TRACES") as mock_traces:
        mock_traces.list_solve_sessions.return_value = fake_sessions
        mock_traces.show_trace.return_value = fake_trace
        with patch("cai.workflows.audit.parse_agent_md", return_value=(fake_config, "")):
            with patch.object(Path, "glob", return_value=[mock_md_path]):
                prompt = _build_tools_prompt([])

    assert "(no tool counts)" in prompt


def test_build_tools_prompt_agent_no_tools_subagents_shows_none():
    """Agent without tools or subagents shows '(none)' for both."""
    fake_sessions = [
        {"session_id": "issue-1", "trace_ids": ["trace-a"]},
    ]
    fake_trace = {"tool_counts": {"read_file": 1}}
    fake_config = {
        "name": "minimal_agent",
        "tools": [],
        "subagents": [],
    }
    mock_md_path = MagicMock()
    mock_md_path.stem = "minimal_agent"

    with patch("cai.workflows.audit._TRACES") as mock_traces:
        mock_traces.list_solve_sessions.return_value = fake_sessions
        mock_traces.show_trace.return_value = fake_trace
        with patch("cai.workflows.audit.parse_agent_md", return_value=(fake_config, "")):
            with patch.object(Path, "glob", return_value=[mock_md_path]):
                prompt = _build_tools_prompt([])

    assert "tools=[(none)]" in prompt
    assert "subagents=[(none)]" in prompt


def test_build_tools_prompt_multiple_sessions_and_agents():
    """Multi-agent, multi-session scenario produces all expected sections."""
    fake_sessions = [
        {"session_id": "issue-1", "trace_ids": ["trace-a"]},
        {"session_id": "issue-2", "trace_ids": ["trace-b"]},
    ]
    trace_a = {"tool_counts": {"read_file": 5, "grep": 2}}
    trace_b = {"tool_counts": {"read_file": 3, "edit_file": 1}}

    agent_a = {"name": "agent_a", "tools": ["read_file", "grep", "edit_file"], "subagents": ["explore"]}
    agent_b = {"name": "agent_b", "tools": ["write_file", "bash"], "subagents": []}

    md_a = MagicMock()
    md_a.stem = "agent_a"
    md_b = MagicMock()
    md_b.stem = "agent_b"

    with patch("cai.workflows.audit._TRACES") as mock_traces:
        mock_traces.list_solve_sessions.return_value = fake_sessions
        mock_traces.show_trace.side_effect = [trace_a, trace_b]
        with patch(
            "cai.workflows.audit.parse_agent_md",
            side_effect=[(agent_a, ""), (agent_b, "")],
        ):
            with patch.object(Path, "glob", return_value=[md_a, md_b]):
                prompt = _build_tools_prompt([])

    # Both agents appear in declarations
    assert "**agent_a**: tools=[read_file, grep, edit_file], subagents=[explore]" in prompt
    assert "**agent_b**: tools=[write_file, bash], subagents=[(none)]" in prompt
    # Both sessions appear
    assert "### Session issue-1 (trace: `trace-a`)" in prompt
    assert "### Session issue-2 (trace: `trace-b`)" in prompt
    # Tool counts rendered
    assert "    5  read_file" in prompt
    assert "    2  grep" in prompt
    assert "    3  read_file" in prompt
    assert "    1  edit_file" in prompt
    # Declared Tools Never Observed: agent_b's tools (write_file, bash) never appear
    never_observed_section = prompt.split("## Declared Tools Never Observed")[1]
    assert "**agent_b**: write_file, bash" in never_observed_section


def test_build_tools_prompt_coverage_aggregates_across_sessions():
    """A tool observed in ANY session counts as observed for the 'never observed' check."""
    fake_sessions = [
        {"session_id": "issue-1", "trace_ids": ["trace-a"]},
        {"session_id": "issue-2", "trace_ids": ["trace-b"]},
    ]
    # write_file only appears in the second session
    trace_a = {"tool_counts": {"read_file": 1}}
    trace_b = {"tool_counts": {"write_file": 1}}

    agent = {"name": "agent_x", "tools": ["read_file", "write_file"], "subagents": []}
    md = MagicMock()
    md.stem = "agent_x"

    with patch("cai.workflows.audit._TRACES") as mock_traces:
        mock_traces.list_solve_sessions.return_value = fake_sessions
        mock_traces.show_trace.side_effect = [trace_a, trace_b]
        with patch("cai.workflows.audit.parse_agent_md", return_value=(agent, "")):
            with patch.object(Path, "glob", return_value=[md]):
                prompt = _build_tools_prompt([])

    never_observed_section = prompt.split("## Declared Tools Never Observed")[1]
    # Both tools observed across sessions, no missing
    assert "(all declared tools were observed" in never_observed_section
    # agent_x should not appear in the missing section
    assert "**agent_x**:" not in never_observed_section


def test_build_tools_prompt_sorts_declared_tools_by_name():
    """Agent .md files are processed in sorted order."""
    fake_sessions = [{"session_id": "issue-1", "trace_ids": ["trace-a"]}]
    fake_trace = {"tool_counts": {"read_file": 1}}
    md_b = MagicMock()
    md_b.stem = "beta"
    md_a = MagicMock()
    md_a.stem = "alpha"

    with patch("cai.workflows.audit._TRACES") as mock_traces:
        mock_traces.list_solve_sessions.return_value = fake_sessions
        mock_traces.show_trace.return_value = fake_trace
        with patch(
            "cai.workflows.audit.parse_agent_md",
            side_effect=[
                ({"name": "alpha", "tools": ["read_file"], "subagents": []}, ""),
                ({"name": "beta", "tools": ["read_file"], "subagents": []}, ""),
            ],
        ):
            with patch.object(Path, "glob", return_value=[md_b, md_a]):
                prompt = _build_tools_prompt([])

    # alpha should come before beta in the prompt
    alpha_pos = prompt.index("**alpha**")
    beta_pos = prompt.index("**beta**")
    assert alpha_pos < beta_pos


def test_build_tools_prompt_uses_stem_fallback_when_name_missing():
    """When config has no 'name' key, the md_path stem is used."""
    fake_sessions = [{"session_id": "issue-1", "trace_ids": ["trace-a"]}]
    fake_trace = {"tool_counts": {"read_file": 1}}
    # No "name" key in config
    fake_config = {"tools": ["read_file"], "subagents": []}
    mock_md_path = MagicMock()
    mock_md_path.stem = "stem_fallback"

    with patch("cai.workflows.audit._TRACES") as mock_traces:
        mock_traces.list_solve_sessions.return_value = fake_sessions
        mock_traces.show_trace.return_value = fake_trace
        with patch("cai.workflows.audit.parse_agent_md", return_value=(fake_config, "")):
            with patch.object(Path, "glob", return_value=[mock_md_path]):
                prompt = _build_tools_prompt([])

    assert "**stem_fallback**" in prompt


def test_build_tools_prompt_main_sections_present():
    """The prompt contains all expected structural sections."""
    fake_sessions = [{"session_id": "issue-1", "trace_ids": ["trace-a"]}]
    fake_trace = {"tool_counts": {"read_file": 1}}
    fake_config = {"name": "a", "tools": ["read_file"], "subagents": []}
    mock_md_path = MagicMock()
    mock_md_path.stem = "a"

    with patch("cai.workflows.audit._TRACES") as mock_traces:
        mock_traces.list_solve_sessions.return_value = fake_sessions
        mock_traces.show_trace.return_value = fake_trace
        with patch("cai.workflows.audit.parse_agent_md", return_value=(fake_config, "")):
            with patch.object(Path, "glob", return_value=[mock_md_path]):
                prompt = _build_tools_prompt([])

    assert "Audit per-agent tool usage across the last 10 issue-solving sessions." in prompt
    assert "## Agent Tool Declarations" in prompt
    assert "## Observed Tool Usage (per session, first trace)" in prompt
    assert "## Declared Tools Never Observed" in prompt
    assert "Delegate deep inspection of interesting traces to trace_analyst." in prompt
    assert "tools declared but never used" in prompt
    assert "tools used heavily that could shift to skills or bash" in prompt
    assert "tools an agent needs but doesn't declare" in prompt


def test_build_tools_prompt_tool_counts_sorted_by_count_desc():
    """Tool counts within a session are listed in descending count order."""
    fake_sessions = [{"session_id": "issue-1", "trace_ids": ["trace-a"]}]
    fake_trace = {"tool_counts": {"grep": 3, "read_file": 10, "edit_file": 5}}
    fake_config = {"name": "a", "tools": ["read_file", "grep", "edit_file"], "subagents": []}
    mock_md_path = MagicMock()
    mock_md_path.stem = "a"

    with patch("cai.workflows.audit._TRACES") as mock_traces:
        mock_traces.list_solve_sessions.return_value = fake_sessions
        mock_traces.show_trace.return_value = fake_trace
        with patch("cai.workflows.audit.parse_agent_md", return_value=(fake_config, "")):
            with patch.object(Path, "glob", return_value=[mock_md_path]):
                prompt = _build_tools_prompt([])

    # Extract the observed tool usage lines and verify descending order
    lines = prompt.splitlines()
    tool_lines = []
    in_tools_section = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("### Session"):
            in_tools_section = True
            continue
        if stripped.startswith("## Declared Tools"):
            break
        if in_tools_section and stripped and stripped[0].isdigit():
            tool_lines.append(stripped)

    counts = [int(line.split()[0]) for line in tool_lines]
    assert counts == sorted(counts, reverse=True)

