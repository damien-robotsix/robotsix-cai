import pytest
from cai.agents.loader import parse_agent_md, resolve_agent_path

def test_audit_agent_config():
    audit_file = resolve_agent_path("audit")
    assert audit_file.exists(), "audit.md must exist in AGENT_DIR"
    config, instructions = parse_agent_md(audit_file)
    
    # Assert basics
    assert config["name"] == "audit"
    assert config["model"] == "deepseek/deepseek-v4-pro"
    
    # Assert expected skills (TOOL_FLAGS)
    skills = config.get("skills", [])
    assert "subagents" in skills
    subagents = config.get("subagents", [])
    assert "trace_analyst" in subagents

    # Assert description
    assert "description" in config
    assert "Langfuse" in config["description"]

    # Assert instructions
    assert "Trace Analysis Agent" in instructions


def test_issue_deduplicator_agent_config():
    dedupe_file = resolve_agent_path("issue_deduplicator")
    assert dedupe_file.exists(), "issue_deduplicator.md must exist in AGENT_DIR"
    config, instructions = parse_agent_md(dedupe_file)
    
    # Assert basics
    assert config["name"] == "Issue Deduplicator"
    assert config["model"] == "deepseek/deepseek-v4-flash"
    # Assert description
    assert "description" in config
    assert "duplicate" in config["description"].lower()
    
    # Assert instructions
    assert "duplicate" in instructions.lower()


def test_architecture_auditor_agent_config():
    arch_file = resolve_agent_path("architecture_auditor")
    assert arch_file.exists(), "architecture_auditor.md must exist in AGENT_DIR"
    config, instructions = parse_agent_md(arch_file)

    # Assert basics
    assert config["name"] == "architecture_auditor"
    assert config["model"] == "deepseek/deepseek-v4-pro"

    # Assert expected skills (TOOL_FLAGS)
    skills = config.get("skills", [])
    assert "filesystem_read" in skills
    assert "subagents" in skills

    # Assert expected subagents
    subagents = config.get("subagents", [])
    assert "explore" in subagents

    # Assert description
    assert "description" in config

    # Assert instructions contain a key phrase
    instructions_lower = instructions.lower()
    assert any(
        phrase in instructions_lower
        for phrase in ("refactoring", "architecture", "structure")
    )
