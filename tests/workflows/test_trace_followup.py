"""Tests for the cai-trace-followup workflow.

Covers body parse/write helpers, the prompt builder, and the end-to-end
graph behavior with a mocked agent and GitHub repo.
"""
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cai.workflows.trace_followup import (
    FollowupNode,
    ReproductionResult,
    TraceFollowupState,
    _build_followup_prompt,
    _format_reproduction_comment,
    _IssueContext,
    _parse_issue_metadata,
    _set_metadata_line,
    _trace_followup_agent,
    _yesterday_window,
    main,
)


@pytest.fixture(autouse=True)
def _reset_agent_cache():
    """Agent factory is lru_cached so a fresh patch lands per test."""
    _trace_followup_agent.cache_clear()
    yield
    _trace_followup_agent.cache_clear()


# ── _parse_issue_metadata ───────────────────────────────────────────────


def test_parse_issue_metadata_no_section():
    body = "Some plain body text\nwithout any trace section."
    meta = _parse_issue_metadata(body)
    assert meta == {"trace_ids": [], "first_observed": None, "trace_filter": None}


def test_parse_issue_metadata_full():
    body = (
        "Original body content describing a symptom.\n\n"
        "## Relevant Traces\n\n"
        "Symptom drawn from the following Langfuse traces. "
        "Inspect them (`traces_show <id>`) to confirm the issue is real.\n\n"
        "**First observed**: 2026-05-03T08:00:00+00:00\n"
        "**Trace filter**: tool errors in Bash from cai-solve\n\n"
        "- `trace-abc-1`\n"
        "- `trace-abc-2`\n"
    )
    meta = _parse_issue_metadata(body)
    assert meta["trace_ids"] == ["trace-abc-1", "trace-abc-2"]
    assert meta["first_observed"] == "2026-05-03T08:00:00+00:00"
    assert meta["trace_filter"] == "tool errors in Bash from cai-solve"


def test_parse_issue_metadata_only_section_header():
    body = "## Relevant Traces\n\nNo bullets, no metadata.\n"
    meta = _parse_issue_metadata(body)
    assert meta == {"trace_ids": [], "first_observed": None, "trace_filter": None}


def test_parse_issue_metadata_ignores_bullets_outside_section():
    """Trace bullets above the section header must not be picked up."""
    body = (
        "Some intro that mentions `- `not-a-trace`` in passing.\n"
        "- `noise-bullet`\n\n"
        "## Relevant Traces\n\n"
        "**Trace filter**: x\n\n"
        "- `real-trace-1`\n"
    )
    meta = _parse_issue_metadata(body)
    assert meta["trace_ids"] == ["real-trace-1"]
    assert meta["trace_filter"] == "x"


# ── _set_metadata_line ─────────────────────────────────────────────────


def test_set_metadata_line_replaces_existing():
    body = (
        "## Relevant Traces\n\n"
        "**First observed**: 2026-05-03T08:00:00+00:00\n"
        "**Last checked**: 2026-05-04T07:00:00+00:00\n\n"
        "- `t-1`\n"
    )
    out = _set_metadata_line(body, "Last checked", "2026-05-05T07:00:00+00:00")
    assert "**Last checked**: 2026-05-05T07:00:00+00:00" in out
    # The old value is gone (only one Last checked line remains)
    assert out.count("**Last checked**:") == 1


def test_set_metadata_line_inserts_before_first_bullet():
    body = (
        "Original body.\n\n"
        "## Relevant Traces\n\n"
        "**First observed**: 2026-05-03T08:00:00+00:00\n\n"
        "- `t-1`\n"
        "- `t-2`\n"
    )
    out = _set_metadata_line(body, "Last checked", "2026-05-05T07:00:00+00:00")
    assert "**Last checked**: 2026-05-05T07:00:00+00:00" in out
    # Inserted before the first bullet, not after
    assert out.index("Last checked") < out.index("- `t-1`")


def test_set_metadata_line_appends_when_no_bullets():
    body = "Plain body without any trace bullets."
    out = _set_metadata_line(body, "Last checked", "2026-05-05T07:00:00+00:00")
    assert out.endswith("**Last checked**: 2026-05-05T07:00:00+00:00\n")


# ── _yesterday_window ──────────────────────────────────────────────────


def test_yesterday_window_returns_full_previous_utc_day():
    now = datetime(2026, 5, 5, 14, 30, tzinfo=timezone.utc)
    start, end = _yesterday_window(now=now)
    assert start == datetime(2026, 5, 4, 0, 0, tzinfo=timezone.utc)
    assert end == datetime(2026, 5, 5, 0, 0, tzinfo=timezone.utc)


# ── _build_followup_prompt ─────────────────────────────────────────────


def test_build_followup_prompt_includes_all_signals():
    issue = _IssueContext(
        number=42,
        title="DeepSeek hallucinates final return",
        body="Symptom: agents emit prose instead of the final tool call.",
        original_trace_ids=["abc-1", "abc-2"],
        first_observed="2026-05-03T08:00:00+00:00",
        trace_filter="cai-solve traces where DeepSeek emits prose",
    )
    prompt = _build_followup_prompt(
        issue, "2026-05-04T00:00:00+00:00", "2026-05-05T00:00:00+00:00"
    )
    assert "issue #42" in prompt
    assert "DeepSeek hallucinates final return" in prompt
    assert "Symptom: agents emit prose" in prompt
    assert "`abc-1`" in prompt and "`abc-2`" in prompt
    assert "2026-05-03T08:00:00+00:00" in prompt
    assert "cai-solve traces where DeepSeek emits prose" in prompt
    assert "2026-05-04T00:00:00+00:00" in prompt
    assert "2026-05-05T00:00:00+00:00" in prompt
    assert "trace_analyst" in prompt


def test_build_followup_prompt_handles_missing_metadata():
    issue = _IssueContext(
        number=7, title="t", body="b",
        original_trace_ids=[],
        first_observed=None,
        trace_filter=None,
    )
    prompt = _build_followup_prompt(issue, "s", "u")
    assert "(none recorded)" in prompt
    assert "First observed: unknown" in prompt
    assert "(no hint provided)" in prompt


# ── _format_reproduction_comment ───────────────────────────────────────


def test_format_reproduction_comment_lists_supporting_traces():
    result = ReproductionResult(
        reproduced=True,
        supporting_trace_ids=["new-1", "new-2"],
        notes="Two new traces show the same prose-instead-of-tool-call pattern.",
    )
    comment = _format_reproduction_comment(result, "2026-05-05T07:00:00+00:00")
    assert "Trace follow-up" in comment
    assert "Checked on 2026-05-05T07:00:00+00:00" in comment
    assert "Two new traces" in comment
    assert "- `new-1`" in comment
    assert "- `new-2`" in comment


# ── End-to-end FollowupNode ────────────────────────────────────────────


def _make_issue(number, title, body):
    """Build a MagicMock GitHub issue with attribute assignment, since
    the repo's get_issues iteration consumes them by attribute access."""
    issue = MagicMock()
    issue.number = number
    issue.title = title
    issue.body = body
    return issue


def _run_followup(state):
    import asyncio
    from pydantic_graph import GraphRunContext
    ctx = GraphRunContext(state=state, deps=None)
    return asyncio.run(FollowupNode().run(ctx))


def test_followup_node_no_open_issues():
    """When there are no open trace-investigation issues, the agent is never run."""
    repo_mock = MagicMock()
    repo_mock.get_issues.return_value = []
    bot = MagicMock()
    bot.repo.return_value = repo_mock

    state = TraceFollowupState(bot=bot, repo="owner/repo")

    with patch("cai.workflows.trace_followup._trace_followup_agent") as agent_factory:
        end = _run_followup(state)

    assert end.data is state
    assert state.issues_processed == 0
    assert state.reproductions == 0
    agent_factory.assert_not_called()
    repo_mock.get_issues.assert_called_once_with(
        state="open", labels=["cai:trace-investigation"]
    )


def test_followup_node_reproduced_posts_comment_and_updates_body():
    body = (
        "Symptom description.\n\n"
        "## Relevant Traces\n\n"
        "**First observed**: 2026-05-03T08:00:00+00:00\n"
        "**Trace filter**: tool errors in Bash\n\n"
        "- `original-trace`\n"
    )
    issue_mock = _make_issue(101, "Bash tool errors", body)
    repo_mock = MagicMock()
    repo_mock.get_issues.return_value = [issue_mock]
    bot = MagicMock()
    bot.repo.return_value = repo_mock

    fake_agent = MagicMock()
    fake_agent.run = AsyncMock(return_value=MagicMock(output=ReproductionResult(
        reproduced=True,
        supporting_trace_ids=["new-trace-1"],
        notes="Saw the same Bash exit-code-127 pattern in this trace.",
    )))

    state = TraceFollowupState(bot=bot, repo="owner/repo")

    with patch(
        "cai.workflows.trace_followup._trace_followup_agent",
        return_value=fake_agent,
    ):
        _run_followup(state)

    assert state.issues_processed == 1
    assert state.reproductions == 1
    assert len(state.results) == 1
    issue_num, result = state.results[0]
    assert issue_num == 101
    assert result.reproduced is True
    assert result.supporting_trace_ids == ["new-trace-1"]

    # A reproduction comment was posted
    issue_mock.create_comment.assert_called_once()
    comment = issue_mock.create_comment.call_args[0][0]
    assert "Trace follow-up" in comment
    assert "- `new-trace-1`" in comment

    # Body was edited with both Last checked and Last reproduced metadata
    issue_mock.edit.assert_called_once()
    new_body = issue_mock.edit.call_args.kwargs["body"]
    assert "**Last checked**:" in new_body
    assert "**Last reproduced**:" in new_body
    # Original metadata preserved
    assert "**First observed**: 2026-05-03T08:00:00+00:00" in new_body
    assert "**Trace filter**: tool errors in Bash" in new_body
    assert "- `original-trace`" in new_body


def test_followup_node_not_reproduced_updates_only_last_checked():
    body = (
        "Symptom.\n\n"
        "## Relevant Traces\n\n"
        "**First observed**: 2026-05-03T08:00:00+00:00\n\n"
        "- `original-trace`\n"
    )
    issue_mock = _make_issue(202, "Quiet symptom", body)
    repo_mock = MagicMock()
    repo_mock.get_issues.return_value = [issue_mock]
    bot = MagicMock()
    bot.repo.return_value = repo_mock

    fake_agent = MagicMock()
    fake_agent.run = AsyncMock(return_value=MagicMock(output=ReproductionResult(
        reproduced=False,
        supporting_trace_ids=[],
        notes="No yesterday traces matched the filter hint.",
    )))

    state = TraceFollowupState(bot=bot, repo="owner/repo")

    with patch(
        "cai.workflows.trace_followup._trace_followup_agent",
        return_value=fake_agent,
    ):
        _run_followup(state)

    assert state.issues_processed == 1
    assert state.reproductions == 0
    issue_mock.create_comment.assert_not_called()
    issue_mock.edit.assert_called_once()
    new_body = issue_mock.edit.call_args.kwargs["body"]
    assert "**Last checked**:" in new_body
    assert "**Last reproduced**:" not in new_body


def test_followup_node_agent_failure_continues_to_next_issue():
    """A single agent crash on one issue must not abort the whole run."""
    issue_a = _make_issue(1, "first", "## Relevant Traces\n\n- `t-a`\n")
    issue_b = _make_issue(2, "second", "## Relevant Traces\n\n- `t-b`\n")
    repo_mock = MagicMock()
    repo_mock.get_issues.return_value = [issue_a, issue_b]
    bot = MagicMock()
    bot.repo.return_value = repo_mock

    fake_agent = MagicMock()
    fake_agent.run = AsyncMock(side_effect=[
        RuntimeError("OpenRouter blew up"),
        MagicMock(output=ReproductionResult(
            reproduced=False,
            supporting_trace_ids=[],
            notes="Nothing matched.",
        )),
    ])

    state = TraceFollowupState(bot=bot, repo="owner/repo")

    with patch(
        "cai.workflows.trace_followup._trace_followup_agent",
        return_value=fake_agent,
    ):
        _run_followup(state)

    # The crash on issue 1 was recorded as an unprocessed-by-agent slot but
    # incremented issues_processed (failure path). Issue 2 ran normally.
    assert state.issues_processed == 2
    assert state.reproductions == 0
    issue_a.create_comment.assert_not_called()
    issue_b.create_comment.assert_not_called()


# ── main CLI ────────────────────────────────────────────────────────────


@patch("sys.argv", ["cai-trace-followup", "--repo", "owner/repo"])
def test_main_invokes_graph_with_repo(monkeypatch):
    """The CLI parses --repo, sets up Langfuse, and runs the graph once."""
    fake_bot = MagicMock()
    repo_mock = MagicMock()
    repo_mock.get_issues.return_value = []
    fake_bot.repo.return_value = repo_mock

    with patch("cai.workflows.trace_followup.setup_langfuse") as setup_mock, \
         patch("cai.workflows.trace_followup.CaiBot", return_value=fake_bot), \
         patch("cai.workflows.trace_followup.langfuse_workflow") as lf_workflow:
        main()

    setup_mock.assert_called_once()
    lf_workflow.assert_called_once()
    # The Langfuse session id includes the repo slug
    kwargs = lf_workflow.call_args.kwargs
    assert kwargs["metadata"] == {"repo": "owner/repo"}
    assert "owner-repo" in kwargs["session_id"]
    assert kwargs["session_id"].startswith("trace-followup-")
