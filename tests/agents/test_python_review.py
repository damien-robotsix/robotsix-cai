from cai.agents.loader import parse_agent_md, resolve_agent_path


def test_python_review_agent_config():
    """Verify python_review frontmatter config matches expected values."""
    pr_file = resolve_agent_path("python_review")
    assert pr_file.exists(), "python_review.md must exist in AGENT_DIR"
    config, instructions = parse_agent_md(pr_file)

    # Name
    assert config["name"] == "python_review"

    # Model
    assert config["model"] == "deepseek/deepseek-v4-pro"

    # Tools
    tools = config.get("tools", [])
    assert "filesystem" in tools
    assert "raise_issue" in tools

    # Description
    assert "description" in config
    assert "Python" in config["description"]
    assert "review" in config["description"].lower()

    # Key instructions content
    assert "## Review rubric" in instructions
    assert "## Severity levels" in instructions
    assert "leaves `commit_message` empty" in instructions

    # Verify avoid-re-reading guidance
    assert "**Avoid re-reading:**" in instructions, (
        "python_review.md must contain 'Avoid re-reading' guidance"
    )
    assert (
        "before calling `read_file`, check your conversation history"
        in instructions
    ), "python_review.md must instruct agent to check conversation history before read_file"
