import pytest
from cai.agents.loader import resolve_agent_path

def test_resolve_agent_path(tmp_path, monkeypatch):
    import cai.agents.loader
    monkeypatch.setattr(cai.agents.loader, "AGENT_DIR", tmp_path)
    
    # Create agents in different directories
    (tmp_path / "foo.md").touch()
    (tmp_path / "bar").mkdir()
    (tmp_path / "bar" / "baz.md").touch()
    
    assert resolve_agent_path("foo") == tmp_path / "foo.md"
    assert resolve_agent_path("baz") == tmp_path / "bar" / "baz.md"
    
def test_resolve_agent_path_not_found(tmp_path, monkeypatch):
    import cai.agents.loader
    monkeypatch.setattr(cai.agents.loader, "AGENT_DIR", tmp_path)
    
    with pytest.raises(FileNotFoundError, match="agent definition not found: qux.md"):
        resolve_agent_path("qux")
        
def test_resolve_agent_path_ambiguous(tmp_path, monkeypatch):
    import cai.agents.loader
    monkeypatch.setattr(cai.agents.loader, "AGENT_DIR", tmp_path)
    
    (tmp_path / "foo.md").touch()
    (tmp_path / "bar").mkdir()
    (tmp_path / "bar" / "foo.md").touch()
    
    with pytest.raises(ValueError, match="ambiguous agent name, found multiple foo.md:"):
        resolve_agent_path("foo")

def test_resolve_subagents(tmp_path, monkeypatch):
    import cai.agents.loader
    monkeypatch.setattr(cai.agents.loader, "AGENT_DIR", tmp_path)
    
    # Create main agent and a couple of subagents
    (tmp_path / "main.md").write_text("---\nname: main\nsubagents:\n  - sub1\n  - sub2\n---\nMain agent")
    (tmp_path / "subagents").mkdir()
    (tmp_path / "subagents" / "sub1.md").write_text("---\nname: sub1\ndescription: A subagent\nmodel: dummy\n---\nhello sub1")
    (tmp_path / "deep").mkdir()
    (tmp_path / "deep" / "sub2.md").write_text("---\nname: sub2\ndescription: Another subagent\nmodel: dummy\n---\nhello sub2")
    
    # Mock build_deep_agent to avoid actually building agents (which requires OpenRouter keys etc)
    def fake_build(config, text):
        return f"built {config['name']}"
    monkeypatch.setattr(cai.agents.loader, "build_deep_agent", fake_build)
    
    config = {"subagents": ["sub1", "sub2"]}
    subs = cai.agents.loader._resolve_subagents(config)
    
    assert len(subs) == 2
    
    assert subs[0]["name"] == "sub1"
    assert subs[0]["description"] == "A subagent"
    assert "hello sub1" in subs[0]["instructions"]
    assert subs[0]["agent"] == "built sub1"
    
    assert subs[1]["name"] == "sub2"
    assert subs[1]["description"] == "Another subagent"
    assert "hello sub2" in subs[1]["instructions"]
    assert subs[1]["agent"] == "built sub2"

def test_resolve_subagents_not_a_list():
    import cai.agents.loader
    config = {"subagents": "sub1"}
    with pytest.raises(ValueError, match="'subagents' must be a list"):
        cai.agents.loader._resolve_subagents(config)
