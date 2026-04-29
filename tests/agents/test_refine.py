import pytest
from cai.agents.loader import parse_agent_md, resolve_agent_path

def test_refine_agent_config():
    refine_file = resolve_agent_path("refine")
    assert refine_file.exists(), "refine.md must exist in AGENT_DIR"
    config, instructions = parse_agent_md(refine_file)
    
    # Assert basics
    assert config["name"] == "refine"
    assert config["model"] == "google/gemini-3.1-pro-preview"
    
    # Assert expected tools
    tools = config.get("tools", [])
    assert "filesystem" in tools
    assert "subagents" in tools
    assert "web_search" in tools
    assert "web_fetch" in tools
    
    # Assert subagents
    subagents = config.get("subagents", [])
    assert "explore" in subagents
    assert "spike" in subagents
    
    # Assert instructions
    assert "web_search" in instructions
    assert "web_fetch" in instructions
