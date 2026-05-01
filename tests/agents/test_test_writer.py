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


def test_test_writer_model_is_not_pro():
    """Regression test: test_writer must NOT use the expensive reasoning model."""
    tw_file = resolve_agent_path("test_writer")
    config, _ = parse_agent_md(tw_file)
    assert config["model"] != "deepseek/deepseek-v4-pro", (
        "test_writer should use deepseek-v4-flash, not the expensive pro reasoning model"
    )
