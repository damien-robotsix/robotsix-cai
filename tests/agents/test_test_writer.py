from cai.agents.loader import parse_agent_md, resolve_agent_path


def test_test_writer_agent_config():
    """Verify test_writer frontmatter config matches expected values."""
    tw_file = resolve_agent_path("test_writer")
    assert tw_file.exists(), "test_writer.md must exist in AGENT_DIR"
    config, instructions = parse_agent_md(tw_file)

    # Name
    assert config["name"] == "test_writer"

    # Model — must be the flash (non-reasoning) variant, not the expensive pro
    assert config["model"] == "deepseek/deepseek-v4-flash"

    # Tools
    tools = config.get("tools", [])
    assert "filesystem" in tools
    assert "git_log" in tools, "test_writer should have read-only git tools"
    assert "git_diff" in tools
    assert "git_blame" in tools
    assert "git_show" in tools

    # Description
    assert "description" in config
    assert "pytest" in config["description"]
    assert "unit tests" in config["description"].lower()
    assert "LLM" in config["description"]

    # Key instructions content
    assert "No LLM calls" in instructions
    assert "No external services" in instructions
    assert "Pure pytest" in instructions
    assert "pytest.mark.parametrize" in instructions

    # Batched workflow guidance (added to reduce LLM call overhead)
    assert "Plan first" in instructions
    assert "Read all relevant files in parallel" in instructions
    assert "batch all test additions" in instructions
    assert "Paginate large files" in instructions
    assert "Read each file once" in instructions


def test_test_writer_model_is_not_pro():
    """Regression test: test_writer must NOT use the expensive reasoning model."""
    tw_file = resolve_agent_path("test_writer")
    config, _ = parse_agent_md(tw_file)
    assert config["model"] != "deepseek/deepseek-v4-pro", (
        "test_writer should use deepseek-v4-flash, not the expensive pro reasoning model"
    )


def test_test_writer_tools_includes_git_tools():
    """test_writer has read-only git tools in its tool list."""
    from cai.agents.loader import parse_agent_md, resolve_agent_path

    config, _ = parse_agent_md(resolve_agent_path("test_writer"))
    tools = config.get("tools", [])
    assert "git_log" in tools
    assert "git_diff" in tools
    assert "git_blame" in tools
    assert "git_show" in tools


def test_test_writer_prompt_includes_git_history_guidance():
    """test_writer instructions include the Git history note about git tools."""
    from cai.agents.loader import parse_agent_md, resolve_agent_path

    _, instructions = parse_agent_md(resolve_agent_path("test_writer"))
    assert "**Git history:**" in instructions
    assert "git_log" in instructions
    assert "git_diff" in instructions
    assert "git_blame" in instructions
    assert "git_show" in instructions
    assert "execute does not exist" in instructions or "`execute` does not exist" in instructions


def test_test_writer_git_history_note_discourages_execute():
    """The Git history note steers away from execute('git ...') toward the git tools."""
    from cai.agents.loader import parse_agent_md, resolve_agent_path

    _, instructions = parse_agent_md(resolve_agent_path("test_writer"))
    assert "execute('git" in instructions or "execute" in instructions
    # The note should prefer git_log over hallucinated execute
    assert "prefer these over" in instructions


def test_test_writer_prompt_includes_avoid_rereading_guidance():
    """Verify test_writer.md contains avoid-re-reading guidance."""
    tw_file = resolve_agent_path("test_writer")
    _, instructions = parse_agent_md(tw_file)

    assert "**Avoid re-reading:**" in instructions, (
        "test_writer.md must contain 'Avoid re-reading' guidance"
    )
    assert (
        "before calling `read_file`, check your conversation history"
        in instructions
    ), "test_writer.md must instruct agent to check conversation history before read_file"
    assert (
        "Only re-read when you need data from an unread range or the file may have changed"
        in instructions
    )


def test_test_writer_prompt_includes_editing_strategy():
    """Verify test_writer.md contains the editing strategy section with
    edit_file disambiguation guidance."""
    tw_file = resolve_agent_path("test_writer")
    _, instructions = parse_agent_md(tw_file)

    assert "## Editing strategy" in instructions, (
        "test_writer.md must contain an 'Editing strategy' section"
    )
    assert "Read files before editing" in instructions, (
        "test_writer.md must instruct agents to read files before editing"
    )
    assert "Copy the exact target lines" in instructions, (
        "test_writer.md must instruct agents to copy exact lines into old_string"
    )
    assert "Disambiguate" in instructions, (
        "test_writer.md must include disambiguation guidance for old_string"
    )
    assert "above AND below" in instructions, (
        "test_writer.md must require surrounding context above AND below the target"
    )
    assert "batch all edits" in instructions, (
        "test_writer.md must encourage batching multiple edits in a single response"
    )
