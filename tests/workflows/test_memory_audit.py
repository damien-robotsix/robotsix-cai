"""Tests for ``cai.workflows.memory_audit`` — the ``cai-memory-audit`` CLI.

Covers the ``MemoryAuditOutput`` model, ``MemoryAuditState`` dataclass,
graph structure, ``MemoryAuditNode`` with mocked agent, and ``main()`` CLI
argument parsing.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from pydantic import ValidationError
from pydantic_graph import End, Graph

from cai.workflows.memory_audit import (
    MemoryAuditNode,
    MemoryAuditOutput,
    MemoryAuditState,
    _memory_audit_agent,
    main,
    memory_audit_graph,
)


@pytest.fixture(autouse=True)
def _reset_agent_cache():
    """_memory_audit_agent is lru_cached so a fresh patch lands per test."""
    _memory_audit_agent.cache_clear()
    yield
    _memory_audit_agent.cache_clear()


def _run(node: MemoryAuditNode, state: MemoryAuditState) -> End[MemoryAuditOutput]:
    ctx = MagicMock()
    ctx.state = state
    return asyncio.run(node.run(ctx))


# ── MemoryAuditOutput model ─────────────────────────────────────────────


def test_output_defaults():
    """entries_marked_* and entries_unchanged default to empty lists."""
    output = MemoryAuditOutput(entries_checked=0, summary="Noop.")
    assert output.entries_checked == 0
    assert output.entries_marked_stale == []
    assert output.entries_marked_superseded == []
    assert output.entries_unchanged == []
    assert output.summary == "Noop."


def test_output_all_fields():
    """Full construction with every field populated."""
    output = MemoryAuditOutput(
        entries_checked=5,
        entries_marked_stale=["a.md", "b.md"],
        entries_marked_superseded=["c.md"],
        entries_unchanged=["d.md", "e.md"],
        summary="Two stale, one superseded.",
    )
    assert output.entries_checked == 5
    assert output.entries_marked_stale == ["a.md", "b.md"]
    assert output.entries_marked_superseded == ["c.md"]
    assert output.entries_unchanged == ["d.md", "e.md"]
    assert output.summary == "Two stale, one superseded."


def test_output_entries_checked_required():
    """entries_checked is a required field."""
    with pytest.raises(ValidationError):
        MemoryAuditOutput(summary="Missing entries_checked.")  # type: ignore[call-arg]


def test_output_summary_required():
    """summary is a required field."""
    with pytest.raises(ValidationError):
        MemoryAuditOutput(entries_checked=1)  # type: ignore[call-arg]


def test_output_serialization():
    """MemoryAuditOutput round-trips through model_dump / model_validate."""
    output = MemoryAuditOutput(
        entries_checked=3,
        entries_marked_stale=["x.md"],
        entries_marked_superseded=["y.md"],
        entries_unchanged=["z.md"],
        summary="Done.",
    )
    d = output.model_dump()
    assert d == {
        "entries_checked": 3,
        "entries_marked_stale": ["x.md"],
        "entries_marked_superseded": ["y.md"],
        "entries_unchanged": ["z.md"],
        "summary": "Done.",
    }
    rehydrated = MemoryAuditOutput.model_validate(d)
    assert rehydrated == output


# ── MemoryAuditState dataclass ──────────────────────────────────────────


def test_state_repo_root_required():
    """repo_root is a required field."""
    state = MemoryAuditState(repo_root=Path("/tmp/repo"))
    assert state.repo_root == Path("/tmp/repo")
    assert state.output is None


def test_state_output_defaults_none():
    """output is None by default."""
    state = MemoryAuditState(repo_root=Path("/tmp/repo"))
    assert state.output is None


def test_state_output_can_be_set():
    """output can be set after construction."""
    state = MemoryAuditState(repo_root=Path("/tmp/repo"))
    output = MemoryAuditOutput(entries_checked=1, summary="ok")
    state.output = output
    assert state.output is output


# ── MemoryAuditNode ─────────────────────────────────────────────────────


def test_node_sets_output_on_state():
    """The node stores the agent result in ctx.state.output."""
    output = MemoryAuditOutput(entries_checked=2, summary="All good.")
    agent_mock = MagicMock()
    agent_mock.run = MagicMock()

    async def fake_run(*args, **kwargs):
        result = MagicMock()
        result.output = output
        return result

    agent_mock.run.side_effect = fake_run

    state = MemoryAuditState(repo_root=Path("/tmp/repo"))

    with patch(
        "cai.workflows.memory_audit._memory_audit_agent", return_value=agent_mock
    ) as mock_factory:
        result = _run(MemoryAuditNode(), state)

    assert isinstance(result, End)
    assert result.data is output
    assert state.output is output
    mock_factory.assert_called_once()


def test_node_passes_repo_root_to_repo_deps():
    """The node calls repo_deps with the state's repo_root and the correct write_globs."""
    output = MemoryAuditOutput(entries_checked=0, summary="Nothing to audit.")
    agent_mock = MagicMock()

    async def fake_run(*args, **kwargs):
        result = MagicMock()
        result.output = output
        return result

    agent_mock.run.side_effect = fake_run

    state = MemoryAuditState(repo_root=Path("/some/repo"))

    with patch(
        "cai.workflows.memory_audit._memory_audit_agent", return_value=agent_mock
    ):
        with patch("cai.workflows.memory_audit.repo_deps") as mock_repo_deps:
            mock_repo_deps.return_value = MagicMock()
            _run(MemoryAuditNode(), state)

    mock_repo_deps.assert_called_once_with(
        Path("/some/repo"),
        write_globs=[".cai/memory/**"],
    )


def test_node_prompt_includes_repo_root():
    """The prompt string passed to the agent includes the repo root path."""
    output = MemoryAuditOutput(entries_checked=0, summary="ok")
    agent_mock = MagicMock()

    async def fake_run(prompt, **kwargs):
        result = MagicMock()
        result.output = output
        result._prompt = prompt
        return result

    agent_mock.run.side_effect = fake_run

    state = MemoryAuditState(repo_root=Path("/home/user/project"))

    with patch(
        "cai.workflows.memory_audit._memory_audit_agent", return_value=agent_mock
    ):
        with patch("cai.workflows.memory_audit.repo_deps", return_value=MagicMock()):
            _run(MemoryAuditNode(), state)

    call_args = agent_mock.run.call_args
    prompt = call_args[0][0]
    assert ".cai/memory/" in prompt
    assert "/home/user/project" in prompt
    assert "Audit all entries" in prompt


# ── Graph structure ─────────────────────────────────────────────────────


def test_graph_is_pydantic_graph():
    """memory_audit_graph is a pydantic_graph.Graph."""
    assert isinstance(memory_audit_graph, Graph)


def test_graph_contains_memory_audit_node():
    """The graph has exactly one node: MemoryAuditNode."""
    nodes = memory_audit_graph.get_nodes()
    assert len(nodes) == 1
    assert nodes[0] is MemoryAuditNode


# ── main() CLI ──────────────────────────────────────────────────────────


@patch("sys.argv", ["cai-memory-audit"])
def test_main_defaults_to_cwd(tmp_path, monkeypatch):
    """When --repo-root is not given, the current working directory is used."""
    monkeypatch.chdir(tmp_path)
    output = MemoryAuditOutput(entries_checked=0, summary="ok")

    agent_mock = MagicMock()

    async def fake_run(*args, **kwargs):
        result = MagicMock()
        result.output = output
        return result

    agent_mock.run.side_effect = fake_run

    with patch(
        "cai.workflows.memory_audit._memory_audit_agent", return_value=agent_mock
    ):
        with patch("cai.workflows.memory_audit.repo_deps", return_value=MagicMock()):
            main()

    call_args = agent_mock.run.call_args
    prompt = call_args[0][0]
    assert str(tmp_path.resolve()) in prompt


@patch("sys.argv", ["cai-memory-audit", "--repo-root", "/custom/repo/path"])
def test_main_custom_repo_root():
    """--repo-root is resolved and passed through to the state."""
    output = MemoryAuditOutput(entries_checked=1, summary="done")
    agent_mock = MagicMock()

    async def fake_run(*args, **kwargs):
        result = MagicMock()
        result.output = output
        return result

    agent_mock.run.side_effect = fake_run

    with patch(
        "cai.workflows.memory_audit._memory_audit_agent", return_value=agent_mock
    ):
        with patch("cai.workflows.memory_audit.repo_deps", return_value=MagicMock()):
            main()

    call_args = agent_mock.run.call_args
    prompt = call_args[0][0]
    assert "/custom/repo/path" in prompt


@patch("sys.argv", ["cai-memory-audit"])
def test_main_prints_output(capsys, tmp_path, monkeypatch):
    """main() prints the summary and per-category counts to stdout."""
    monkeypatch.chdir(tmp_path)
    output = MemoryAuditOutput(
        entries_checked=4,
        entries_marked_stale=["one.md", "two.md"],
        entries_marked_superseded=["three.md"],
        entries_unchanged=["four.md"],
        summary="Two stale, one superseded, one unchanged.",
    )

    agent_mock = MagicMock()

    async def fake_run(*args, **kwargs):
        result = MagicMock()
        result.output = output
        return result

    agent_mock.run.side_effect = fake_run

    with patch(
        "cai.workflows.memory_audit._memory_audit_agent", return_value=agent_mock
    ):
        with patch("cai.workflows.memory_audit.repo_deps", return_value=MagicMock()):
            main()

    captured = capsys.readouterr()
    assert "Entries checked: 4" in captured.out
    assert "Marked stale: 2" in captured.out
    assert "  - one.md" in captured.out
    assert "  - two.md" in captured.out
    assert "Marked superseded: 1" in captured.out
    assert "  - three.md" in captured.out
    assert "Unchanged: 1" in captured.out
    assert "Two stale, one superseded, one unchanged." in captured.out


@patch("sys.argv", ["cai-memory-audit"])
def test_main_no_output_when_state_output_none(capsys, tmp_path, monkeypatch):
    """When state.output stays None (e.g. graph run fails silently), nothing is printed."""
    monkeypatch.chdir(tmp_path)

    with patch.object(
        memory_audit_graph, "run", side_effect=RuntimeError("boom")
    ):
        with pytest.raises(RuntimeError, match="boom"):
            main()

    captured = capsys.readouterr()
    assert captured.out == ""


@patch("sys.argv", ["cai-memory-audit"])
def test_main_agent_not_called_when_no_entries(tmp_path, monkeypatch):
    """The agent is still invoked even if .cai/memory/ doesn't exist — the
    agent handles the no-op case itself."""
    monkeypatch.chdir(tmp_path)
    output = MemoryAuditOutput(entries_checked=0, summary="No .cai/memory/ directory found.")

    agent_mock = MagicMock()

    async def fake_run(*args, **kwargs):
        result = MagicMock()
        result.output = output
        return result

    agent_mock.run.side_effect = fake_run

    with patch(
        "cai.workflows.memory_audit._memory_audit_agent", return_value=agent_mock
    ):
        with patch("cai.workflows.memory_audit.repo_deps", return_value=MagicMock()):
            main()

    agent_mock.run.assert_called_once()
