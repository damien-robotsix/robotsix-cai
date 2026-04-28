import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch

from pydantic_ai.exceptions import ModelRetry

from cai.workflows.state import ImplementOutput, ReviewReply
from cai.workflows.implement import _implement_agent

def test_implement_agent_fix_must_edit_valid():
    # When is_dirty returns true, 'fix' action doesn't raise
    
    agent = _implement_agent()
    # Output validator is async, so we'll test the actual validation logic indirectly
    # or by extracting it. The easiest way is to run the validator directly.
    validator = list(agent._result_validators)[0]
    
    ctx = MagicMock()
    ctx.deps.backend.root_dir = "/tmp"
    
    output = ImplementOutput(
        summary="A", 
        commit_message="B",
        required_checks=[],
        replies=[ReviewReply(thread_id="1", action="fix", reply="Done")]
    )
    
    with patch("cai.workflows.implement.Repo") as mock_repo:
        repo_instance = MagicMock()
        repo_instance.is_dirty.return_value = True
        mock_repo.return_value = repo_instance
        
        import asyncio
        result = asyncio.run(validator(ctx, output))
        assert result == output

def test_implement_agent_fix_must_edit_invalid():
    # When is_dirty returns false, 'fix' action raises ModelRetry
    
    agent = _implement_agent()
    validator = list(agent._result_validators)[0]
    
    ctx = MagicMock()
    ctx.deps.backend.root_dir = "/tmp"
    
    output = ImplementOutput(
        summary="A", 
        commit_message="B",
        required_checks=[],
        replies=[ReviewReply(thread_id="1", action="fix", reply="Done")]
    )
    
    with patch("cai.workflows.implement.Repo") as mock_repo:
        repo_instance = MagicMock()
        repo_instance.is_dirty.return_value = False
        mock_repo.return_value = repo_instance
        
        import asyncio
        with pytest.raises(ModelRetry) as exc:
            asyncio.run(validator(ctx, output))
        assert "working tree has no changes" in str(exc.value)

def test_implement_agent_reply_only_no_changes_ok():
    # When is_dirty returns false, but action is 'reply_only', it doesn't raise
    
    agent = _implement_agent()
    validator = list(agent._result_validators)[0]
    
    ctx = MagicMock()
    ctx.deps.backend.root_dir = "/tmp"
    
    output = ImplementOutput(
        summary="A", 
        commit_message="B",
        required_checks=[],
        replies=[ReviewReply(thread_id="1", action="reply_only", reply="Wontfix")]
    )
    
    with patch("cai.workflows.implement.Repo") as mock_repo:
        repo_instance = MagicMock()
        repo_instance.is_dirty.return_value = False
        mock_repo.return_value = repo_instance
        
        import asyncio
        result = asyncio.run(validator(ctx, output))
        assert result == output
