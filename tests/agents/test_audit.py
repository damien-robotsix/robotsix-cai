import pytest
from cai.agents.loader import parse_agent_md, resolve_agent_path

def test_audit_agent_config():
    audit_file = resolve_agent_path("audit")
    assert audit_file.exists(), "audit.md must exist in AGENT_DIR (recursively)"
    config, instructions = parse_agent_md(audit_file)
    
    # Assert basics
    assert config["name"] == "audit"
    assert config["model"] == "anthropic/claude-3.5-sonnet"
    
    # Assert expected tools
    tools = config.get("tools", [])
    assert "traces_list" in tools
    assert "traces_failures" in tools
    assert "traces_issue_cost" in tools
    assert "subagents" in config
    
    # Assert description
    assert "description" in config
    assert "Analyzes Langfuse traces" in config["description"]
    
    # Assert instructions
    assert "Trace Analysis Agent" in instructions


def test_issue_deduplicator_agent_config():
    dedupe_file = AGENT_DIR / "issue_deduplicator.md"
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
