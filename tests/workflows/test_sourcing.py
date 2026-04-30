"""Tests for ``cai.workflows.sourcing``.

Covers the sourcing workflow's pure logic, models, state, prompt builder,
graph construction, and the CreateIssuesNode issue-processing pipeline.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic_graph import Graph

from cai.workflows.audit import DedupeOutput, ProposedIssue
from cai.workflows.sourcing import (
    SourcingOutput,
    SourcingState,
    _SourcingInnerOutput,
    _build_sourcing_prompt,
    _labels_for_confidence,
    _sourcing_agent,
    CreateIssuesNode,
    RunSourcingNode,
    main,
    sourcing_graph,
)


@pytest.fixture(autouse=True)
def _reset_agent_cache():
    """_sourcing_agent is lru_cached so a fresh patch lands per test."""
    _sourcing_agent.cache_clear()
    yield
    _sourcing_agent.cache_clear()


# ── _labels_for_confidence ────────────────────────────────────────────


@pytest.mark.parametrize(
    "confidence,expected",
    [
        (1, ["cai:sourcing", "cai:human-review"]),
        (5, ["cai:sourcing", "cai:human-review"]),
        (8, ["cai:sourcing", "cai:human-review"]),
        (9, ["cai:sourcing", "cai:raised"]),
        (10, ["cai:sourcing", "cai:raised"]),
    ],
)
def test_labels_for_confidence(confidence, expected):
    """Confidence >= 9 routes to cai:raised; below routes to cai:human-review.

    Both branches carry the cai:sourcing label prefix.
    """
    assert _labels_for_confidence(confidence) == expected


# ── SourcingOutput model ──────────────────────────────────────────────


def test_sourcing_output_model():
    """SourcingOutput wraps a list of ProposedIssue items."""
    issue = ProposedIssue(title="Tool X", body="Consider adopting Tool X.", confidence=8)
    output = SourcingOutput(issues=[issue])
    assert len(output.issues) == 1
    assert output.issues[0].title == "Tool X"
    assert output.issues[0].body == "Consider adopting Tool X."
    assert output.issues[0].confidence == 8


def test_sourcing_output_empty():
    """SourcingOutput can hold an empty issues list."""
    output = SourcingOutput(issues=[])
    assert output.issues == []


def test_sourcing_output_issues_required():
    """SourcingOutput requires the 'issues' field."""
    import pydantic

    with pytest.raises(pydantic.ValidationError):
        SourcingOutput()  # type: ignore[call-arg]


# ── _SourcingInnerOutput model ────────────────────────────────────────


def test_sourcing_inner_output_model():
    """_SourcingInnerOutput mirrors SourcingOutput but is the agent-facing schema."""
    issue = ProposedIssue(
        title="Library Y",
        body="Library Y provides declarative config.",
        confidence=9,
        last_detected_at="2025-06-01T00:00:00Z",
    )
    inner = _SourcingInnerOutput(issues=[issue])
    assert len(inner.issues) == 1
    assert inner.issues[0].title == "Library Y"
    assert inner.issues[0].confidence == 9
    assert inner.issues[0].last_detected_at == "2025-06-01T00:00:00Z"


def test_sourcing_inner_output_empty():
    """_SourcingInnerOutput can hold an empty issues list."""
    inner = _SourcingInnerOutput(issues=[])
    assert inner.issues == []


# ── SourcingState dataclass ───────────────────────────────────────────


def test_sourcing_state_defaults():
    """SourcingState defaults output to None."""
    bot = MagicMock()
    state = SourcingState(bot=bot, repo="owner/repo", prompt="test")
    assert state.bot is bot
    assert state.repo == "owner/repo"
    assert state.prompt == "test"
    assert state.output is None


def test_sourcing_state_with_output():
    """SourcingState accepts an explicit output."""
    bot = MagicMock()
    output = SourcingOutput(issues=[])
    state = SourcingState(bot=bot, repo="x/y", prompt="p", output=output)
    assert state.output is output


# ── _build_sourcing_prompt ────────────────────────────────────────────


def test_build_sourcing_prompt_returns_non_empty_string():
    """_build_sourcing_prompt returns a non-empty string."""
    prompt = _build_sourcing_prompt()
    assert isinstance(prompt, str)
    assert len(prompt) > 0


def test_build_sourcing_prompt_mentions_key_areas():
    """The prompt names the project's technology areas."""
    prompt = _build_sourcing_prompt()
    assert "AI agent frameworks" in prompt
    assert "pydantic-ai" in prompt or "pydantic-deep" in prompt
    assert "GitHub automation" in prompt
    assert "PyGithub" in prompt
    assert "Observability" in prompt
    assert "Langfuse" in prompt
    assert "Code analysis" in prompt
    assert "jscpd" in prompt
    assert "CI/CD" in prompt
    assert "Docker" in prompt


def test_build_sourcing_prompt_instructs_web_search_and_fetch():
    """The prompt asks the agent to use web_search and web_fetch."""
    prompt = _build_sourcing_prompt()
    assert "web_search" in prompt
    assert "web_fetch" in prompt


def test_build_sourcing_prompt_instructs_evaluation_criteria():
    """The prompt describes the evaluation rubric for candidates."""
    prompt = _build_sourcing_prompt()
    assert "license" in prompt.lower()
    assert "MIT" in prompt or "Apache" in prompt or "BSD" in prompt
    assert "last_detected_at" in prompt
    assert "confidence" in prompt.lower()
    assert "actively maintained" in prompt.lower() or "commits" in prompt.lower()


def test_build_sourcing_prompt_instructs_output_format():
    """The prompt tells the agent to return a SourcingOutput."""
    prompt = _build_sourcing_prompt()
    assert "SourcingOutput" in prompt
    assert "ProposedIssue" in prompt


# ── sourcing_graph ────────────────────────────────────────────────────


def test_sourcing_graph_is_graph_instance():
    """sourcing_graph is a pydantic_graph.Graph."""
    assert isinstance(sourcing_graph, Graph)


# ── RunSourcingNode ───────────────────────────────────────────────────


def test_run_sourcing_node_produces_output():
    """RunSourcingNode calls the sourcing agent and returns CreateIssuesNode
    when issues are proposed."""
    from pydantic_graph import GraphRunContext

    fake_agent = MagicMock()
    fake_agent.run = AsyncMock(return_value=MagicMock(
        output=_SourcingInnerOutput(issues=[
            ProposedIssue(title="Tool A", body="Body A", confidence=9),
        ])
    ))

    with patch("cai.workflows.sourcing._sourcing_agent", return_value=fake_agent):
        ctx = GraphRunContext(
            state=SourcingState(bot=MagicMock(), repo="o/r", prompt="p"),
            deps=None,
        )
        node = RunSourcingNode()
        result = asyncio.run(node.run(ctx))

    fake_agent.run.assert_called_once_with("p")
    assert isinstance(result, CreateIssuesNode)
    assert ctx.state.output is not None
    assert len(ctx.state.output.issues) == 1
    assert ctx.state.output.issues[0].title == "Tool A"


def test_run_sourcing_node_no_issues_returns_end():
    """RunSourcingNode returns End when the agent proposes no issues."""
    from pydantic_graph import End, GraphRunContext

    fake_agent = MagicMock()
    fake_agent.run = AsyncMock(return_value=MagicMock(
        output=_SourcingInnerOutput(issues=[])
    ))

    with patch("cai.workflows.sourcing._sourcing_agent", return_value=fake_agent):
        ctx = GraphRunContext(
            state=SourcingState(bot=MagicMock(), repo="o/r", prompt="p"),
            deps=None,
        )
        node = RunSourcingNode()
        result = asyncio.run(node.run(ctx))

    assert isinstance(result, End)


# ── CreateIssuesNode ─────────────────────────────────────────────────


def test_create_issues_node_new_issue():
    """CreateIssuesNode creates a new issue when dedupe says 'new'."""
    from pydantic_graph import End, GraphRunContext

    fake_dedupe = MagicMock()
    fake_dedupe.run = AsyncMock(return_value=MagicMock(
        output=DedupeOutput(action="new", target_issue_number=None, reason="Brand new")
    ))

    repo_mock = MagicMock()
    repo_mock.get_issues.return_value = []
    created_issue = MagicMock()
    created_issue.html_url = "https://github.com/owner/repo/issues/42"
    repo_mock.create_issue.return_value = created_issue

    bot = MagicMock()
    bot.repo.return_value = repo_mock

    output = SourcingOutput(issues=[
        ProposedIssue(title="Tool A", body="Body A", confidence=9),
    ])

    with patch("cai.workflows.sourcing._dedupe_agent", return_value=fake_dedupe):
        ctx = GraphRunContext(
            state=SourcingState(bot=bot, repo="owner/repo", prompt="p", output=output),
            deps=None,
        )
        node = CreateIssuesNode()
        result = asyncio.run(node.run(ctx))

    assert isinstance(result, End)
    repo_mock.create_issue.assert_called_once_with(
        title="Tool A",
        body="Body A",
        labels=["cai:sourcing", "cai:raised"],
    )


def test_create_issues_node_discard():
    """CreateIssuesNode skips issue creation when dedupe says 'discard'."""
    from pydantic_graph import End, GraphRunContext

    fake_dedupe = MagicMock()
    fake_dedupe.run = AsyncMock(return_value=MagicMock(
        output=DedupeOutput(action="discard", target_issue_number=None, reason="Duplicate")
    ))

    repo_mock = MagicMock()
    repo_mock.get_issues.return_value = []

    bot = MagicMock()
    bot.repo.return_value = repo_mock

    output = SourcingOutput(issues=[
        ProposedIssue(title="Tool A", body="Body A", confidence=5),
    ])

    with patch("cai.workflows.sourcing._dedupe_agent", return_value=fake_dedupe):
        ctx = GraphRunContext(
            state=SourcingState(bot=bot, repo="owner/repo", prompt="p", output=output),
            deps=None,
        )
        node = CreateIssuesNode()
        result = asyncio.run(node.run(ctx))

    assert isinstance(result, End)
    repo_mock.create_issue.assert_not_called()


def test_create_issues_node_append():
    """CreateIssuesNode appends to existing issue when dedupe says 'append'."""
    from pydantic_graph import End, GraphRunContext

    fake_dedupe = MagicMock()
    fake_dedupe.run = AsyncMock(return_value=MagicMock(
        output=DedupeOutput(action="append", target_issue_number=123, reason="Related")
    ))

    existing_issue = MagicMock()
    existing_issue.number = 123
    existing_issue.title = "Existing issue"

    repo_mock = MagicMock()
    repo_mock.get_issues.return_value = [existing_issue]
    repo_mock.get_issue.return_value = existing_issue

    bot = MagicMock()
    bot.repo.return_value = repo_mock

    output = SourcingOutput(issues=[
        ProposedIssue(title="Tool B", body="Body B details", confidence=7),
    ])

    with patch("cai.workflows.sourcing._dedupe_agent", return_value=fake_dedupe):
        ctx = GraphRunContext(
            state=SourcingState(bot=bot, repo="owner/repo", prompt="p", output=output),
            deps=None,
        )
        node = CreateIssuesNode()
        result = asyncio.run(node.run(ctx))

    assert isinstance(result, End)
    repo_mock.create_issue.assert_not_called()
    existing_issue.create_comment.assert_called_once_with(
        "**Additional proposed issue details:**\n\n**Title**: Tool B\n\n**Body**:\nBody B details"
    )


def test_create_issues_node_append_no_target_falls_back():
    """When dedupe says 'append' but provides no target_issue_number,
    CreateIssuesNode falls back to creating a new issue."""
    from pydantic_graph import End, GraphRunContext

    fake_dedupe = MagicMock()
    fake_dedupe.run = AsyncMock(return_value=MagicMock(
        output=DedupeOutput(action="append", target_issue_number=None, reason="Related")
    ))

    repo_mock = MagicMock()
    repo_mock.get_issues.return_value = []
    created_issue = MagicMock()
    created_issue.html_url = "https://github.com/owner/repo/issues/99"
    repo_mock.create_issue.return_value = created_issue

    bot = MagicMock()
    bot.repo.return_value = repo_mock

    output = SourcingOutput(issues=[
        ProposedIssue(title="Tool C", body="Body C", confidence=8),
    ])

    with patch("cai.workflows.sourcing._dedupe_agent", return_value=fake_dedupe):
        ctx = GraphRunContext(
            state=SourcingState(bot=bot, repo="owner/repo", prompt="p", output=output),
            deps=None,
        )
        node = CreateIssuesNode()
        result = asyncio.run(node.run(ctx))

    assert isinstance(result, End)
    repo_mock.create_issue.assert_called_once_with(
        title="Tool C",
        body="Body C",
        labels=["cai:sourcing", "cai:human-review"],
    )


def test_create_issues_node_multiple_issues():
    """CreateIssuesNode processes multiple issues with mixed dedupe outcomes."""
    from pydantic_graph import End, GraphRunContext

    fake_dedupe = MagicMock()
    fake_dedupe.run = AsyncMock(side_effect=[
        MagicMock(output=DedupeOutput(action="new", target_issue_number=None, reason="New")),
        MagicMock(output=DedupeOutput(action="discard", target_issue_number=None, reason="Dup")),
        MagicMock(output=DedupeOutput(action="new", target_issue_number=None, reason="Also new")),
    ])

    created_issue_1 = MagicMock()
    created_issue_1.html_url = "https://github.com/owner/repo/issues/1"
    created_issue_2 = MagicMock()
    created_issue_2.html_url = "https://github.com/owner/repo/issues/3"

    repo_mock = MagicMock()
    repo_mock.get_issues.return_value = []
    repo_mock.create_issue.side_effect = [created_issue_1, created_issue_2]

    bot = MagicMock()
    bot.repo.return_value = repo_mock

    output = SourcingOutput(issues=[
        ProposedIssue(title="Issue 1", body="Body 1", confidence=9),
        ProposedIssue(title="Issue 2", body="Body 2", confidence=6),
        ProposedIssue(title="Issue 3", body="Body 3", confidence=10),
    ])

    with patch("cai.workflows.sourcing._dedupe_agent", return_value=fake_dedupe):
        ctx = GraphRunContext(
            state=SourcingState(bot=bot, repo="owner/repo", prompt="p", output=output),
            deps=None,
        )
        node = CreateIssuesNode()
        result = asyncio.run(node.run(ctx))

    assert isinstance(result, End)
    assert repo_mock.create_issue.call_count == 2
    # First call — confidence 9 → cai:raised
    assert repo_mock.create_issue.call_args_list[0][1] == {
        "title": "Issue 1", "body": "Body 1",
        "labels": ["cai:sourcing", "cai:raised"],
    }
    # Second call — confidence 10 → cai:raised
    assert repo_mock.create_issue.call_args_list[1][1] == {
        "title": "Issue 3", "body": "Body 3",
        "labels": ["cai:sourcing", "cai:raised"],
    }


def test_create_issues_node_low_confidence_uses_human_review():
    """Issues with confidence < 9 get cai:human-review label."""
    from pydantic_graph import End, GraphRunContext

    fake_dedupe = MagicMock()
    fake_dedupe.run = AsyncMock(return_value=MagicMock(
        output=DedupeOutput(action="new", target_issue_number=None, reason="New")
    ))

    repo_mock = MagicMock()
    repo_mock.get_issues.return_value = []
    created_issue = MagicMock()
    created_issue.html_url = "https://github.com/owner/repo/issues/7"
    repo_mock.create_issue.return_value = created_issue

    bot = MagicMock()
    bot.repo.return_value = repo_mock

    output = SourcingOutput(issues=[
        ProposedIssue(title="Low Conf", body="Body", confidence=3),
    ])

    with patch("cai.workflows.sourcing._dedupe_agent", return_value=fake_dedupe):
        ctx = GraphRunContext(
            state=SourcingState(bot=bot, repo="owner/repo", prompt="p", output=output),
            deps=None,
        )
        node = CreateIssuesNode()
        asyncio.run(node.run(ctx))

    repo_mock.create_issue.assert_called_once_with(
        title="Low Conf",
        body="Body",
        labels=["cai:sourcing", "cai:human-review"],
    )


# ── main CLI entry ───────────────────────────────────────────────────


@patch("sys.argv", ["cai-sourcing", "--repo", "owner/repo"])
def test_main_runs_graph(
):
    """main() sets up Langfuse, builds a prompt, creates a CaiBot, and runs the graph."""
    with patch("cai.workflows.sourcing.setup_langfuse") as mock_setup:
        with patch("cai.workflows.sourcing.CaiBot") as mock_bot_cls:
            bot_instance = MagicMock()
            mock_bot_cls.return_value = bot_instance

            with patch("cai.workflows.sourcing.langfuse_workflow") as mock_lf:
                with patch("cai.workflows.sourcing.sourcing_graph") as mock_graph:
                    mock_graph.run = AsyncMock()

                    main()

    mock_setup.assert_called_once()
    mock_bot_cls.assert_called_once()
    mock_lf.assert_called_once()
    mock_graph.run.assert_called_once()
    # Verify the graph is started at RunSourcingNode
    call_args = mock_graph.run.call_args
    assert isinstance(call_args[0][0], RunSourcingNode)


@patch("sys.argv", ["cai-sourcing", "--repo", "owner/repo"])
def test_main_session_id_format():
    """main() creates a session id matching sourcing-YYYYMMDD-HHMMSS."""
    import re

    with patch("cai.workflows.sourcing.setup_langfuse"):
        with patch("cai.workflows.sourcing.CaiBot"):
            with patch("cai.workflows.sourcing.langfuse_workflow") as mock_lf:
                with patch("cai.workflows.sourcing.sourcing_graph") as mock_graph:
                    mock_graph.run = AsyncMock()
                    main()

    call_kwargs = mock_lf.call_args[1]
    session_id = call_kwargs["session_id"]
    assert re.match(r"^sourcing-\d{8}-\d{6}$", session_id), f"unexpected format: {session_id!r}"


@patch("sys.argv", ["cai-sourcing", "--repo", "owner/repo"])
def test_main_metadata_includes_repo():
    """main() passes the repo in the langfuse_workflow metadata."""
    with patch("cai.workflows.sourcing.setup_langfuse"):
        with patch("cai.workflows.sourcing.CaiBot"):
            with patch("cai.workflows.sourcing.langfuse_workflow") as mock_lf:
                with patch("cai.workflows.sourcing.sourcing_graph") as mock_graph:
                    mock_graph.run = AsyncMock()
                    main()

    call_kwargs = mock_lf.call_args
    assert call_kwargs[1]["metadata"] == {"repo": "owner/repo"}
