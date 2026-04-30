from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from pydantic_graph import End

from cai.github.issues import IssueMeta
from cai.workflows.refine import RefineNode, refine_agent
from cai.workflows.state import ExploreOutput, IssueState, RefineOutput


@pytest.fixture(autouse=True)
def _reset_agent_cache():
    refine_agent.cache_clear()
    yield
    refine_agent.cache_clear()


@pytest.fixture
def state(tmp_path: Path) -> IssueState:
    body = tmp_path / "42.md"
    body.write_text("## Issue body\n")
    meta = IssueMeta(repo="owner/repo", number=42, title="Original title", labels=["cai:raised"])
    bot = MagicMock()
    bot.token_for.return_value = "tok"
    s = IssueState(
        bot=bot,
        meta=meta,
        body_path=body,
        repo_root=tmp_path,
        meta_json='{"number": 42}',
        body="## Issue body\n",
    )
    s.findings = ExploreOutput(summary="Some findings.", related_files=[])
    s.reference_files = []
    return s


def _run(node, state):
    ctx = MagicMock()
    ctx.state = state
    return asyncio.run(node.run(ctx))


@patch("cai.workflows.refine.add_sub_issue")
@patch("cai.workflows.refine.push")
@patch("cai.workflows.refine.refine_agent")
def test_only_first_sub_issue_gets_cai_raised(mock_agent_factory, mock_push, mock_add_sub_issue, state, tmp_path):
    """Only the first sub-issue inherits ``cai:raised``; followups have it stripped."""
    # Set up the agent mock to return two sub-issues
    agent_instance = MagicMock()
    mock_agent_factory.return_value = agent_instance

    async def mock_run(*args, **kwargs):
        result = MagicMock()
        result.output = RefineOutput(
            title="Refined title",
            reference_files=[],
            sub_issues=["Sub-task A", "Sub-task B"],
        )
        return result

    agent_instance.run = mock_run

    # push returns an Issue-like object with an id
    def push_side_effect(bot, json_path):
        issue = MagicMock()
        issue.id = 1001
        issue.number = 10
        # Write back a number so push doesn't fail on re-read
        meta = IssueMeta.model_validate_json(Path(json_path).read_text())
        meta.number = 10
        Path(json_path).write_text(meta.model_dump_json(indent=2) + "\n")
        return issue

    mock_push.side_effect = push_side_effect

    result = _run(RefineNode(), state)

    assert isinstance(result, End)

    # Check that both sub-issues were created with the parent's labels
    sub_json_0 = tmp_path / "sub_issue_0.json"
    sub_json_1 = tmp_path / "sub_issue_1.json"
    assert sub_json_0.exists()
    assert sub_json_1.exists()

    meta_0 = IssueMeta.model_validate_json(sub_json_0.read_text())
    meta_1 = IssueMeta.model_validate_json(sub_json_1.read_text())

    assert meta_0.labels == ["cai:raised"]
    assert meta_1.labels == []


@patch("cai.workflows.refine.add_sub_issue")
@patch("cai.workflows.refine.push")
@patch("cai.workflows.refine.refine_agent")
def test_followup_sub_issues_drop_cai_raised_but_keep_other_labels(mock_agent_factory, mock_push, mock_add_sub_issue, tmp_path):
    """First sub-issue keeps all parent labels; followups keep everything except ``cai:raised``."""
    body = tmp_path / "42.md"
    body.write_text("## Issue body\n")
    meta = IssueMeta(
        repo="owner/repo",
        number=42,
        title="Original title",
        labels=["cai:raised", "bug", "priority:high"],
    )
    bot = MagicMock()
    s = IssueState(
        bot=bot,
        meta=meta,
        body_path=body,
        repo_root=tmp_path,
        meta_json='{"number": 42}',
        body="## Issue body\n",
    )
    s.findings = ExploreOutput(summary="findings", related_files=[])
    s.reference_files = []

    agent_instance = MagicMock()
    mock_agent_factory.return_value = agent_instance

    async def mock_run(*args, **kwargs):
        result = MagicMock()
        result.output = RefineOutput(
            title="Refined title",
            reference_files=[],
            sub_issues=["Sub-task X", "Sub-task Y", "Sub-task Z"],
        )
        return result

    agent_instance.run = mock_run

    def push_side_effect(bot, json_path):
        issue = MagicMock()
        issue.id = 2001
        issue.number = 20
        meta_local = IssueMeta.model_validate_json(Path(json_path).read_text())
        meta_local.number = 20
        Path(json_path).write_text(meta_local.model_dump_json(indent=2) + "\n")
        return issue

    mock_push.side_effect = push_side_effect

    result = _run(RefineNode(), s)

    assert isinstance(result, End)

    sub_meta_0 = IssueMeta.model_validate_json((tmp_path / "sub_issue_0.json").read_text())
    assert sub_meta_0.labels == ["cai:raised", "bug", "priority:high"]
    for i in (1, 2):
        sub_meta = IssueMeta.model_validate_json((tmp_path / f"sub_issue_{i}.json").read_text())
        assert sub_meta.labels == ["bug", "priority:high"]


@patch("cai.workflows.refine.add_sub_issue")
@patch("cai.workflows.refine.push")
@patch("cai.workflows.refine.refine_agent")
def test_sub_issues_inherit_no_labels_when_parent_has_none(mock_agent_factory, mock_push, mock_add_sub_issue, tmp_path):
    """Sub-issues inherit empty labels if the parent has none."""
    body = tmp_path / "42.md"
    body.write_text("## Issue body\n")
    meta = IssueMeta(
        repo="owner/repo",
        number=42,
        title="Original title",
        labels=[],
    )
    bot = MagicMock()
    s = IssueState(
        bot=bot,
        meta=meta,
        body_path=body,
        repo_root=tmp_path,
        meta_json='{"number": 42}',
        body="## Issue body\n",
    )
    s.findings = ExploreOutput(summary="findings", related_files=[])
    s.reference_files = []

    agent_instance = MagicMock()
    mock_agent_factory.return_value = agent_instance

    async def mock_run(*args, **kwargs):
        result = MagicMock()
        result.output = RefineOutput(
            title="Refined title",
            reference_files=[],
            sub_issues=["Sub-task A"],
        )
        return result

    agent_instance.run = mock_run

    def push_side_effect(bot, json_path):
        issue = MagicMock()
        issue.id = 3001
        issue.number = 30
        meta_local = IssueMeta.model_validate_json(Path(json_path).read_text())
        meta_local.number = 30
        Path(json_path).write_text(meta_local.model_dump_json(indent=2) + "\n")
        return issue

    mock_push.side_effect = push_side_effect

    result = _run(RefineNode(), s)

    assert isinstance(result, End)

    sub_json = tmp_path / "sub_issue_0.json"
    sub_meta = IssueMeta.model_validate_json(sub_json.read_text())
    assert sub_meta.labels == []


@patch("cai.workflows.refine.add_sub_issue")
@patch("cai.workflows.refine.push")
@patch("cai.workflows.refine.refine_agent")
def test_prompt_includes_session_id(mock_agent_factory, mock_push, mock_add_sub_issue, state):
    """The prompt passed to the refine agent includes the session ID for trace inspection."""
    agent_instance = MagicMock()
    mock_agent_factory.return_value = agent_instance

    captured_prompt = None

    async def mock_run(prompt, *args, **kwargs):
        nonlocal captured_prompt
        captured_prompt = prompt
        result = MagicMock()
        result.output = RefineOutput(
            title="Refined title",
            reference_files=[],
            sub_issues=[],
        )
        return result

    agent_instance.run = mock_run

    def push_side_effect(bot, json_path):
        issue = MagicMock()
        issue.id = 4001
        issue.number = 40
        meta_local = IssueMeta.model_validate_json(Path(json_path).read_text())
        meta_local.number = 40
        Path(json_path).write_text(meta_local.model_dump_json(indent=2) + "\n")
        return issue

    mock_push.side_effect = push_side_effect

    _run(RefineNode(), state)

    assert captured_prompt is not None
    assert "issue-42" in captured_prompt
    assert "## Session" in captured_prompt
    assert "traces_session" in captured_prompt
    assert "traces_solve_sessions" in captured_prompt
    # Reference files section should be absent when there are no reference files
    assert "## Reference files" not in captured_prompt


@patch("cai.workflows.refine.add_sub_issue")
@patch("cai.workflows.refine.push")
@patch("cai.workflows.refine.refine_agent")
def test_prompt_includes_reference_files_section_when_present(mock_agent_factory, mock_push, mock_add_sub_issue, state, tmp_path):
    """When reference files exist, the prompt includes a reference files section after the session section."""
    # Create a real reference file on disk
    ref_file = tmp_path / "src" / "example.py"
    ref_file.parent.mkdir(parents=True, exist_ok=True)
    ref_file.write_text("def foo():\n    return 42\n")
    state.reference_files = ["src/example.py"]

    agent_instance = MagicMock()
    mock_agent_factory.return_value = agent_instance

    captured_prompt = None

    async def mock_run(prompt, *args, **kwargs):
        nonlocal captured_prompt
        captured_prompt = prompt
        result = MagicMock()
        result.output = RefineOutput(
            title="Refined title",
            reference_files=[],
            sub_issues=[],
        )
        return result

    agent_instance.run = mock_run

    def push_side_effect(bot, json_path):
        issue = MagicMock()
        issue.id = 5001
        issue.number = 50
        meta_local = IssueMeta.model_validate_json(Path(json_path).read_text())
        meta_local.number = 50
        Path(json_path).write_text(meta_local.model_dump_json(indent=2) + "\n")
        return issue

    mock_push.side_effect = push_side_effect

    _run(RefineNode(), state)

    assert captured_prompt is not None
    assert "## Session" in captured_prompt
    assert "issue-42" in captured_prompt
    assert "## Reference files" in captured_prompt
    # Session section must appear before reference files section
    session_idx = captured_prompt.index("## Session")
    ref_idx = captured_prompt.index("## Reference files")
    assert session_idx < ref_idx, "Session section must precede reference files section"
