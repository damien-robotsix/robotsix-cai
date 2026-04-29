import sys
from unittest.mock import AsyncMock, MagicMock, patch

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
def mock_cai_bot():
    with patch("cai.workflows.audit.CaiBot") as mock:
        bot_instance = MagicMock()
        mock.return_value = bot_instance
        yield bot_instance


@pytest.fixture
def mock_langfuse_workflow():
    with patch("cai.workflows.audit.langfuse_workflow") as mock:
        yield mock


@pytest.fixture
def mock_build_prompt():
    with patch("cai.workflows.audit._build_cost_prompt", return_value="mocked audit prompt") as mock:
        yield mock


@pytest.fixture
def mock_audit_agent():
    agent_mock = MagicMock()
    with patch("cai.workflows.audit._audit_agent", return_value=agent_mock):
        yield agent_mock


@pytest.fixture
def mock_dedupe_agent():
    agent_mock = MagicMock()
    with patch("cai.workflows.audit._dedupe_agent", return_value=agent_mock):
        yield agent_mock


def test_audit_output_model():
    issue = ProposedIssue(title="Test Issue", body="Test Body", confidence=8)
    output = AuditOutput(issues=[issue])
    assert len(output.issues) == 1
    assert output.issues[0].title == "Test Issue"
    assert output.issues[0].body == "Test Body"
    assert output.issues[0].confidence == 8


def test_proposed_issue_confidence_required():
    import pydantic

    with pytest.raises(pydantic.ValidationError):
        ProposedIssue(title="Test Issue", body="Test Body")


def test_proposed_issue_confidence_bounds():
    import pydantic

    with pytest.raises(pydantic.ValidationError):
        ProposedIssue(title="t", body="b", confidence=0)
    with pytest.raises(pydantic.ValidationError):
        ProposedIssue(title="t", body="b", confidence=11)


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
    mock_build_prompt,
    mock_cai_bot,
    mock_langfuse_workflow,
    mock_audit_agent,
    mock_dedupe_agent,
):
    mock_audit_agent.run = AsyncMock(return_value=MagicMock(
        output=AuditOutput(issues=[
            ProposedIssue(title="Issue 1", body="Body 1", confidence=9),
            ProposedIssue(title="Issue 2", body="Body 2", confidence=6),
        ])
    ))
    mock_dedupe_agent.run = AsyncMock(side_effect=[
        MagicMock(output=DedupeOutput(action="new", target_issue_number=None, reason="Brand new")),
        MagicMock(output=DedupeOutput(action="discard", target_issue_number=None, reason="Duplicate")),
    ])

    repo_mock = mock_cai_bot.repo.return_value
    repo_mock.get_issues.return_value = []
    repo_mock.create_issue.return_value = MagicMock(html_url="https://github.com/owner/repo/issues/1")

    main()

    mock_setup_langfuse.assert_called_once()
    mock_build_prompt.assert_called_once()
    mock_cai_bot.repo.assert_called_once_with("owner/repo")
    repo_mock.create_issue.assert_called_once_with(
        title="Issue 1", body="Body 1", labels=["cai:audit"]
    )


@patch("sys.argv", ["cai-audit", "--repo", "owner/repo"])
def test_main_append_issue(
    mock_setup_langfuse,
    mock_build_prompt,
    mock_cai_bot,
    mock_langfuse_workflow,
    mock_audit_agent,
    mock_dedupe_agent,
):
    mock_audit_agent.run = AsyncMock(return_value=MagicMock(
        output=AuditOutput(issues=[ProposedIssue(title="Issue 1", body="Body 1", confidence=9)])
    ))
    mock_dedupe_agent.run = AsyncMock(return_value=MagicMock(
        output=DedupeOutput(action="append", target_issue_number=123, reason="Related")
    ))

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
    mock_build_prompt,
    mock_cai_bot,
    mock_langfuse_workflow,
    mock_audit_agent,
    mock_dedupe_agent,
):
    mock_audit_agent.run = AsyncMock(return_value=MagicMock(
        output=AuditOutput(issues=[ProposedIssue(title="Issue 1", body="Body 1", confidence=9)])
    ))
    mock_dedupe_agent.run = AsyncMock(return_value=MagicMock(
        output=DedupeOutput(action="append", target_issue_number=None, reason="Related")
    ))

    repo_mock = mock_cai_bot.repo.return_value
    repo_mock.get_issues.return_value = []
    repo_mock.create_issue.return_value = MagicMock(html_url="https://github.com/owner/repo/issues/1")

    main()

    repo_mock.create_issue.assert_called_once_with(
        title="Issue 1", body="Body 1", labels=["cai:audit"]
    )


@patch("sys.argv", ["cai-audit", "--repo", "owner/repo"])
def test_main_no_issues(
    mock_setup_langfuse,
    mock_build_prompt,
    mock_cai_bot,
    mock_langfuse_workflow,
    mock_audit_agent,
    mock_dedupe_agent,
):
    mock_audit_agent.run = AsyncMock(return_value=MagicMock(output=AuditOutput(issues=[])))

    repo_mock = mock_cai_bot.repo.return_value

    main()

    mock_audit_agent.run.assert_called_once()
    mock_dedupe_agent.run.assert_not_called()
    repo_mock.create_issue.assert_not_called()


@patch("sys.argv", ["cai-audit", "--repo", "owner/repo", "extra", "context"])
def test_main_with_unknown_args(
    mock_setup_langfuse,
    mock_build_prompt,
    mock_cai_bot,
    mock_langfuse_workflow,
    mock_audit_agent,
    mock_dedupe_agent,
):
    mock_audit_agent.run = AsyncMock(return_value=MagicMock(output=AuditOutput(issues=[])))

    main()

    mock_audit_agent.run.assert_called_once()
    mock_build_prompt.assert_called_once_with(["extra", "context"])
