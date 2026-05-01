from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import pytest
from pydantic_ai.usage import UsageLimits

from cai.workflows.docs import DocsNode, _docs_agent
from cai.workflows.pr import PRNode
from cai.workflows.state import DocsOutput, ImplementOutput


def _run(node, state):
    ctx = MagicMock()
    ctx.state = state
    return asyncio.run(node.run(ctx))


@pytest.fixture(autouse=True)
def _reset_agent_cache():
    _docs_agent.cache_clear()
    yield
    _docs_agent.cache_clear()


# ---------------------------------------------------------------------------
# _docs_agent — output_retries configuration
# ---------------------------------------------------------------------------


def test_docs_agent_passes_output_retries():
    """_docs_agent() passes output_retries=3 to build_deep_agent for
    structured-output resilience, matching the refine agent's pattern."""
    _docs_agent.cache_clear()

    with patch("cai.workflows.docs.build_deep_agent") as mock_build:
        mock_build.return_value = MagicMock()
        _docs_agent()

    mock_build.assert_called_once()
    assert mock_build.call_args[1].get("output_retries") == 3

    _docs_agent.cache_clear()


# ---------------------------------------------------------------------------
# DocsNode — prompt construction
# ---------------------------------------------------------------------------


@patch("cai.workflows.docs._docs_agent")
def test_docs_node_prompt_includes_implement_output(mock_agent_factory, state):
    """The prompt passed to the docs agent includes the implementation
    summary and commit message."""
    agent_instance = MagicMock()
    mock_agent_factory.return_value = agent_instance

    state.implement_output = ImplementOutput(
        summary="Added a new --timeout flag to the CLI.",
        commit_message="feat: add --timeout flag to CLI",
    )

    captured_prompt = None

    async def mock_run(prompt, *args, **kwargs):
        nonlocal captured_prompt
        captured_prompt = prompt
        result = MagicMock()
        result.output = DocsOutput(
            summary="Updated docs/cli.md",
            commit_message="docs: document --timeout flag",
        )
        return result

    agent_instance.run = mock_run

    _run(DocsNode(), state)

    assert captured_prompt is not None
    assert "Added a new --timeout flag to the CLI." in captured_prompt
    assert "feat: add --timeout flag to CLI" in captured_prompt


@patch("cai.workflows.docs._docs_agent")
def test_docs_node_prompt_includes_issue_metadata(mock_agent_factory, state):
    """The prompt includes the refined issue metadata as JSON."""
    agent_instance = MagicMock()
    mock_agent_factory.return_value = agent_instance

    state.implement_output = ImplementOutput(
        summary="Some changes.",
        commit_message="fix: changes",
    )

    captured_prompt = None

    async def mock_run(prompt, *args, **kwargs):
        nonlocal captured_prompt
        captured_prompt = prompt
        result = MagicMock()
        result.output = DocsOutput(summary="Updated docs.", commit_message="docs: update")
        return result

    agent_instance.run = mock_run

    _run(DocsNode(), state)

    assert captured_prompt is not None
    assert "## Issue metadata" in captured_prompt
    assert '"number": 99' in captured_prompt or "o/r" in captured_prompt


@patch("cai.workflows.docs._docs_agent")
def test_docs_node_prompt_includes_issue_body(mock_agent_factory, state, tmp_path):
    """The prompt includes the body text from the issue body file."""
    agent_instance = MagicMock()
    mock_agent_factory.return_value = agent_instance

    state.implement_output = ImplementOutput(
        summary="Changes.",
        commit_message="fix: changes",
    )

    # Write some body text
    state.body_path.write_text("## Issue body\n\nAdd the --timeout flag.")

    captured_prompt = None

    async def mock_run(prompt, *args, **kwargs):
        nonlocal captured_prompt
        captured_prompt = prompt
        result = MagicMock()
        result.output = DocsOutput(summary="Updated docs.", commit_message="docs: update")
        return result

    agent_instance.run = mock_run

    _run(DocsNode(), state)

    assert captured_prompt is not None
    assert "## Issue body (plan)" in captured_prompt
    assert "Add the --timeout flag." in captured_prompt


@patch("cai.workflows.docs._docs_agent")
def test_docs_node_prompt_contains_sections_in_order(mock_agent_factory, state):
    """The prompt sections appear in the expected order: metadata, body,
    implementation summary, implementation commit message."""
    agent_instance = MagicMock()
    mock_agent_factory.return_value = agent_instance

    state.implement_output = ImplementOutput(
        summary="Changes summary.",
        commit_message="fix: changes",
    )

    captured_prompt = None

    async def mock_run(prompt, *args, **kwargs):
        nonlocal captured_prompt
        captured_prompt = prompt
        result = MagicMock()
        result.output = DocsOutput(summary="Updated docs.", commit_message="docs: update")
        return result

    agent_instance.run = mock_run

    _run(DocsNode(), state)

    assert captured_prompt is not None
    meta_idx = captured_prompt.index("## Issue metadata")
    body_idx = captured_prompt.index("## Issue body (plan)")
    summary_idx = captured_prompt.index("## Implementation summary")
    commit_idx = captured_prompt.index("## Implementation commit message")

    assert meta_idx < body_idx < summary_idx < commit_idx


# ---------------------------------------------------------------------------
# DocsNode — transition and state
# ---------------------------------------------------------------------------


@patch("cai.workflows.docs._docs_agent")
def test_docs_node_returns_pr_node(mock_agent_factory, state):
    """DocsNode.run() must return a PRNode on success."""
    agent_instance = MagicMock()
    mock_agent_factory.return_value = agent_instance

    state.implement_output = ImplementOutput(
        summary="Changed the timeout.",
        commit_message="fix: timeout",
    )

    async def mock_run(prompt, *args, **kwargs):
        result = MagicMock()
        result.output = DocsOutput(summary="Updated docs.", commit_message="docs: update")
        return result

    agent_instance.run = mock_run

    result = _run(DocsNode(), state)

    assert isinstance(result, PRNode)


@patch("cai.workflows.docs._docs_agent")
def test_docs_node_sets_state_docs_output(mock_agent_factory, state):
    """After a successful run, state.docs_output must be populated with
    the agent's output."""
    agent_instance = MagicMock()
    mock_agent_factory.return_value = agent_instance

    state.implement_output = ImplementOutput(
        summary="Changed the timeout.",
        commit_message="fix: timeout",
    )

    expected_output = DocsOutput(
        summary="Updated docs/cli.md to cover the new `--timeout` flag.",
        commit_message="docs: document --timeout flag in cli.md",
    )

    async def mock_run(prompt, *args, **kwargs):
        result = MagicMock()
        result.output = expected_output
        return result

    agent_instance.run = mock_run

    _run(DocsNode(), state)

    assert state.docs_output is not None
    assert state.docs_output.summary == expected_output.summary
    assert state.docs_output.commit_message == expected_output.commit_message


@patch("cai.workflows.docs._docs_agent")
def test_docs_node_uses_request_limit(mock_agent_factory, state):
    """The docs agent is called with UsageLimits(request_limit=50)."""
    agent_instance = MagicMock()
    mock_agent_factory.return_value = agent_instance

    state.implement_output = ImplementOutput(
        summary="Changes.",
        commit_message="fix: changes",
    )

    async def mock_run(prompt, *args, **kwargs):
        result = MagicMock()
        result.output = DocsOutput(summary="Updated docs.", commit_message="docs: update")
        return result

    agent_instance.run = MagicMock(side_effect=mock_run)

    _run(DocsNode(), state)

    agent_instance.run.assert_called_once()
    _, kwargs = agent_instance.run.call_args
    assert "usage_limits" in kwargs
    assert isinstance(kwargs["usage_limits"], UsageLimits)
    assert kwargs["usage_limits"].request_limit == 50


# ---------------------------------------------------------------------------
# _deps helper
# ---------------------------------------------------------------------------


def test_deps_creates_deep_agent_deps_with_local_backend(tmp_path):
    """_deps() returns a DeepAgentDeps using a LocalBackend pointed at the
    given repo root."""
    from cai.workflows.docs import _deps

    deps = _deps(tmp_path)
    assert deps.backend is not None
    assert str(deps.backend.root_dir) == str(tmp_path)
    assert tmp_path in deps.backend._allowed_directories


# ---------------------------------------------------------------------------
# State assertions — _docs_agent raises without required state fields
# ---------------------------------------------------------------------------


def test_docs_node_raises_without_new_meta(state):
    """DocsNode.run() raises AssertionError when state.new_meta is None."""
    state.new_meta = None
    state.implement_output = ImplementOutput(
        summary="Changes.",
        commit_message="fix: changes",
    )

    with pytest.raises(AssertionError):
        _run(DocsNode(), state)


def test_docs_node_raises_without_implement_output(state):
    """DocsNode.run() raises AssertionError when state.implement_output is None."""
    state.implement_output = None

    with pytest.raises(AssertionError):
        _run(DocsNode(), state)
