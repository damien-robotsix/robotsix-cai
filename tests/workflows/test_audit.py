import argparse
import sys
from unittest.mock import MagicMock, call, patch

import pytest
from pydantic import BaseModel

from cai.workflows.audit import AuditOutput, ProposedIssue, main


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


@patch("sys.argv", ["cai-audit", "--repo", "owner/repo"])
def test_main_creates_issues(
    mock_setup_langfuse,
    mock_load_agent_from_md,
    mock_cai_bot,
    mock_langfuse_workflow,
):
    agent_mock = MagicMock()
    mock_load_agent_from_md.return_value = agent_mock
    
    result_mock = MagicMock()
    result_mock.data = AuditOutput(
        issues=[
            ProposedIssue(title="Issue 1", body="Body 1"),
            ProposedIssue(title="Issue 2", body="Body 2"),
        ]
    )
    agent_mock.run_sync.return_value = result_mock
    
    repo_mock = MagicMock()
    mock_cai_bot.repo.return_value = repo_mock
    repo_mock.create_issue.side_effect = [
        MagicMock(html_url="http://github.com/issue/1"),
        MagicMock(html_url="http://github.com/issue/2"),
    ]

    main()

    mock_setup_langfuse.assert_called_once()
    mock_cai_bot.repo.assert_called_once_with("owner/repo")
    
    # Check agent run prompt
    prompt = "Please audit the recent workflow traces. Analyze them and draft improvements as proposed issues."
    agent_mock.run_sync.assert_called_once_with(prompt)
    
    # Check issues created
    assert repo_mock.create_issue.call_count == 2
    repo_mock.create_issue.assert_has_calls(
        [
            call(title="Issue 1", body="Body 1", labels=["cai:audit"]),
            call(title="Issue 2", body="Body 2", labels=["cai:audit"]),
        ]
    )

    # Check context manager
    mock_langfuse_workflow.assert_called_once_with("cai-audit", metadata={"repo": "owner/repo"})


@patch("sys.argv", ["cai-audit", "--repo", "owner/repo", "extra", "context"])
def test_main_with_unknown_args(
    mock_setup_langfuse,
    mock_load_agent_from_md,
    mock_cai_bot,
    mock_langfuse_workflow,
):
    agent_mock = MagicMock()
    mock_load_agent_from_md.return_value = agent_mock
    result_mock = MagicMock()
    result_mock.data = AuditOutput(issues=[])
    agent_mock.run_sync.return_value = result_mock
    mock_cai_bot.repo.return_value = MagicMock()

    main()

    prompt = (
        "Please audit the recent workflow traces. Analyze them and draft improvements "
        "as proposed issues. Additional context: extra context"
    )
    agent_mock.run_sync.assert_called_once_with(prompt)


@patch("sys.argv", ["cai-audit", "--repo", "owner/repo"])
def test_main_no_issues_proposed(
    mock_setup_langfuse,
    mock_load_agent_from_md,
    mock_cai_bot,
    mock_langfuse_workflow,
    capsys,
):
    agent_mock = MagicMock()
    mock_load_agent_from_md.return_value = agent_mock
    result_mock = MagicMock()
    result_mock.data = AuditOutput(issues=[])
    agent_mock.run_sync.return_value = result_mock
    
    repo_mock = MagicMock()
    mock_cai_bot.repo.return_value = repo_mock

    main()

    repo_mock.create_issue.assert_not_called()
    captured = capsys.readouterr()
    assert "No issues proposed by the audit agent." in captured.err