import pytest
from cai.agents.loader import parse_agent_md, AGENT_DIR

def test_implement_agent_config():
    implement_file = AGENT_DIR / "implement.md"
    assert implement_file.exists(), "implement.md must exist in AGENT_DIR"
    config, instructions = parse_agent_md(implement_file)
    
    # Assert expectations for tools
    tools = config.get("tools", [])
    assert "web_search" in tools
    assert "web_fetch" in tools

def test_refine_agent_config():
    refine_file = AGENT_DIR / "refine.md"
    assert refine_file.exists(), "refine.md must exist in AGENT_DIR"
    config, instructions = parse_agent_md(refine_file)
    
    # Assert expectations for tools
    tools = config.get("tools", [])
    assert "web_search" in tools
    assert "web_fetch" in tools
