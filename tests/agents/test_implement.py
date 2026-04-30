import pytest
from cai.agents.loader import parse_agent_md, resolve_agent_path


def test_implement_agent_config():
    implement_file = resolve_agent_path("implement")
    assert implement_file.exists(), "implement.md must exist in AGENT_DIR"
    config, instructions = parse_agent_md(implement_file)

    # Assert basics
    assert config["name"] == "implement"
    assert config["model"] == "deepseek/deepseek-v4-pro"

    # Assert expected tools
    tools = config.get("tools", [])
    assert "filesystem" in tools
    assert "web_search" in tools
    assert "web_fetch" in tools

    # Assert instructions
    assert "web_search" in instructions
    assert "web_fetch" in instructions
    assert "API documentation" in instructions

    # Assert specific rules
    assert (
        r"Do not run repository-wide global searches (like \`grep\` or \`glob\`)"
    ) in instructions
    assert "post-refactor to verify changes" in instructions

    # Assert re-read-after-edit guidance is present (issue #1525)
    assert (
        "re-read it before constructing `old_string`"
        in instructions
    ), "Prompt must warn that reference files diverge from disk after edits"
    assert (
        "construct `old_string` from the fresh read, not from memory or the initial snapshot"
        in instructions
    ), "Prompt must instruct agent to build old_string from a fresh re-read"
    assert (
        "re-read the file before each new batch"
        in instructions
    ), "Prompt must instruct re-reading between multi-response edit batches"
