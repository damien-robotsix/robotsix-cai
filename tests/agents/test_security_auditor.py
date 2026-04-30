import pytest
from cai.agents.loader import parse_agent_md, resolve_agent_path


def test_security_auditor_agent_config():
    """The security_auditor agent definition must exist and parse correctly."""
    sec_file = resolve_agent_path("security_auditor")
    assert sec_file.exists(), "security_auditor.md must exist in AGENT_DIR"
    config, instructions = parse_agent_md(sec_file)

    # Assert basics
    assert config["name"] == "security_auditor"
    assert config["model"] == "deepseek/deepseek-v4-pro"

    # Assert expected tools
    tools = config.get("tools", [])
    assert "filesystem_read" in tools
    assert "subagents" in tools

    # Assert subagents
    subagents = config.get("subagents", [])
    assert "explore" in subagents

    # Assert description
    assert "description" in config
    assert "vulnerability" in config["description"].lower()


def test_security_auditor_instructions_structure():
    """The security_auditor system prompt must include expected sections."""
    path = resolve_agent_path("security_auditor")
    _, instructions = parse_agent_md(path)

    # Required sections
    assert "# Security Auditor" in instructions
    assert "## How to work" in instructions
    assert "## What to look for" in instructions
    assert "## Confidence rubric" in instructions
    assert "## Output" in instructions

    # Key security lenses
    assert "Hardcoded secrets" in instructions
    assert "Unsafe subprocess" in instructions
    assert "Path traversal" in instructions
    assert "Command injection" in instructions
    assert "SQL injection" in instructions
    assert "eval" in instructions
    assert "exec" in instructions
    assert "Insecure deserialization" in instructions
    assert "Insecure tempfile" in instructions
    assert "Missing TLS" in instructions
    assert "Overly permissive" in instructions


def test_security_auditor_delegates_to_explore():
    """The security_auditor must instruct delegation to the explore subagent."""
    path = resolve_agent_path("security_auditor")
    _, instructions = parse_agent_md(path)

    assert "explore" in instructions.lower()
    assert "delegate" in instructions.lower()


def test_security_auditor_output_format():
    """The output section must reference AuditOutput and ProposedIssue."""
    path = resolve_agent_path("security_auditor")
    _, instructions = parse_agent_md(path)

    assert "AuditOutput" in instructions
    assert "ProposedIssue" in instructions
    assert "title" in instructions
    assert "body" in instructions
    assert "confidence" in instructions
    assert "last_detected_at" in instructions


def test_security_auditor_confidence_rubric_specialized():
    """The confidence rubric must contain security-specific anchors, not the generic default."""
    path = resolve_agent_path("security_auditor")
    _, instructions = parse_agent_md(path)

    # Security-specific anchors from the specialized rubric
    assert "confirmed by reading the file" in instructions.lower() or "confirmed the vulnerability by reading the file" in instructions.lower()
    assert "filesystem_read" in instructions
    assert "real injection vector" in instructions.lower() or "exploit vector" in instructions.lower()
    assert "speculative grep hit" in instructions.lower()

    # Must NOT contain the generic rubric placeholder from WithConfidence
    assert "Stake the next automated step on it" not in instructions
    assert "Tentative hypothesis based on a symptom" not in instructions


def test_security_auditor_last_detected_at_null():
    """The agent must instruct that last_detected_at should be left null."""
    path = resolve_agent_path("security_auditor")
    _, instructions = parse_agent_md(path)

    assert "leave null" in instructions.lower() or "null" in instructions.lower()


def test_security_auditor_empty_issue_list():
    """A conservative agent must return an empty issue list when nothing is worth fixing."""
    path = resolve_agent_path("security_auditor")
    _, instructions = parse_agent_md(path)

    assert "empty" in instructions.lower()
    assert "conservative" in instructions.lower()


def test_security_auditor_uses_filesystem_read():
    """The agent must use filesystem_read for deeper file inspection."""
    path = resolve_agent_path("security_auditor")
    _, instructions = parse_agent_md(path)

    assert "filesystem_read" in instructions


@pytest.mark.parametrize(
    "confidence_level,expected_text",
    [
        (10, "exploit vector"),
        (9, "one judgement call"),
        (7, "remediation design"),
        (5, "grep hit"),
        (1, "Speculative grep hit"),
    ],
)
def test_security_auditor_confidence_levels(confidence_level, expected_text):
    """Each confidence level in the rubric must contain the expected anchor text."""
    path = resolve_agent_path("security_auditor")
    _, instructions = parse_agent_md(path)

    # Find the confidence section
    rubric_start = instructions.find("## Confidence rubric")
    assert rubric_start >= 0, "Confidence rubric section not found"

    # The level marker like "**10**" or "- **10**"
    assert expected_text in instructions
