from cai.agents.loader import parse_agent_md, resolve_agent_path

def test_refine_agent_config():
    refine_file = resolve_agent_path("refine")
    assert refine_file.exists(), "refine.md must exist in AGENT_DIR"
    config, instructions = parse_agent_md(refine_file)
    
    # Assert basics
    assert config["name"] == "refine"
    assert config["model"] == "deepseek/deepseek-v4-pro"
    
    # Assert expected tools
    tools = config.get("tools", [])
    assert "filesystem" in tools
    assert "subagents" in tools
    assert "web_search" in tools
    assert "web_fetch" in tools
    assert "traces_list" in tools
    assert "traces_show" in tools
    assert "traces_failures" in tools
    assert "traces_session" in tools
    assert "traces_solve_sessions" in tools
    assert "context_manager" in tools
    assert "history_archive" in tools
    
    # Assert subagents
    subagents = config.get("subagents", [])
    assert "explore" in subagents
    assert "spike" in subagents
    assert "trace_analyst" in subagents
    
    # Assert instructions reference trace tools and subagent
    assert "web_search" in instructions
    assert "web_fetch" in instructions
    assert "trace_analyst" in instructions
    assert "traces_session" in instructions
    assert "traces_solve_sessions" in instructions
    
    # Assert verification template updates
    assert "grep for Y" not in instructions
    assert "check that modified file Z looks like" in instructions

    # Assert context management instructions
    assert "## Context management" in instructions
    assert "Write intermediate research findings" in instructions
    assert "context_manager" in instructions
    assert "history_archive" in instructions
