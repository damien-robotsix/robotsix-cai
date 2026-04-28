import argparse
import sys
from unittest.mock import MagicMock, call, patch

import pytest
from pydantic import BaseModel

from cai.workflows.audit import AuditOutput, DedupeOutput, ProposedIssue, main


@pytest.fixture
def mock_setup_langfuse():
    with patch("cai.workflows.audit.setup_langfuse") as mock:
        yield mock


@pytest.fixture
def mock_load_agent_from_md():
    with patch("cai.workflows.audit.load_agent_from_md") as mock:
        yield mock


@pytest.fixture
def mock_cai_bot():
    with patch("cai.workflows.audit.CaiBot") as mock:
        bot_instance = MagicMock()
        mock.return_value = bot_instance
        yield bot_instance


@pytest.fixture
def mock_langfuse_workflow():
    with patch("cai.workflows.audit.langfuse_workflow") as mock:
        yield mock


def test_audit_output_model():
    issue = ProposedIssue(title="Test Issue", body="Test Body")
    output = AuditOutput(issues=[issue])
    assert len(output.issues) == 1
    assert output.issues[0].title == "Test Issue"
    assert output.issues[0].body == "Test Body"


def test_dedupe_output_model():
    output = DedupeOutput(action="new", target_issue_number=None, reason="Brand new")
    assert output.action == "new"
    assert output.target_issue_number is None
    assert output.reason == "Brand new"
    
    output = DedupeOutput(action="append", target_issue_number=123, reason="Related")
    assert output.action == "append"
    assert output.target_issue_number == 123
    assert output.reason == "Related"

@patch("sys.argv", ["cai-audit", "--repo", "owner/repo"])
def test_main_creates_issues(
    mock_setup_langfuse,
    mock_load_agent_from_md,
    mock_cai_bot,
    mock_langfuse_workflow,
):
    audit_agent_mock = MagicMock()
    dedupe_agent_mock = MagicMock()
    
    def side_effect(path, output_type):
        if "audit.md" in str(path):
            return audit_agent_mock
        return dedupe_agent_mock
        
    mock_load_agent_from_md.side_effect = side_effect

    mock_audit_result = MagicMock()
    mock_audit_result.data = AuditOutput(
        issues=[
            ProposedIssue(title="Issue 1", body="Body 1"),
            ProposedIssue(title="Issue 2", body="Body 2"),
        ]
    )
    audit_agent_mock.run_sync.return_value = mock_audit_result

    # Deduplicator returns different actions
    mock_dedupe_result_1 = MagicMock()
    mock_dedupe_result_1.data = DedupeOutput(action="new", target_issue_number=None, reason="Brand new")
    
    mock_dedupe_result_2 = MagicMock()
    mock_dedupe_result_2.data = DedupeOutput(action="discard", target_issue_number=None, reason="Duplicate")
    
    dedupe_agent_mock.run_sync.side_effect = [mock_dedupe_result_1, mock_dedupe_result_2]

    # Mock git issues
    mock_repo = mock_cai_bot.return_value.repo.return_value
    mock_repo.get_issues.return_value = []
    
    mock_create_issue = mock_repo.create_issue
    mock_created_issue = MagicMock()
    mock_created_issue.html_url = "https://github.com/owner/repo/issues/1"
    mock_create_issue.return_value = mock_created_issue

    main()

    mock_setup_langfuse.assert_called_once()
    assert mock_load_agent_from_md.call_count == 2
    mock_cai_bot.assert_called_once()
    mock_cai_bot.return_value.repo.assert_called_once_with("owner/repo")

    # One new issue, one discarded
    mock_create_issue.assert_called_once_with(
        title="Issue 1", body="Body 1", labels=["cai:audit"]
    )


@patch("sys.argv", ["cai-audit", "--repo", "owner/repo"])
def test_main_append_issue(
    mock_setup_langfuse,
    mock_load_agent_from_md,
    mock_cai_bot,
    mock_langfuse_workflow,
):
    audit_agent_mock = MagicMock()
    dedupe_agent_mock = MagicMock()
    
    def side_effect(path, output_type):
        if "audit.md" in str(path):
            return audit_agent_mock
        return dedupe_agent_mock
        
    mock_load_agent_from_md.side_effect = side_effect

    mock_audit_result = MagicMock()
    mock_audit_result.data = AuditOutput(
        issues=[
            ProposedIssue(title="Issue 1", body="Body 1"),
        ]
    )
    audit_agent_mock.run_sync.return_value = mock_audit_result

    # Deduplicator returns append
    mock_dedupe_result = MagicMock()
    mock_dedupe_result.data = DedupeOutput(action="append", target_issue_number=123, reason="Related")
    dedupe_agent_mock.run_sync.return_value = mock_dedupe_result

    mock_repo = mock_cai_bot.return_value.repo.return_value
    
    # Mock existing issue
    mock_existing_issue = MagicMock()
    mock_existing_issue.number = 123
    mock_existing_issue.title = "Existing issue"
    mock_repo.get_issues.return_value = [mock_existing_issue]
    mock_repo.get_issue.return_value = mock_existing_issue
    
    mock_create_issue = mock_repo.create_issue

    main()

    # One append (comment created, no new issue created)
    mock_create_issue.assert_not_called()
    mock_existing_issue.create_comment.assert_called_once_with(
        "**Additional proposed issue details:**\n\n**Title**: Issue 1\n\n**Body**:\nBody 1"
    )
    

@patch("sys.argv", ["cai-audit", "--repo", "owner/repo"])
def test_main_append_issue_fallback(
    mock_setup_langfuse,
    mock_load_agent_from_md,
    mock_cai_bot,
    mock_langfuse_workflow,
):
    audit_agent_mock = MagicMock()
    dedupe_agent_mock = MagicMock()
    
    def side_effect(path, output_type):
        if "audit.md" in str(path):
            return audit_agent_mock
        return dedupe_agent_mock
        
    mock_load_agent_from_md.side_effect = side_effect

    mock_audit_result = MagicMock()
    mock_audit_result.data = AuditOutput(
        issues=[
            ProposedIssue(title="Issue 1", body="Body 1"),
        ]
    )
    audit_agent_mock.run_sync.return_value = mock_audit_result

    # Deduplicator returns append but no target issue
    mock_dedupe_result = MagicMock()
    mock_dedupe_result.data = DedupeOutput(action="append", target_issue_number=None, reason="Related")
    dedupe_agent_mock.run_sync.return_value = mock_dedupe_result

    mock_repo = mock_cai_bot.return_value.repo.return_value
    mock_repo.get_issues.return_value = []
    
    mock_create_issue = mock_repo.create_issue
    mock_created_issue = MagicMock()
    mock_created_issue.html_url = "https://github.com/owner/repo/issues/1"
    mock_create_issue.return_value = mock_created_issue

    main()

    # Falls back to new issue creation
    mock_create_issue.assert_called_once_with(
        title="Issue 1", body="Body 1", labels=["cai:audit"]
    )


@patch("sys.argv", ["cai-audit", "--repo", "owner/repo"])
def test_main_no_issues(
    mock_setup_langfuse,
    mock_load_agent_from_md,
    mock_cai_bot,
    mock_langfuse_workflow,
):
    audit_agent_mock = MagicMock()
    dedupe_agent_mock = MagicMock()
    
    def side_effect(path, output_type):
        if "audit.md" in str(path):
            return audit_agent_mock
        return dedupe_agent_mock
        
    mock_load_agent_from_md.side_effect = side_effect

    mock_audit_result = MagicMock()
    mock_audit_result.data = AuditOutput(issues=[])
    audit_agent_mock.run_sync.return_value = mock_audit_result

    mock_repo = mock_cai_bot.return_value.repo.return_value
    mock_create_issue = mock_repo.create_issue

    main()

    audit_agent_mock.run_sync.assert_called_once()
    dedupe_agent_mock.run_sync.assert_not_called()
    mock_create_issue.assert_not_called()


@patch("sys.argv", ["cai-audit", "--repo", "owner/repo", "extra", "context"])
def test_main_with_unknown_args(
    mock_setup_langfuse,
    mock_load_agent_from_md,
    mock_cai_bot,
    mock_langfuse_workflow,
):
    audit_agent_mock = MagicMock()
    dedupe_agent_mock = MagicMock()
    
    def side_effect(path, output_type):
        if "audit.md" in str(path):
            return audit_agent_mock
        return dedupe_agent_mock
        
    mock_load_agent_from_md.side_effect = side_effect
    
    mock_audit_result = MagicMock()
    mock_audit_result.data = AuditOutput(issues=[])
    audit_agent_mock.run_sync.return_value = mock_audit_result

    main()

    audit_agent_mock.run_sync.assert_called_once()
    prompt = audit_agent_mock.run_sync.call_args[0][0]
    assert "extra context" in prompt
