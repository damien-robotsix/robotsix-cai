import pytest
from cai.agents.loader import parse_agent_md, resolve_agent_path

def test_audit_agent_config():
    audit_file = resolve_agent_path("audit")
    assert audit_file.exists(), "audit.md must exist in AGENT_DIR"
    config, instructions = parse_agent_md(audit_file)
    
    # Assert basics
    assert config["name"] == "audit"
    assert config["model"] == "google/gemini-3.1-pro-preview"
    
    # Assert expected tools
    tools = config.get("tools", [])
    assert "subagents" in tools
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
    assert config["model"] == "google/gemini-3-flash-preview"
    # Assert description
    assert "description" in config
    assert "duplicate" in config["description"].lower()
    
    # Assert instructions
    assert "duplicate" in instructions.lower()
