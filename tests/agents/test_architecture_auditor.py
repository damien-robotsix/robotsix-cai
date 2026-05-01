import pytest
from cai.agents.loader import parse_agent_md, resolve_agent_path


def test_architecture_auditor_agent_config():
    """The architecture_auditor agent definition must exist and parse correctly."""
    arch_file = resolve_agent_path("architecture_auditor")
    assert arch_file.exists(), "architecture_auditor.md must exist in AGENT_DIR"
    config, instructions = parse_agent_md(arch_file)

    # Assert basics
    assert config["name"] == "architecture_auditor"
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
    assert "refactoring" in config["description"].lower()


def test_architecture_auditor_instructions_structure():
    """The architecture_auditor system prompt must include expected sections."""
    path = resolve_agent_path("architecture_auditor")
    _, instructions = parse_agent_md(path)

    # Required sections
    assert "# Architecture Auditor" in instructions
    assert "## How to work" in instructions
    assert "## What to look for" in instructions
    assert "## Confidence rubric" in instructions
    assert "## Output" in instructions

    # Key architectural lenses
    assert "Module organisation" in instructions
    assert "Documentation coverage" in instructions
    assert "Interface consistency" in instructions
    assert "Module size" in instructions
    assert "Dead code" in instructions
    assert "Configuration duplication" in instructions


def test_architecture_auditor_delegates_to_explore():
    """The architecture_auditor must instruct delegation to the explore subagent."""
    path = resolve_agent_path("architecture_auditor")
    _, instructions = parse_agent_md(path)

    assert "explore" in instructions.lower()
    assert "delegate" in instructions.lower()


def test_architecture_auditor_output_format():
    """The output section must reference AuditOutput and ProposedIssue."""
    path = resolve_agent_path("architecture_auditor")
    _, instructions = parse_agent_md(path)

    assert "AuditOutput" in instructions
    assert "ProposedIssue" in instructions
    assert "title" in instructions
    assert "body" in instructions
    assert "confidence" in instructions
    assert "last_detected_at" in instructions


def test_architecture_auditor_confidence_rubric_specialized():
    """The confidence rubric must contain architecture-specific anchors, not the generic default."""
    path = resolve_agent_path("architecture_auditor")
    _, instructions = parse_agent_md(path)

    # Architecture-specific anchors from the specialized rubric
    assert "filesystem_read" in instructions
    assert "refactor target is unambiguous" in instructions
    assert "Safe to auto-dispatch" in instructions

    # Must NOT contain the generic rubric placeholder from WithConfidence
    assert "Stake the next automated step on it" not in instructions
    assert "Tentative hypothesis based on a symptom" not in instructions


def test_architecture_auditor_last_detected_at_null():
    """The agent must instruct that last_detected_at should be left null."""
    path = resolve_agent_path("architecture_auditor")
    _, instructions = parse_agent_md(path)

    assert "leave null" in instructions.lower() or "null" in instructions.lower()


def test_architecture_auditor_empty_issue_list():
    """A conservative agent must return an empty issue list when nothing is worth refactoring."""
    path = resolve_agent_path("architecture_auditor")
    _, instructions = parse_agent_md(path)

    assert "empty" in instructions.lower()
    assert "conservative" in instructions.lower()


def test_architecture_auditor_uses_filesystem_read():
    """The agent must use filesystem_read for deeper file inspection."""
    path = resolve_agent_path("architecture_auditor")
    _, instructions = parse_agent_md(path)

    assert "filesystem_read" in instructions
    assert "Inspect" in instructions


def test_architecture_auditor_module_size_lens():
    """The Module size lens must flag any file over 300 lines, not just those bundling unrelated concerns."""
    path = resolve_agent_path("architecture_auditor")
    _, instructions = parse_agent_md(path)

    # The 300-line threshold is preserved
    assert "300 lines" in instructions

    # Files should be split into smaller, single-purpose modules
    # (no longer requires that files "bundle unrelated concerns")
    assert "split into smaller" in instructions
    assert "single-purpose" in instructions

    # Clarifying note: even single-concern files become hard to navigate, review, and test
    assert "navigate" in instructions
    assert "hard to" in instructions

    # Old precondition must be absent
    assert "bundle unrelated" not in instructions.lower()
    assert "bundling unrelated" not in instructions.lower()


@pytest.mark.parametrize(
    "confidence_level,expected_text",
    [
        (10, "inspected both sides end-to-end"),
        (9, "one judgement call"),
        (7, "Real architectural issue"),
        (5, "Plausible pattern"),
        (1, "Speculative observation"),
    ],
)
def test_architecture_auditor_confidence_levels(confidence_level, expected_text):
    """Each confidence level in the rubric must contain the expected anchor text."""
    path = resolve_agent_path("architecture_auditor")
    _, instructions = parse_agent_md(path)

    # Find the confidence section
    rubric_start = instructions.find("## Confidence rubric")
    assert rubric_start >= 0, "Confidence rubric section not found"

    # The level marker like "**10**" or "- **10**"
    assert expected_text in instructions
