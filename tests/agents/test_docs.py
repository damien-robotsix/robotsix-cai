from cai.agents.loader import parse_agent_md, resolve_agent_path


def test_docs_agent_config():
    """docs.md frontmatter contains expected name, model, and tools."""
    docs_file = resolve_agent_path("docs")
    assert docs_file.exists(), "docs.md must exist in AGENT_DIR"
    config, instructions = parse_agent_md(docs_file)

    assert config["name"] == "docs"
    assert config["description"] == (
        "Reviews implementation changes and updates documentation in the docs/ folder."
    )

    # Assert expected tools
    tools = config.get("tools", [])
    assert "filesystem" in tools


def test_docs_agent_prompt_includes_output_format_section():
    """The system prompt must include a dedicated ## Output format section."""
    docs_file = resolve_agent_path("docs")
    _, system_prompt = parse_agent_md(docs_file)

    assert "## Output format" in system_prompt


def test_docs_agent_prompt_includes_raw_json_instruction():
    """The system prompt instructs the model to return only a raw JSON object
    with no markdown fences."""
    docs_file = resolve_agent_path("docs")
    _, system_prompt = parse_agent_md(docs_file)

    assert "no markdown fences" in system_prompt
    assert "raw JSON object" in system_prompt


def test_docs_agent_prompt_includes_json_example():
    """The system prompt includes a JSON code block showing an example
    of the exact summary + commit_message shape the model must return."""
    docs_file = resolve_agent_path("docs")
    _, system_prompt = parse_agent_md(docs_file)

    # The JSON example block uses a ```json fence and contains both
    # fields with realistic-looking values.
    assert "```json" in system_prompt
    assert '"summary"' in system_prompt
    assert '"commit_message"' in system_prompt
    assert "langfuse-server" in system_prompt
    assert "--timeout" in system_prompt


def test_docs_agent_prompt_states_return_fields():
    """The system prompt explicitly lists summary and commit_message as return values."""
    docs_file = resolve_agent_path("docs")
    _, system_prompt = parse_agent_md(docs_file)

    assert "- `summary`" in system_prompt
    assert "- `commit_message`" in system_prompt


def test_docs_agent_prompt_includes_guidelines():
    """The system prompt includes the ## Guidelines section."""
    docs_file = resolve_agent_path("docs")
    _, system_prompt = parse_agent_md(docs_file)

    assert "## Guidelines" in system_prompt
    assert "Focus purely on the `docs/` folder" in system_prompt
    assert "Prefer extending an existing page" in system_prompt
