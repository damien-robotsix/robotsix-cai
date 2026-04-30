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
    assert "vulnerabilit" in config["description"].lower()


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
    assert "Hardcoded credentials" in instructions
    assert "Unsafe subprocess" in instructions
    assert "Path traversal" in instructions
    assert "injection" in instructions.lower()
    assert "eval" in instructions.lower()
    assert "deserialization" in instructions.lower()
    assert "TLS" in instructions or "certificate verification" in instructions.lower()
    assert "cryptography" in instructions.lower()


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
    """The confidence rubric must contain security-specific anchors."""
    path = resolve_agent_path("security_auditor")
    _, instructions = parse_agent_md(path)

    # Security-specific anchors from the specialized rubric
    assert "filesystem_read" in instructions
    assert "reachable from untrusted input" in instructions
    assert "Safe to auto-dispatch" in instructions

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
    """The agent must use filesystem_read for file inspection."""
    path = resolve_agent_path("security_auditor")
    _, instructions = parse_agent_md(path)

    assert "filesystem_read" in instructions
    assert "inspect" in instructions.lower()


@pytest.mark.parametrize(
    "confidence_level,expected_text",
    [
        (10, "inspected the code with"),
        (9, "one judgement call"),
        (7, "Real vulnerability"),
        (5, "Plausible vulnerability pattern"),
        (1, "Speculative observation"),
    ],
)
def test_security_auditor_confidence_levels(confidence_level, expected_text):
    """Each confidence level in the rubric must contain the expected anchor text."""
    path = resolve_agent_path("security_auditor")
    _, instructions = parse_agent_md(path)

    # Find the confidence section
    rubric_start = instructions.find("## Confidence rubric")
    assert rubric_start >= 0, "Confidence rubric section not found"

    assert expected_text in instructions
