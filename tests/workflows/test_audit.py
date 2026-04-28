import sys
from unittest.mock import MagicMock, patch

import pytest

from cai.workflows.audit import (
    AuditOutput,
    DedupeOutput,
    ProposedIssue,
    _audit_agent,
    _dedupe_agent,
    main,
)


@pytest.fixture(autouse=True)
def _reset_agent_cache():
    """Agent factories are lru_cached so a fresh patch lands per test."""
    _audit_agent.cache_clear()
    _dedupe_agent.cache_clear()
    yield
    _audit_agent.cache_clear()
    _dedupe_agent.cache_clear()


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


def _make_agent_dispatch(audit_agent_mock, dedupe_agent_mock):
    def side_effect(path, output_type):
        if "audit.md" in str(path):
            return audit_agent_mock
        return dedupe_agent_mock

    return side_effect


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
    mock_load_agent_from_md.side_effect = _make_agent_dispatch(
        audit_agent_mock, dedupe_agent_mock
    )

    audit_result = MagicMock()
    audit_result.data = AuditOutput(
        issues=[
            ProposedIssue(title="Issue 1", body="Body 1"),
            ProposedIssue(title="Issue 2", body="Body 2"),
        ]
    )
    audit_agent_mock.run_sync.return_value = audit_result

    dedupe_agent_mock.run_sync.side_effect = [
        MagicMock(data=DedupeOutput(action="new", target_issue_number=None, reason="Brand new")),
        MagicMock(data=DedupeOutput(action="discard", target_issue_number=None, reason="Duplicate")),
    ]

    repo_mock = mock_cai_bot.repo.return_value
    repo_mock.get_issues.return_value = []
    created_mock = MagicMock(html_url="https://github.com/owner/repo/issues/1")
    repo_mock.create_issue.return_value = created_mock

    main()

    mock_setup_langfuse.assert_called_once()
    assert mock_load_agent_from_md.call_count == 2
    mock_cai_bot.repo.assert_called_once_with("owner/repo")
    repo_mock.create_issue.assert_called_once_with(
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
    mock_load_agent_from_md.side_effect = _make_agent_dispatch(
        audit_agent_mock, dedupe_agent_mock
    )

    audit_agent_mock.run_sync.return_value = MagicMock(
        data=AuditOutput(issues=[ProposedIssue(title="Issue 1", body="Body 1")])
    )
    dedupe_agent_mock.run_sync.return_value = MagicMock(
        data=DedupeOutput(action="append", target_issue_number=123, reason="Related")
    )

    repo_mock = mock_cai_bot.repo.return_value
    existing_issue = MagicMock()
    existing_issue.number = 123
    existing_issue.title = "Existing issue"
    repo_mock.get_issues.return_value = [existing_issue]
    repo_mock.get_issue.return_value = existing_issue

    main()

    repo_mock.create_issue.assert_not_called()
    existing_issue.create_comment.assert_called_once_with(
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
    mock_load_agent_from_md.side_effect = _make_agent_dispatch(
        audit_agent_mock, dedupe_agent_mock
    )

    audit_agent_mock.run_sync.return_value = MagicMock(
        data=AuditOutput(issues=[ProposedIssue(title="Issue 1", body="Body 1")])
    )
    dedupe_agent_mock.run_sync.return_value = MagicMock(
        data=DedupeOutput(action="append", target_issue_number=None, reason="Related")
    )

    repo_mock = mock_cai_bot.repo.return_value
    repo_mock.get_issues.return_value = []
    repo_mock.create_issue.return_value = MagicMock(
        html_url="https://github.com/owner/repo/issues/1"
    )

    main()

    repo_mock.create_issue.assert_called_once_with(
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
    mock_load_agent_from_md.side_effect = _make_agent_dispatch(
        audit_agent_mock, dedupe_agent_mock
    )

    audit_agent_mock.run_sync.return_value = MagicMock(data=AuditOutput(issues=[]))

    repo_mock = mock_cai_bot.repo.return_value

    main()

    audit_agent_mock.run_sync.assert_called_once()
    dedupe_agent_mock.run_sync.assert_not_called()
    repo_mock.create_issue.assert_not_called()


@patch("sys.argv", ["cai-audit", "--repo", "owner/repo", "extra", "context"])
def test_main_with_unknown_args(
    mock_setup_langfuse,
    mock_load_agent_from_md,
    mock_cai_bot,
    mock_langfuse_workflow,
):
    audit_agent_mock = MagicMock()
    dedupe_agent_mock = MagicMock()
    mock_load_agent_from_md.side_effect = _make_agent_dispatch(
        audit_agent_mock, dedupe_agent_mock
    )

    audit_agent_mock.run_sync.return_value = MagicMock(data=AuditOutput(issues=[]))

    main()

    audit_agent_mock.run_sync.assert_called_once()
    prompt = audit_agent_mock.run_sync.call_args[0][0]
    assert "extra context" in prompt
