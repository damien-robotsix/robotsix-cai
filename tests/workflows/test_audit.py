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
    _build_security_prompt,
    _create_issues_from_proposals,
    _dedupe_agent,
    _labels_for_confidence,
    _recent_commits_since,
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


def test_audit_agent_cache_fits_all_modes():
    """The _audit_agent LRU cache (maxsize=4) must hold all four agent
    names without eviction: audit, architecture_auditor, duplication_auditor,
    security_auditor."""
    # We can't actually build agents (needs OPENROUTER_API_KEY), but we can
    # verify the cache info reports maxsize=4.
    assert _audit_agent.cache_info().maxsize == 4


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


@patch("sys.argv", ["cai-audit", "--repo", "owner/repo", "--mode", "security"])
def test_main_security_mode(
    mock_setup_langfuse,
    mock_build_prompt,
    mock_cai_bot,
    mock_langfuse_workflow,
    mock_audit_agent,
    mock_dedupe_agent,
):
    mock_audit_agent.run = AsyncMock(return_value=MagicMock(output=AuditOutput(issues=[])))

    canned_prompt = "security prompt"
    with patch(
        "cai.workflows.audit._build_security_prompt",
        return_value=canned_prompt,
    ) as mock_sec_prompt:
        # Also capture the call to _audit_agent to verify agent_name
        with patch(
            "cai.workflows.audit._audit_agent",
            return_value=mock_audit_agent,
        ) as patched_agent:
            main()

    mock_sec_prompt.assert_called_once()
    # Verify the agent factory was called with security_auditor name
    patched_agent.assert_called_once_with("security_auditor")
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


# ── _build_security_prompt ──────────────────────────────────────────────


def test_build_security_prompt():
    """Verify _build_security_prompt produces a non-empty prompt with
    security-scanning instructions."""
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = Path(tmpdir) / "repo"
        workspace.mkdir()
        # Create some files (content doesn't matter — the prompt is static)
        (workspace / "main.py").write_text("import os\nos.system('ls')\n")
        (workspace / "config.yaml").write_text("api_key: sk-abc123\n")

        with patch(
            "cai.workflows.audit._clone_repo_for_audit", return_value=None
        ):
            prompt = _build_security_prompt(
                MagicMock(), "owner/repo", workspace, []
            )

    assert prompt
    assert "owner/repo" in prompt
    assert "Security audit" in prompt
    assert "filesystem_read" in prompt
    assert "explore" in prompt
    assert "shell=True" in prompt
    assert "hardcoded credentials" in prompt.lower() or "hardcoded" in prompt.lower()
    assert "eval" in prompt
    assert "exec" in prompt
    assert "pickle" in prompt
    assert "yaml.load" in prompt
    assert "TLS" in prompt
    assert "AuditOutput" in prompt
    assert "conservative" in prompt.lower()
    # No "Additional context" when unknown is empty
    assert "Additional context" not in prompt


def test_build_security_prompt_unknown_args():
    """Unknown CLI args are forwarded as 'Additional context' in the security prompt."""
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = Path(tmpdir) / "repo"
        workspace.mkdir()
        (workspace / "mod.py").write_text("x = 1\n")

        with patch(
            "cai.workflows.audit._clone_repo_for_audit", return_value=None
        ):
            prompt = _build_security_prompt(
                MagicMock(), "owner/repo", workspace,
                ["--focus", "injection"],
            )

    assert "Additional context: --focus injection" in prompt


def test_build_security_prompt_clones_repo():
    """_build_security_prompt must clone the target repo into the workspace."""
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = Path(tmpdir) / "repo"
        workspace.mkdir()

        with patch(
            "cai.workflows.audit._clone_repo_for_audit"
        ) as mock_clone:
            _build_security_prompt(MagicMock(), "owner/repo", workspace, [])

    mock_clone.assert_called_once()
    # First positional arg should be the bot, second the repo, third the workspace
    assert mock_clone.call_args[0][1] == "owner/repo"
    assert mock_clone.call_args[0][2] == workspace


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


# ── _recent_commits_since ───────────────────────────────────────────────


def test_recent_commits_since_no_timestamp_returns_empty():
    """When last_detected_at is None, the function returns an empty string."""
    result = _recent_commits_since(MagicMock(), None)
    assert result == ""


def test_recent_commits_since_no_commits_returns_empty():
    """When no commits are found since the timestamp, returns empty string."""
    repo_mock = MagicMock()
    repo_mock.get_commits.return_value = []

    result = _recent_commits_since(repo_mock, "2025-06-01T00:00:00Z")
    assert result == ""


def test_recent_commits_since_formats_commits():
    """Commits are formatted with short sha and first line of message."""
    commit_a = MagicMock()
    commit_a.sha = "abc123def456789"
    commit_a.commit.message = "Fix: resolve the bug\n\nExtended description."

    commit_b = MagicMock()
    commit_b.sha = "fed9876543210"
    commit_b.commit.message = "feat: add new feature"

    repo_mock = MagicMock()
    repo_mock.get_commits.return_value = [commit_a, commit_b]

    result = _recent_commits_since(repo_mock, "2025-06-01T00:00:00Z")

    assert "Commits merged after" in result
    assert "2025-06-01T00:00:00" in result
    assert "abc123de" in result
    assert "Fix: resolve the bug" in result
    assert "fed98765" in result
    assert "feat: add new feature" in result
    assert "discard" in result


def test_recent_commits_since_truncates_at_20_commits():
    """Only the first 20 commits are included."""
    commits = []
    for i in range(25):
        c = MagicMock()
        c.sha = f"sha{i:040d}"
        c.commit.message = f"commit {i}"
        commits.append(c)

    repo_mock = MagicMock()
    repo_mock.get_commits.return_value = commits

    result = _recent_commits_since(repo_mock, "2025-06-01T00:00:00Z")

    # The 21st commit (index 20) should NOT appear
    assert "commit 20" not in result
    assert "commit 19" in result


def test_recent_commits_since_handles_exception():
    """When get_commits raises, the function returns empty string gracefully."""
    repo_mock = MagicMock()
    repo_mock.get_commits.side_effect = RuntimeError("API error")

    result = _recent_commits_since(repo_mock, "2025-06-01T00:00:00Z")
    assert result == ""


def test_recent_commits_since_parses_iso_timestamp():
    """The function correctly parses ISO timestamps with Z suffix."""
    commit = MagicMock()
    commit.sha = "abc123def456"
    commit.commit.message = "fix: something"

    repo_mock = MagicMock()
    repo_mock.get_commits.return_value = [commit]

    result = _recent_commits_since(repo_mock, "2025-06-01T12:30:00Z")

    # The truncated timestamp (first 19 chars) should appear
    assert "2025-06-01T12:30:00" in result
    # Verify get_commits was called with a datetime argument
    call_args = repo_mock.get_commits.call_args
    from datetime import datetime, timezone
    assert call_args[1]["since"] == datetime(2025, 6, 1, 12, 30, 0, tzinfo=timezone.utc)


# ── _create_issues_from_proposals ──────────────────────────────────────


def test_create_issues_from_proposals_new_issue():
    """When dedupe says 'new', the function creates an issue with labels
    from the provided callable."""
    fake_dedupe = MagicMock()
    fake_dedupe.run = AsyncMock(return_value=MagicMock(
        output=DedupeOutput(action="new", target_issue_number=None, reason="Brand new")
    ))

    repo_mock = MagicMock()
    repo_mock.get_issues.return_value = []
    created = MagicMock()
    created.html_url = "https://github.com/owner/repo/issues/1"
    repo_mock.create_issue.return_value = created

    bot = MagicMock()
    bot.repo.return_value = repo_mock

    issue = ProposedIssue(title="Test", body="Body text", confidence=7)

    with patch("cai.workflows.audit._dedupe_agent", return_value=fake_dedupe):
        import asyncio
        asyncio.run(_create_issues_from_proposals(
            bot=bot,
            repo_name="owner/repo",
            issues=[issue],
            labels_for_confidence=lambda c: ["custom", "label"],
        ))

    repo_mock.create_issue.assert_called_once_with(
        title="Test",
        body="Body text",
        labels=["custom", "label"],
    )


def test_create_issues_from_proposals_discard():
    """When dedupe says 'discard', no issue is created or appended."""
    fake_dedupe = MagicMock()
    fake_dedupe.run = AsyncMock(return_value=MagicMock(
        output=DedupeOutput(action="discard", target_issue_number=None, reason="Duplicate")
    ))

    repo_mock = MagicMock()
    repo_mock.get_issues.return_value = []

    bot = MagicMock()
    bot.repo.return_value = repo_mock

    issue = ProposedIssue(title="Test", body="Body", confidence=5)

    with patch("cai.workflows.audit._dedupe_agent", return_value=fake_dedupe):
        import asyncio
        asyncio.run(_create_issues_from_proposals(
            bot=bot,
            repo_name="owner/repo",
            issues=[issue],
            labels_for_confidence=lambda c: ["tag"],
        ))

    repo_mock.create_issue.assert_not_called()


def test_create_issues_from_proposals_append():
    """When dedupe says 'append' with a target, a comment is added."""
    fake_dedupe = MagicMock()
    fake_dedupe.run = AsyncMock(return_value=MagicMock(
        output=DedupeOutput(action="append", target_issue_number=42, reason="Related")
    ))

    existing = MagicMock()
    existing.number = 42
    existing.title = "Existing"

    repo_mock = MagicMock()
    repo_mock.get_issues.return_value = [existing]
    repo_mock.get_issue.return_value = existing

    bot = MagicMock()
    bot.repo.return_value = repo_mock

    issue = ProposedIssue(title="Test", body="Body content", confidence=8)

    with patch("cai.workflows.audit._dedupe_agent", return_value=fake_dedupe):
        import asyncio
        asyncio.run(_create_issues_from_proposals(
            bot=bot,
            repo_name="owner/repo",
            issues=[issue],
            labels_for_confidence=lambda c: ["tag"],
        ))

    repo_mock.create_issue.assert_not_called()
    existing.create_comment.assert_called_once_with(
        "**Additional proposed issue details:**\n\n**Title**: Test\n\n**Body**:\nBody content"
    )


def test_create_issues_from_proposals_append_no_target_falls_back():
    """When dedupe says 'append' but gives no target, a new issue is created."""
    fake_dedupe = MagicMock()
    fake_dedupe.run = AsyncMock(return_value=MagicMock(
        output=DedupeOutput(action="append", target_issue_number=None, reason="Related")
    ))

    repo_mock = MagicMock()
    repo_mock.get_issues.return_value = []
    created = MagicMock()
    created.html_url = "https://github.com/owner/repo/issues/1"
    repo_mock.create_issue.return_value = created

    bot = MagicMock()
    bot.repo.return_value = repo_mock

    issue = ProposedIssue(title="Test", body="Body", confidence=10)

    with patch("cai.workflows.audit._dedupe_agent", return_value=fake_dedupe):
        import asyncio
        asyncio.run(_create_issues_from_proposals(
            bot=bot,
            repo_name="owner/repo",
            issues=[issue],
            labels_for_confidence=lambda c: ["prefix", "fallback"],
        ))

    repo_mock.create_issue.assert_called_once_with(
        title="Test",
        body="Body",
        labels=["prefix", "fallback"],
    )


def test_create_issues_from_proposals_multiple_issues():
    """Mixed outcomes across multiple issues are all processed."""
    fake_dedupe = MagicMock()
    fake_dedupe.run = AsyncMock(side_effect=[
        MagicMock(output=DedupeOutput(action="new", target_issue_number=None, reason="New")),
        MagicMock(output=DedupeOutput(action="discard", target_issue_number=None, reason="Dup")),
        MagicMock(output=DedupeOutput(action="new", target_issue_number=None, reason="Also new")),
    ])

    c1 = MagicMock()
    c1.html_url = "https://github.com/owner/repo/issues/1"
    c2 = MagicMock()
    c2.html_url = "https://github.com/owner/repo/issues/3"

    repo_mock = MagicMock()
    repo_mock.get_issues.return_value = []
    repo_mock.create_issue.side_effect = [c1, c2]

    bot = MagicMock()
    bot.repo.return_value = repo_mock

    issues = [
        ProposedIssue(title="A", body="Body A", confidence=9),
        ProposedIssue(title="B", body="Body B", confidence=4),
        ProposedIssue(title="C", body="Body C", confidence=10),
    ]

    def labeler(c: int) -> list[str]:
        return ["audit", "raised" if c >= 9 else "human-review"]

    with patch("cai.workflows.audit._dedupe_agent", return_value=fake_dedupe):
        import asyncio
        asyncio.run(_create_issues_from_proposals(
            bot=bot,
            repo_name="owner/repo",
            issues=issues,
            labels_for_confidence=labeler,
        ))

    assert repo_mock.create_issue.call_count == 2
    assert repo_mock.create_issue.call_args_list[0][1] == {
        "title": "A", "body": "Body A",
        "labels": ["audit", "raised"],
    }
    assert repo_mock.create_issue.call_args_list[1][1] == {
        "title": "C", "body": "Body C",
        "labels": ["audit", "raised"],
    }


def test_create_issues_from_proposals_open_issues_listed():
    """Open issues are summarized and included in the dedupe prompt."""
    fake_dedupe = MagicMock()
    fake_dedupe.run = AsyncMock(return_value=MagicMock(
        output=DedupeOutput(action="new", target_issue_number=None, reason="New")
    ))

    existing_a = MagicMock()
    existing_a.number = 1
    existing_a.title = "First existing issue"
    existing_b = MagicMock()
    existing_b.number = 2
    existing_b.title = "Second existing issue"

    repo_mock = MagicMock()
    repo_mock.get_issues.return_value = [existing_a, existing_b]
    created = MagicMock()
    created.html_url = "https://github.com/owner/repo/issues/3"
    repo_mock.create_issue.return_value = created

    bot = MagicMock()
    bot.repo.return_value = repo_mock

    issue = ProposedIssue(title="Test", body="Body", confidence=5)

    with patch("cai.workflows.audit._dedupe_agent", return_value=fake_dedupe):
        import asyncio
        asyncio.run(_create_issues_from_proposals(
            bot=bot,
            repo_name="owner/repo",
            issues=[issue],
            labels_for_confidence=lambda c: ["label"],
        ))

    # The dedupe prompt should include the open issues summary
    prompt = fake_dedupe.run.call_args[0][0]
    assert "#1: First existing issue" in prompt
    assert "#2: Second existing issue" in prompt


def test_create_issues_from_proposals_empty_issues_list():
    """When no issues are passed, repo is never accessed."""
    bot = MagicMock()

    with patch("cai.workflows.audit._dedupe_agent") as mock_dedupe_factory:
        import asyncio
        asyncio.run(_create_issues_from_proposals(
            bot=bot,
            repo_name="owner/repo",
            issues=[],
            labels_for_confidence=lambda c: ["label"],
        ))

    bot.repo.assert_not_called()
    mock_dedupe_factory.assert_not_called()


def test_create_issues_from_proposals_recent_commits_in_prompt():
    """When an issue has last_detected_at, recent commits are included in
    the dedupe prompt."""
    fake_dedupe = MagicMock()
    fake_dedupe.run = AsyncMock(return_value=MagicMock(
        output=DedupeOutput(action="new", target_issue_number=None, reason="New")
    ))

    commit = MagicMock()
    commit.sha = "abc123def456789"
    commit.commit.message = "fix: resolve the bug"

    repo_mock = MagicMock()
    repo_mock.get_issues.return_value = []
    repo_mock.get_commits.return_value = [commit]
    created = MagicMock()
    created.html_url = "https://github.com/owner/repo/issues/1"
    repo_mock.create_issue.return_value = created

    bot = MagicMock()
    bot.repo.return_value = repo_mock

    issue = ProposedIssue(
        title="Test", body="Body", confidence=5,
        last_detected_at="2025-06-01T00:00:00Z",
    )

    with patch("cai.workflows.audit._dedupe_agent", return_value=fake_dedupe):
        import asyncio
        asyncio.run(_create_issues_from_proposals(
            bot=bot,
            repo_name="owner/repo",
            issues=[issue],
            labels_for_confidence=lambda c: ["label"],
        ))

    prompt = fake_dedupe.run.call_args[0][0]
    assert "Commits merged after" in prompt
    assert "abc123de" in prompt
    assert "fix: resolve the bug" in prompt


def test_create_issues_from_proposals_no_open_issues_message():
    """When there are no open issues, a placeholder message is shown."""
    fake_dedupe = MagicMock()
    fake_dedupe.run = AsyncMock(return_value=MagicMock(
        output=DedupeOutput(action="new", target_issue_number=None, reason="New")
    ))

    repo_mock = MagicMock()
    repo_mock.get_issues.return_value = []
    created = MagicMock()
    created.html_url = "https://github.com/owner/repo/issues/1"
    repo_mock.create_issue.return_value = created

    bot = MagicMock()
    bot.repo.return_value = repo_mock

    issue = ProposedIssue(title="Test", body="Body", confidence=5)

    with patch("cai.workflows.audit._dedupe_agent", return_value=fake_dedupe):
        import asyncio
        asyncio.run(_create_issues_from_proposals(
            bot=bot,
            repo_name="owner/repo",
            issues=[issue],
            labels_for_confidence=lambda c: ["label"],
        ))

    prompt = fake_dedupe.run.call_args[0][0]
    assert "No open issues." in prompt
