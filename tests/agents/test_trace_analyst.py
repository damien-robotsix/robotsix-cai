from cai.agents.loader import parse_agent_md, resolve_agent_path


def test_trace_analyst_agent_config():
    """Verify trace_analyst frontmatter config matches expected values."""
    tw_file = resolve_agent_path("trace_analyst")
    assert tw_file.exists(), "trace_analyst.md must exist in AGENT_DIR"
    config, instructions = parse_agent_md(tw_file)

    # Name
    assert config["name"] == "trace_analyst"

    # Model — must be the flash (non-reasoning) variant
    assert config["model"] == "deepseek/deepseek-v4-flash"

    # Tools
    tools = config.get("tools", [])
    assert "filesystem_read" in tools
    assert "traces_show" in tools
    assert "file_info" in tools

    # Common sections
    common = config.get("common", [])
    assert "anti_hallucination_guard" in common
    assert "antipattern_examples" in common


def test_trace_analyst_prompt_includes_trace_data_first_warning():
    """Verify trace_analyst.md contains the 'Trace data first' warning block."""
    tw_file = resolve_agent_path("trace_analyst")
    _, instructions = parse_agent_md(tw_file)

    assert "> **Trace data first:**" in instructions, (
        "trace_analyst.md must contain the 'Trace data first' warning"
    )
    assert (
        "traces often reference ephemeral temp directories"
        in instructions
    ), "trace_analyst.md must warn about ephemeral temp directories"
    assert (
        "Analyze the trace data you already have via `traces_show`"
        in instructions
    ), "trace_analyst.md must instruct to analyze pre-fetched trace data"


def test_trace_analyst_prompt_includes_filesystem_budget():
    """Verify trace_analyst.md has the filesystem exploration budget."""
    tw_file = resolve_agent_path("trace_analyst")
    _, instructions = parse_agent_md(tw_file)

    assert "Filesystem exploration budget" in instructions, (
        "trace_analyst.md must contain a filesystem exploration budget"
    )
    assert "at most 1 `ls`" in instructions, (
        "trace_analyst.md must limit ls calls to at most 1"
    )
    assert "at most 2 `grep`" in instructions, (
        "trace_analyst.md must limit grep calls to at most 2"
    )
    assert (
        "stop filesystem exploration entirely" in instructions
    ), "trace_analyst.md must instruct to stop exploring when path doesn't exist"


def test_trace_analyst_prompt_includes_step_2a_filesystem_last_resort():
    """Verify the 'Filesystem is a last resort' step 2a is present."""
    tw_file = resolve_agent_path("trace_analyst")
    _, instructions = parse_agent_md(tw_file)

    assert "2a. **Filesystem is a last resort:**" in instructions, (
        "trace_analyst.md must contain step 2a about filesystem being a last resort"
    )
    assert (
        "The `traces_show` output already contains"
        in instructions
    ), "trace_analyst.md must explain why traces_show is the primary source"
    assert (
        "Only explore the filesystem when the parent agent has explicitly told you"
        in instructions
    ), "trace_analyst.md must limit filesystem exploration to instructed cases"
    assert (
        "confirm the path exists with `ls` first"
        in instructions
    ), "trace_analyst.md must require ls verification before filesystem exploration"


def test_trace_analyst_model_is_not_pro():
    """Regression test: trace_analyst must NOT use the expensive reasoning model."""
    tw_file = resolve_agent_path("trace_analyst")
    config, _ = parse_agent_md(tw_file)
    assert config["model"] != "deepseek/deepseek-v4-pro", (
        "trace_analyst should use deepseek-v4-flash, not the expensive pro reasoning model"
    )


def test_trace_analyst_prompt_does_not_contain_grep_truncation_note():
    """The grep truncation note from explore.md must NOT appear in trace_analyst.md."""
    tw_file = resolve_agent_path("trace_analyst")
    _, instructions = parse_agent_md(tw_file)

    assert "> **grep truncation:**" not in instructions, (
        "trace_analyst.md must NOT contain the grep-truncation note inherited from explore.md"
    )
    assert "grep output is truncated" not in instructions, (
        "trace_analyst.md must not contain grep truncation guidance"
    )
