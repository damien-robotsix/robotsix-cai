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

    # Assert expected skills (TOOL_FLAGS)
    skills = config.get("skills", [])
    assert "filesystem" in skills


def test_docs_agent_prompt_includes_edit_instructions():
    """The system prompt instructs the model to use write_file or edit_file
    to make actual documentation changes."""
    docs_file = resolve_agent_path("docs")
    _, system_prompt = parse_agent_md(docs_file)

    assert "write_file" in system_prompt
    assert "edit_file" in system_prompt
    assert "Editing strategy" in system_prompt


def test_docs_agent_prompt_includes_files_changed():
    """The system prompt lists files_changed as a return field."""
    docs_file = resolve_agent_path("docs")
    _, system_prompt = parse_agent_md(docs_file)

    assert "files_changed" in system_prompt


def test_docs_agent_prompt_states_return_fields():
    """The system prompt explicitly lists summary, commit_message, and
    files_changed as return values."""
    docs_file = resolve_agent_path("docs")
    _, system_prompt = parse_agent_md(docs_file)

    assert "- `summary`" in system_prompt
    assert "- `commit_message`" in system_prompt
    assert "- `files_changed`" in system_prompt


def test_docs_agent_prompt_includes_guidelines():
    """The system prompt includes the ## Guidelines section."""
    docs_file = resolve_agent_path("docs")
    _, system_prompt = parse_agent_md(docs_file)

    assert "## Guidelines" in system_prompt
    assert "Focus purely on the `docs/` folder" in system_prompt
    assert "Prefer extending an existing page" in system_prompt


def test_docs_agent_prompt_what_you_receive_includes_reference_files():
    """The system prompt's 'What you receive' section mentions reference
    files."""
    docs_file = resolve_agent_path("docs")
    _, system_prompt = parse_agent_md(docs_file)

    assert "## What you receive" in system_prompt
    assert "Reference files" in system_prompt
    assert "refine agent flagged as required reading" in system_prompt


def test_docs_agent_prompt_how_to_work_step3_names_tools():
    """Step 3 of 'How to work' explicitly names write_file and edit_file."""
    docs_file = resolve_agent_path("docs")
    _, system_prompt = parse_agent_md(docs_file)

    assert "3. Use `write_file` or `edit_file`" in system_prompt


def test_docs_agent_prompt_has_output_section():
    """The system prompt has an ## Output section with return fields, not
    an 'Output format' section."""
    docs_file = resolve_agent_path("docs")
    _, system_prompt = parse_agent_md(docs_file)

    assert "## Output" in system_prompt
    assert "## Output format" not in system_prompt
