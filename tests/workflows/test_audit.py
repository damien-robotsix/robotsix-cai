import sys
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cai.workflows.audit import (
    AuditOutput,
    DedupeOutput,
    ProposedIssue,
    _audit_agent,
    _build_architecture_prompt,
    _build_errors_prompt,
    _dedupe_agent,
    _labels_for_confidence,
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


@pytest.mark.parametrize(
    "confidence,expected",
    [
        (1, ["cai:audit", "cai:human-review"]),
        (8, ["cai:audit", "cai:human-review"]),
        (9, ["cai:audit", "cai:raised"]),
        (10, ["cai:audit", "cai:raised"]),
    ],
)
def test_labels_for_confidence(confidence, expected):
    assert _labels_for_confidence(confidence) == expected


@patch("sys.argv", ["cai-audit", "--repo", "owner/repo"])
def test_main_low_confidence_uses_human_review_label(
    mock_setup_langfuse,
    mock_build_prompt,
    mock_cai_bot,
    mock_langfuse_workflow,
    mock_audit_agent,
    mock_dedupe_agent,
):
    mock_audit_agent.run = AsyncMock(return_value=MagicMock(
        output=AuditOutput(issues=[ProposedIssue(title="Issue 1", body="Body 1", confidence=7)])
    ))
    mock_dedupe_agent.run = AsyncMock(return_value=MagicMock(
        output=DedupeOutput(action="new", target_issue_number=None, reason="Brand new")
    ))

    repo_mock = mock_cai_bot.repo.return_value
    repo_mock.get_issues.return_value = []
    repo_mock.create_issue.return_value = MagicMock(html_url="https://github.com/owner/repo/issues/1")

    main()

    repo_mock.create_issue.assert_called_once_with(
        title="Issue 1", body="Body 1", labels=["cai:audit", "cai:human-review"]
    )


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
        title="Issue 1", body="Body 1", labels=["cai:audit", "cai:raised"]
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
        title="Issue 1", body="Body 1", labels=["cai:audit", "cai:raised"]
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


def test_build_architecture_prompt():
    """Verify _build_architecture_prompt produces a non-empty prompt with
    structural signals from a small temp repo."""
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = Path(tmpdir) / "repo"
        workspace.mkdir()

        # Create a package with __init__.py
        pkg = workspace / "mypkg"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("# package init\n")
        (pkg / "module.py").write_text("def foo():\n    pass\n")

        # Create a large file (>300 lines)
        large = workspace / "big_file.py"
        large.write_text("\n".join(f"# line {i}" for i in range(350)))

        # Create a non-Python file
        (workspace / "README.md").write_text("# Hello\n")

        # Create a directory with Python files but no __init__.py
        nopkg = workspace / "nopkg"
        nopkg.mkdir()
        (nopkg / "util.py").write_text("def bar():\n    return 1\n")

        with patch(
            "cai.workflows.audit._clone_repo_for_audit", return_value=None
        ):
            prompt = _build_architecture_prompt(
                MagicMock(), "owner/repo", workspace, []
            )

    assert prompt
    assert "owner/repo" in prompt
    assert "mypkg/" in prompt
    assert "nopkg/" in prompt
    assert "module.py" in prompt
    assert "big_file.py" in prompt
    assert "!LARGE!" in prompt
    assert "README.md" in prompt
    assert "__init__.py" in prompt
    # Line-count annotation for the small module
    assert "lines" in prompt
    # Package structure summary
    assert "mypkg" in prompt
    assert "nopkg" in prompt
    # Closing instruction
    assert "filesystem_read" in prompt
    assert "explore" in prompt
    # No "Additional context" when unknown is empty
    assert "Additional context" not in prompt


def test_build_architecture_prompt_unknown_args():
    """Unknown CLI args are forwarded as 'Additional context' in the prompt."""
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = Path(tmpdir) / "repo"
        workspace.mkdir()
        (workspace / "mod.py").write_text("x = 1\n")

        with patch(
            "cai.workflows.audit._clone_repo_for_audit", return_value=None
        ):
            prompt = _build_architecture_prompt(
                MagicMock(), "owner/repo", workspace,
                ["--extra-flag", "some-value"],
            )

    assert "Additional context: --extra-flag some-value" in prompt


@pytest.mark.parametrize(
    "line_count,expect_large",
    [
        (300, False),
        (301, True),
    ],
)
def test_build_architecture_prompt_large_boundary(line_count, expect_large):
    """Files with exactly 300 lines are NOT marked !LARGE!; 301+ are."""
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = Path(tmpdir) / "repo"
        workspace.mkdir()
        (workspace / "mod.py").write_text(
            "\n".join(f"# line {i}" for i in range(line_count))
        )

        with patch(
            "cai.workflows.audit._clone_repo_for_audit", return_value=None
        ):
            prompt = _build_architecture_prompt(
                MagicMock(), "owner/repo", workspace, []
            )

    if expect_large:
        assert "!LARGE!" in prompt
    else:
        assert "!LARGE!" not in prompt


def test_build_architecture_prompt_no_python_files():
    """Prompt handles repos with no Python files gracefully."""
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = Path(tmpdir) / "repo"
        workspace.mkdir()
        (workspace / "README.md").write_text("# Hello\n")
        (workspace / "script.sh").write_text("echo hi\n")

        with patch(
            "cai.workflows.audit._clone_repo_for_audit", return_value=None
        ):
            prompt = _build_architecture_prompt(
                MagicMock(), "owner/repo", workspace, []
            )

    assert "(no Python files found)" in prompt
    assert "No directories with __init__.py found" in prompt


@patch("sys.argv", ["cai-audit", "--repo", "owner/repo", "--mode", "architecture"])
def test_main_architecture_mode(
    mock_setup_langfuse,
    mock_build_prompt,
    mock_cai_bot,
    mock_langfuse_workflow,
    mock_audit_agent,
    mock_dedupe_agent,
):
    mock_audit_agent.run = AsyncMock(return_value=MagicMock(output=AuditOutput(issues=[])))

    canned_prompt = "architecture prompt"
    with patch(
        "cai.workflows.audit._build_architecture_prompt",
        return_value=canned_prompt,
    ) as mock_arch_prompt:
        # Also capture the call to _audit_agent to verify agent_name
        with patch(
            "cai.workflows.audit._audit_agent",
            return_value=mock_audit_agent,
        ) as patched_agent:
            main()

    mock_arch_prompt.assert_called_once()
    # Verify the agent factory was called with architecture_auditor name
    patched_agent.assert_called_once_with("architecture_auditor")
    mock_audit_agent.run.assert_called_once()
    call_args = mock_audit_agent.run.call_args
    assert call_args[0][0] == canned_prompt


@patch("sys.argv", ["cai-audit", "--repo", "owner/repo", "--mode", "bogus"])
def test_main_architecture_invalid_mode_rejected(
    mock_setup_langfuse,
    mock_build_prompt,
    mock_cai_bot,
    mock_langfuse_workflow,
    mock_audit_agent,
    mock_dedupe_agent,
):
    with pytest.raises(SystemExit):
        main()


# ── _build_errors_prompt ────────────────────────────────────────────────


def test_build_errors_prompt():
    """_build_errors_prompt formats recent failure traces into a prompt."""
    fake_failures = [
        {
            "id": "trace-abc-123",
            "name": "cai-solve",
            "timestamp": "2025-06-15T10:30:00+00:00",
            "errors": [
                {
                    "name": "implement",
                    "status_message": "LLM rate limit exceeded",
                    "output": "Error: 429 Too Many Requests",
                },
            ],
        },
        {
            "id": "trace-def-456",
            "name": "cai-audit",
            "timestamp": "2025-06-15T11:00:00+00:00",
            "errors": [
                {
                    "name": "run_audit",
                    "status_message": "Context length exceeded",
                    "output": None,
                },
            ],
        },
    ]

    with patch("cai.workflows.audit._TRACES") as mock_traces:
        mock_traces.list_failures.return_value = fake_failures
        prompt = _build_errors_prompt([])

    assert "Recent failures (2 traces with errors)" in prompt
    assert "trace-abc-123" in prompt
    assert "trace-def-456" in prompt
    assert "cai-solve" in prompt
    assert "cai-audit" in prompt
    assert "2025-06-15T10:30:00" in prompt
    assert "2025-06-15T11:00:00" in prompt
    assert "implement" in prompt
    assert "run_audit" in prompt
    assert "LLM rate limit exceeded" in prompt
    assert "Context length exceeded" in prompt
    assert "Error: 429 Too Many Requests" in prompt
    assert "trace_analyst" in prompt
    assert "last_detected_at" in prompt
    assert "Additional context" not in prompt


def test_build_errors_prompt_with_unknown_args():
    """Unknown CLI args are forwarded as 'Additional context'."""
    fake_failures = [
        {
            "id": "trace-001",
            "name": "cai-solve",
            "timestamp": "2025-01-01T00:00:00+00:00",
            "errors": [{"name": "step", "status_message": "err", "output": None}],
        },
    ]

    with patch("cai.workflows.audit._TRACES") as mock_traces:
        mock_traces.list_failures.return_value = fake_failures
        prompt = _build_errors_prompt(["--verbose", "--dry-run"])

    assert "Additional context: --verbose --dry-run" in prompt


def test_build_errors_prompt_no_failures():
    """_build_errors_prompt raises SystemExit when no failures are found."""
    with patch("cai.workflows.audit._TRACES") as mock_traces:
        mock_traces.list_failures.return_value = []
        with pytest.raises(SystemExit):
            _build_errors_prompt([])


def test_build_errors_prompt_truncates_long_messages():
    """Status messages longer than 300 chars are truncated with ..."""
    long_msg = "x" * 500
    fake_failures = [
        {
            "id": "trace-001",
            "name": "cai-solve",
            "timestamp": "2025-01-01T00:00:00+00:00",
            "errors": [{"name": "step", "status_message": long_msg, "output": None}],
        },
    ]

    with patch("cai.workflows.audit._TRACES") as mock_traces:
        mock_traces.list_failures.return_value = fake_failures
        prompt = _build_errors_prompt([])

    # Full message should NOT appear (truncated to 300 chars)
    assert long_msg not in prompt
    assert long_msg[:300] in prompt


def test_build_errors_prompt_output_truncated():
    """Output fields longer than 200 chars are truncated."""
    long_output = "y" * 300
    fake_failures = [
        {
            "id": "trace-001",
            "name": "cai-solve",
            "timestamp": "2025-01-01T00:00:00+00:00",
            "errors": [{"name": "step", "status_message": "msg", "output": long_output}],
        },
    ]

    with patch("cai.workflows.audit._TRACES") as mock_traces:
        mock_traces.list_failures.return_value = fake_failures
        prompt = _build_errors_prompt([])

    assert long_output not in prompt
    assert long_output[:200] in prompt


def test_build_errors_prompt_handles_missing_optional_fields():
    """Errors dicts missing status_message or output render gracefully."""
    fake_failures = [
        {
            "id": "trace-minimal",
            "name": "cai-solve",
            "timestamp": None,
            "errors": [{"name": "step"}],
        },
    ]

    with patch("cai.workflows.audit._TRACES") as mock_traces:
        mock_traces.list_failures.return_value = fake_failures
        prompt = _build_errors_prompt([])

    assert "trace-minimal" in prompt
    assert "cai-solve" in prompt
    # Should not crash; just renders what it has
    assert "Recent failures (1 traces with errors)" in prompt


# ── main --mode errors ──────────────────────────────────────────────────


@patch("sys.argv", ["cai-audit", "--repo", "owner/repo", "--mode", "errors"])
def test_main_errors_mode(
    mock_setup_langfuse,
    mock_build_prompt,
    mock_cai_bot,
    mock_langfuse_workflow,
    mock_audit_agent,
    mock_dedupe_agent,
):
    mock_audit_agent.run = AsyncMock(return_value=MagicMock(
        output=AuditOutput(issues=[ProposedIssue(title="Err issue", body="Details", confidence=8)])
    ))
    mock_dedupe_agent.run = AsyncMock(return_value=MagicMock(
        output=DedupeOutput(action="new", target_issue_number=None, reason="Related")
    ))

    repo_mock = mock_cai_bot.repo.return_value
    repo_mock.get_issues.return_value = []
    repo_mock.create_issue.return_value = MagicMock(html_url="https://github.com/owner/repo/issues/1")

    with patch("cai.workflows.audit._build_errors_prompt", return_value="errors prompt") as mock_ep:
        main()

    mock_ep.assert_called_once()
    mock_audit_agent.run.assert_called_once()
    repo_mock.create_issue.assert_called_once_with(
        title="Err issue", body="Details", labels=["cai:audit", "cai:human-review"]
    )


@patch("sys.argv", ["cai-audit", "--repo", "owner/repo", "--mode", "errors"])
def test_main_errors_mode_no_traces(
    mock_setup_langfuse,
    mock_build_prompt,
    mock_cai_bot,
    mock_langfuse_workflow,
    mock_audit_agent,
    mock_dedupe_agent,
):
    """When no failure traces exist, the errors prompt builder calls sys.exit."""
    with patch(
        "cai.workflows.audit._build_errors_prompt",
        side_effect=SystemExit(1),
    ) as mock_ep:
        with pytest.raises(SystemExit):
            main()

    mock_ep.assert_called_once()
    mock_audit_agent.run.assert_not_called()
