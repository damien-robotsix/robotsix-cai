"""Tests for ``cai.workflows.ci_triage`` — the ``cai-ci-triage`` CLI.

Covers the ``CiTriageState`` dataclass, ``FetchAndTriageNode`` logic (HTTP
calls to the GitHub Actions API, log fetching, truncation, agent dispatch),
graph structure, agent factory caching, and the ``main()`` CLI entry point.
"""

from __future__ import annotations

import asyncio
import re
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from pydantic_graph import End, Graph, GraphRunContext

from cai.workflows.ci_triage import (
    CiTriageState,
    FetchAndTriageNode,
    _ci_triage_agent,
    ci_triage_graph,
    main,
)


@pytest.fixture(autouse=True)
def _reset_agent_cache():
    """_ci_triage_agent is lru_cached so a fresh patch lands per test."""
    _ci_triage_agent.cache_clear()
    yield
    _ci_triage_agent.cache_clear()


# ── CiTriageState dataclass ────────────────────────────────────────────


def test_ci_triage_state_construction():
    """CiTriageState stores bot, repo, and run_id."""
    bot = MagicMock()
    state = CiTriageState(bot=bot, repo="owner/repo", run_id=12345)
    assert state.bot is bot
    assert state.repo == "owner/repo"
    assert state.run_id == 12345


# ── ci_triage_graph ────────────────────────────────────────────────────


def test_ci_triage_graph_is_graph_instance():
    """ci_triage_graph is a pydantic_graph.Graph."""
    assert isinstance(ci_triage_graph, Graph)


def test_ci_triage_graph_contains_fetch_and_triage_node():
    """The graph has exactly one node: FetchAndTriageNode."""
    nodes = ci_triage_graph.get_nodes()
    assert len(nodes) == 1
    assert nodes[0] is FetchAndTriageNode


# ── _ci_triage_agent cache ─────────────────────────────────────────────


def test_ci_triage_agent_is_cached():
    """Calling _ci_triage_agent twice returns the same instance."""
    with patch("cai.workflows.ci_triage.build_deep_agent") as mock_build:
        mock_build.return_value = MagicMock()
        agent1 = _ci_triage_agent()
        agent2 = _ci_triage_agent()
        assert agent1 is agent2
        # The factory should only be called once due to lru_cache
        mock_build.assert_called_once()


# ── FetchAndTriageNode ─────────────────────────────────────────────────


def _make_mock_response(status: int = 200, json_data: dict | None = None, text: str = ""):
    """Build an async mock httpx.Response."""
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status
    resp.json = MagicMock(return_value=json_data or {})
    resp.text = text
    resp.raise_for_status = MagicMock()
    return resp


def _build_failed_job(job_id: int, name: str, step_name: str, log_text: str = "") -> dict:
    """Build a minimal failed job dict."""
    return {
        "id": job_id,
        "name": name,
        "conclusion": "failure",
        "steps": [
            {"name": step_name, "conclusion": "failure", "number": 1},
        ],
    }


def test_fetch_and_triage_node_no_failed_jobs():
    """When no jobs have conclusion==failure, the node returns End(None) early."""
    bot = MagicMock()
    bot.token_for.return_value = "gh_token"

    state = CiTriageState(bot=bot, repo="owner/repo", run_id=42)

    jobs_resp = _make_mock_response(json_data={"jobs": [{"id": 1, "name": "passing", "conclusion": "success"}]})

    node = FetchAndTriageNode()
    ctx = GraphRunContext(state=state, deps=None)

    with patch("cai.workflows.ci_triage.httpx.AsyncClient") as mock_client_cls:
        mock_client = MagicMock()
        mock_client.__aenter__.return_value = mock_client
        mock_client_cls.return_value = mock_client
        mock_client.get = AsyncMock(return_value=jobs_resp)

        result = asyncio.run(node.run(ctx))

    assert isinstance(result, End)
    assert result.data is None
    bot.token_for.assert_called_once_with("owner/repo")
    # Only the jobs endpoint should have been called
    mock_client.get.assert_called_once()


def test_fetch_and_triage_node_single_failed_job():
    """A single failed job fetches logs and runs the triage agent."""
    bot = MagicMock()
    bot.token_for.return_value = "gh_token"

    state = CiTriageState(bot=bot, repo="owner/repo", run_id=42)

    failed_job = _build_failed_job(
        job_id=1, name="test", step_name="run_tests", log_text="ERROR: test failed\n"
    )

    jobs_resp = _make_mock_response(
        json_data={"jobs": [failed_job]}
    )
    logs_resp = _make_mock_response(text="ERROR: test failed\n")

    fake_agent = MagicMock()
    fake_agent.run = AsyncMock()

    node = FetchAndTriageNode()
    ctx = GraphRunContext(state=state, deps=None)

    with patch("cai.workflows.ci_triage.httpx.AsyncClient") as mock_client_cls:
        mock_client = MagicMock()
        mock_client.__aenter__.return_value = mock_client
        mock_client_cls.return_value = mock_client
        mock_client.get = AsyncMock(side_effect=[jobs_resp, logs_resp])

        with patch("cai.workflows.ci_triage._ci_triage_agent", return_value=fake_agent):
            result = asyncio.run(node.run(ctx))

    assert isinstance(result, End)
    assert result.data is None

    # Verify two HTTP calls: jobs and logs
    assert mock_client.get.call_count == 2

    # Verify the agent was called with a prompt containing the job log
    fake_agent.run.assert_called_once()
    prompt_arg = fake_agent.run.call_args[0][0]
    assert "Job: test" in prompt_arg
    assert "run_tests" in prompt_arg
    assert "ERROR: test failed" in prompt_arg


def test_fetch_and_triage_node_multiple_failed_jobs():
    """Multiple failed jobs each have their logs fetched and included in the prompt."""
    bot = MagicMock()
    bot.token_for.return_value = "gh_token"

    state = CiTriageState(bot=bot, repo="owner/repo", run_id=42)

    job_a = _build_failed_job(job_id=1, name="lint", step_name="flake8", log_text="E302 expected 2 blank lines\n")
    job_b = _build_failed_job(job_id=2, name="test", step_name="pytest", log_text="FAILED test_foo\n")

    jobs_resp = _make_mock_response(json_data={"jobs": [job_a, job_b]})
    logs_resp_a = _make_mock_response(text="E302 expected 2 blank lines\n")
    logs_resp_b = _make_mock_response(text="FAILED test_foo\n")

    fake_agent = MagicMock()
    fake_agent.run = AsyncMock()

    node = FetchAndTriageNode()
    ctx = GraphRunContext(state=state, deps=None)

    with patch("cai.workflows.ci_triage.httpx.AsyncClient") as mock_client_cls:
        mock_client = MagicMock()
        mock_client.__aenter__.return_value = mock_client
        mock_client_cls.return_value = mock_client
        mock_client.get = AsyncMock(side_effect=[jobs_resp, logs_resp_a, logs_resp_b])

        with patch("cai.workflows.ci_triage._ci_triage_agent", return_value=fake_agent):
            result = asyncio.run(node.run(ctx))

    assert isinstance(result, End)
    # 3 HTTP calls: jobs + 2 log fetches
    assert mock_client.get.call_count == 3

    fake_agent.run.assert_called_once()
    prompt_arg = fake_agent.run.call_args[0][0]
    assert "Job: lint" in prompt_arg
    assert "Job: test" in prompt_arg
    assert "flake8" in prompt_arg
    assert "pytest" in prompt_arg


def test_fetch_and_triage_node_log_truncation():
    """Logs longer than 8000 characters are truncated to the last 8000 chars."""
    bot = MagicMock()
    bot.token_for.return_value = "gh_token"

    state = CiTriageState(bot=bot, repo="owner/repo", run_id=42)

    long_log = "A" * 10_000

    failed_job = _build_failed_job(job_id=1, name="test", step_name="run_tests", log_text=long_log)

    jobs_resp = _make_mock_response(json_data={"jobs": [failed_job]})
    logs_resp = _make_mock_response(text=long_log)

    fake_agent = MagicMock()
    fake_agent.run = AsyncMock()

    node = FetchAndTriageNode()
    ctx = GraphRunContext(state=state, deps=None)

    with patch("cai.workflows.ci_triage.httpx.AsyncClient") as mock_client_cls:
        mock_client = MagicMock()
        mock_client.__aenter__.return_value = mock_client
        mock_client_cls.return_value = mock_client
        mock_client.get = AsyncMock(side_effect=[jobs_resp, logs_resp])

        with patch("cai.workflows.ci_triage._ci_triage_agent", return_value=fake_agent):
            result = asyncio.run(node.run(ctx))

    assert isinstance(result, End)
    fake_agent.run.assert_called_once()
    prompt_arg = fake_agent.run.call_args[0][0]
    # Should contain the truncation notice
    assert "logs truncated" in prompt_arg
    # Should contain the last 8000 chars
    assert "A" * 8000 in prompt_arg
    # Should not contain the first 2000 chars (which were truncated)
    assert len(prompt_arg) < 10_000


def test_fetch_and_triage_node_short_log_not_truncated():
    """Logs shorter than 8000 characters are included in full."""
    bot = MagicMock()
    bot.token_for.return_value = "gh_token"

    state = CiTriageState(bot=bot, repo="owner/repo", run_id=42)

    short_log = "B" * 500

    failed_job = _build_failed_job(job_id=1, name="test", step_name="run_tests", log_text=short_log)

    jobs_resp = _make_mock_response(json_data={"jobs": [failed_job]})
    logs_resp = _make_mock_response(text=short_log)

    fake_agent = MagicMock()
    fake_agent.run = AsyncMock()

    node = FetchAndTriageNode()
    ctx = GraphRunContext(state=state, deps=None)

    with patch("cai.workflows.ci_triage.httpx.AsyncClient") as mock_client_cls:
        mock_client = MagicMock()
        mock_client.__aenter__.return_value = mock_client
        mock_client_cls.return_value = mock_client
        mock_client.get = AsyncMock(side_effect=[jobs_resp, logs_resp])

        with patch("cai.workflows.ci_triage._ci_triage_agent", return_value=fake_agent):
            result = asyncio.run(node.run(ctx))

    assert isinstance(result, End)
    fake_agent.run.assert_called_once()
    prompt_arg = fake_agent.run.call_args[0][0]
    assert "logs truncated" not in prompt_arg
    assert "B" * 500 in prompt_arg


def test_fetch_and_triage_node_uses_bot_token():
    """The node obtains a GitHub token from CaiBot and uses it for API calls."""
    bot = MagicMock()
    bot.token_for.return_value = "my_gh_token"

    state = CiTriageState(bot=bot, repo="custom/repo", run_id=99)

    jobs_resp = _make_mock_response(json_data={"jobs": []})
    fake_agent = MagicMock()
    fake_agent.run = AsyncMock()

    node = FetchAndTriageNode()
    ctx = GraphRunContext(state=state, deps=None)

    with patch("cai.workflows.ci_triage.httpx.AsyncClient") as mock_client_cls:
        mock_client = MagicMock()
        mock_client.__aenter__.return_value = mock_client
        mock_client_cls.return_value = mock_client
        mock_client.get = AsyncMock(return_value=jobs_resp)

        with patch("cai.workflows.ci_triage._ci_triage_agent", return_value=fake_agent):
            asyncio.run(node.run(ctx))

    bot.token_for.assert_called_once_with("custom/repo")
    # Check the Authorization header was set correctly
    call_kwargs = mock_client.get.call_args[1]
    assert call_kwargs["headers"]["Authorization"] == "Bearer my_gh_token"


def test_fetch_and_triage_node_constructs_correct_urls():
    """The node builds the correct GitHub API URLs for jobs and logs."""
    bot = MagicMock()
    bot.token_for.return_value = "token"
    state = CiTriageState(bot=bot, repo="org/project", run_id=77)

    failed_job = _build_failed_job(job_id=5, name="ci", step_name="build")
    jobs_resp = _make_mock_response(json_data={"jobs": [failed_job]})
    logs_resp = _make_mock_response(text="log output")

    fake_agent = MagicMock()
    fake_agent.run = AsyncMock()

    node = FetchAndTriageNode()
    ctx = GraphRunContext(state=state, deps=None)

    with patch("cai.workflows.ci_triage.httpx.AsyncClient") as mock_client_cls:
        mock_client = MagicMock()
        mock_client.__aenter__.return_value = mock_client
        mock_client_cls.return_value = mock_client
        mock_client.get = AsyncMock(side_effect=[jobs_resp, logs_resp])

        with patch("cai.workflows.ci_triage._ci_triage_agent", return_value=fake_agent):
            asyncio.run(node.run(ctx))

    # First call: jobs endpoint
    jobs_url = mock_client.get.call_args_list[0][0][0]
    assert "org/project" in jobs_url
    assert "/actions/runs/77/jobs" in jobs_url

    # Second call: logs endpoint
    logs_url = mock_client.get.call_args_list[1][0][0]
    assert "org/project" in logs_url
    assert "/actions/jobs/5/logs" in logs_url


def test_fetch_and_triage_node_follows_redirects():
    """The httpx.AsyncClient is constructed with follow_redirects=True."""
    bot = MagicMock()
    bot.token_for.return_value = "gh_token"

    state = CiTriageState(bot=bot, repo="owner/repo", run_id=42)

    jobs_resp = _make_mock_response(json_data={"jobs": []})
    fake_agent = MagicMock()
    fake_agent.run = AsyncMock()

    node = FetchAndTriageNode()
    ctx = GraphRunContext(state=state, deps=None)

    with patch("cai.workflows.ci_triage.httpx.AsyncClient") as mock_client_cls:
        mock_client = MagicMock()
        mock_client.__aenter__.return_value = mock_client
        mock_client_cls.return_value = mock_client
        mock_client.get = AsyncMock(return_value=jobs_resp)

        with patch("cai.workflows.ci_triage._ci_triage_agent", return_value=fake_agent):
            asyncio.run(node.run(ctx))

    # The constructor must be called with follow_redirects=True so that
    # the client follows 302 redirects from the GitHub Actions logs API.
    mock_client_cls.assert_called_once_with(follow_redirects=True)


# ── main() CLI ─────────────────────────────────────────────────────────


@patch("sys.argv", ["cai-ci-triage", "--repo", "owner/repo", "--run-id", "12345"])
def test_main_runs_graph():
    """main() sets up Langfuse, creates a CaiBot, and runs the graph."""
    with patch("cai.workflows.ci_triage.setup_langfuse") as mock_setup:
        with patch("cai.workflows.ci_triage.CaiBot") as mock_bot_cls:
            bot_instance = MagicMock()
            mock_bot_cls.return_value = bot_instance

            with patch("cai.workflows.ci_triage.langfuse_workflow") as mock_lf:
                with patch("cai.workflows.ci_triage.ci_triage_graph") as mock_graph:
                    mock_graph.run = AsyncMock()

                    main()

    mock_setup.assert_called_once()
    mock_bot_cls.assert_called_once()
    mock_lf.assert_called_once()
    mock_graph.run.assert_called_once()
    # Verify the graph is started at FetchAndTriageNode
    call_args = mock_graph.run.call_args
    assert isinstance(call_args[0][0], FetchAndTriageNode)


@patch("sys.argv", ["cai-ci-triage", "--repo", "owner/repo", "--run-id", "12345"])
def test_main_session_id_format():
    """main() creates a session id matching ci-triage-YYYYMMDD-HHMMSS."""
    with patch("cai.workflows.ci_triage.setup_langfuse"):
        with patch("cai.workflows.ci_triage.CaiBot"):
            with patch("cai.workflows.ci_triage.langfuse_workflow") as mock_lf:
                with patch("cai.workflows.ci_triage.ci_triage_graph") as mock_graph:
                    mock_graph.run = AsyncMock()
                    main()

    call_kwargs = mock_lf.call_args[1]
    session_id = call_kwargs["session_id"]
    assert re.match(r"^ci-triage-\d{8}-\d{6}$", session_id), f"unexpected format: {session_id!r}"


@patch("sys.argv", ["cai-ci-triage", "--repo", "owner/repo", "--run-id", "12345"])
def test_main_metadata_includes_repo_and_run_id():
    """main() passes repo and run_id in the langfuse_workflow metadata."""
    with patch("cai.workflows.ci_triage.setup_langfuse"):
        with patch("cai.workflows.ci_triage.CaiBot"):
            with patch("cai.workflows.ci_triage.langfuse_workflow") as mock_lf:
                with patch("cai.workflows.ci_triage.ci_triage_graph") as mock_graph:
                    mock_graph.run = AsyncMock()
                    main()

    call_kwargs = mock_lf.call_args
    assert call_kwargs[1]["metadata"] == {"repo": "owner/repo", "run_id": 12345}


@patch("sys.argv", ["cai-ci-triage", "--repo", "owner/repo", "--run-id", "12345"])
def test_main_passes_state_to_graph():
    """main() constructs CiTriageState with the correct fields."""
    with patch("cai.workflows.ci_triage.setup_langfuse"):
        with patch("cai.workflows.ci_triage.CaiBot") as mock_bot_cls:
            bot_instance = MagicMock()
            mock_bot_cls.return_value = bot_instance

            with patch("cai.workflows.ci_triage.langfuse_workflow"):
                with patch("cai.workflows.ci_triage.ci_triage_graph") as mock_graph:
                    mock_graph.run = AsyncMock()
                    main()

    # The state is passed as the second positional arg
    state_arg = mock_graph.run.call_args[1]["state"]
    assert isinstance(state_arg, CiTriageState)
    assert state_arg.repo == "owner/repo"
    assert state_arg.run_id == 12345
    assert state_arg.bot is bot_instance
