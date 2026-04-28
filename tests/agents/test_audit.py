import pytest
from cai.agents.loader import parse_agent_md, AGENT_DIR

def test_audit_agent_config():
    audit_file = AGENT_DIR / "audit.md"
    assert audit_file.exists(), "audit.md must exist in AGENT_DIR"
    config, instructions = parse_agent_md(audit_file)
    
    # Assert basics
    assert config["name"] == "audit"
    assert config["model"] == "anthropic/claude-3.5-sonnet"
    
    # Assert expected tools
    tools = config.get("tools", [])
    assert "traces_list" in tools
    assert "traces_show" in tools
    assert "traces_failures" in tools
    assert "traces_issue_cost" in tools
    
    # Assert description
    assert "description" in config
    assert "Analyzes Langfuse traces" in config["description"]
    
    # Assert instructions
    assert "Trace Analysis Agent" in instructions
