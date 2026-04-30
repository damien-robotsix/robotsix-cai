import pytest
from cai.agents.loader import parse_agent_md, resolve_agent_path


def test_deps_auditor_agent_config():
    """The deps_auditor agent definition must exist and parse correctly."""
    audit_file = resolve_agent_path("deps_auditor")
    assert audit_file.exists(), "deps_auditor.md must exist in AGENT_DIR"
    config, instructions = parse_agent_md(audit_file)

    # Assert basics
    assert config["name"] == "deps_auditor"
    assert config["model"] == "deepseek/deepseek-v4-pro"

    # Assert expected tools
    tools = config.get("tools", [])
    assert "filesystem_read" in tools
    assert "web_fetch" in tools
    assert "subagents" in tools

    # Assert subagents
    subagents = config.get("subagents", [])
    assert "explore" in subagents

    # Assert description
    assert "description" in config
    assert "dependency" in config["description"].lower()


def test_deps_auditor_instructions_structure():
    """The deps_auditor system prompt must include expected sections."""
    path = resolve_agent_path("deps_auditor")
    _, instructions = parse_agent_md(path)

    # Required sections
    assert "# Deps Auditor" in instructions
    assert "## How to work" in instructions
    assert "## What to look for" in instructions
    assert "## Confidence rubric" in instructions
    assert "## Output" in instructions

    # Key dependency-audit lenses
    assert "Breaking changes" in instructions
    assert "Deprecated APIs" in instructions
    assert "Security advisories" in instructions
    assert "Significant new features" in instructions
    assert "Compatibility constraints" in instructions


def test_deps_auditor_delegates_to_explore():
    """The deps_auditor must instruct delegation to the explore subagent."""
    path = resolve_agent_path("deps_auditor")
    _, instructions = parse_agent_md(path)

    assert "explore" in instructions.lower()
    assert "delegate" in instructions.lower()


def test_deps_auditor_output_format():
    """The output section must reference AuditOutput and ProposedIssue."""
    path = resolve_agent_path("deps_auditor")
    _, instructions = parse_agent_md(path)

    assert "AuditOutput" in instructions
    assert "ProposedIssue" in instructions
    assert "title" in instructions
    assert "body" in instructions
    assert "confidence" in instructions
    assert "last_detected_at" in instructions


def test_deps_auditor_confidence_rubric_specialized():
    """The confidence rubric must contain dependency-specific anchors, not the generic default."""
    path = resolve_agent_path("deps_auditor")
    _, instructions = parse_agent_md(path)

    # Dependency-specific anchors from the specialized rubric
    assert "changelog explicitly lists a breaking change" in instructions
    assert "one judgement call" in instructions
    assert "Safe to auto-dispatch" in instructions

    # Must NOT contain the generic rubric placeholder from WithConfidence
    assert "Stake the next automated step on it" not in instructions
    assert "Tentative hypothesis based on a symptom" not in instructions


def test_deps_auditor_last_detected_at_null():
    """The agent must instruct that last_detected_at should be left null."""
    path = resolve_agent_path("deps_auditor")
    _, instructions = parse_agent_md(path)

    assert "leave null" in instructions.lower() or "null" in instructions.lower()


def test_deps_auditor_empty_issue_list():
    """A conservative agent must return an empty issue list when nothing is worth upgrading."""
    path = resolve_agent_path("deps_auditor")
    _, instructions = parse_agent_md(path)

    assert "empty" in instructions.lower()
    assert "conservative" in instructions.lower()


def test_deps_auditor_uses_filesystem_read():
    """The agent must use filesystem_read for deeper file inspection."""
    path = resolve_agent_path("deps_auditor")
    _, instructions = parse_agent_md(path)

    assert "filesystem_read" in instructions
    assert "Inspect" in instructions


def test_deps_auditor_uses_web_fetch():
    """The agent must use web_fetch to read upstream changelog pages."""
    path = resolve_agent_path("deps_auditor")
    _, instructions = parse_agent_md(path)

    assert "web_fetch" in instructions


def test_deps_auditor_no_trivial_patch_bumps():
    """The agent must warn against filing issues for every outdated package."""
    path = resolve_agent_path("deps_auditor")
    _, instructions = parse_agent_md(path)

    assert "patch bump" in instructions.lower() or "patch bumps" in instructions.lower()
    assert "noisy" in instructions.lower()


@pytest.mark.parametrize(
    "confidence_level,expected_text",
    [
        (10, "changelog explicitly lists a breaking change"),
        (9, "one judgement call"),
        (7, "Real update with plausible impact"),
        (5, "Version gap exists but impact is unclear"),
        (1, "Trivial or patch bump"),
    ],
)
def test_deps_auditor_confidence_levels(confidence_level, expected_text):
    """Each confidence level in the rubric must contain the expected anchor text."""
    path = resolve_agent_path("deps_auditor")
    _, instructions = parse_agent_md(path)

    # Find the confidence section
    rubric_start = instructions.find("## Confidence rubric")
    assert rubric_start >= 0, "Confidence rubric section not found"

    assert expected_text in instructions
