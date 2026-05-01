from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

from cai.workflows.state import ExploreOutput, ImplementOutput
from cai.workflows.test_runner import TestNode


def _run(node, state):
    ctx = MagicMock()
    ctx.state = state
    return asyncio.run(node.run(ctx))


@patch("cai.workflows.test_runner._test_writer_agent")
def test_test_node_prompt_includes_findings_when_present(
    mock_agent, state,
):
    """When state.findings is set, the TestNode prompt includes the
    explore agent's findings."""
    state.findings = ExploreOutput(
        summary="Service layer uses async patterns. Key file: src/services/order.py.",
        related_files=["src/services/order.py"],
    )
    state.implement_output = ImplementOutput(
        summary="Added order service.",
        commit_message="feat: add order service",
    )

    mock_agent_instance = MagicMock()
    mock_agent.return_value = mock_agent_instance

    captured_prompt = None

    async def mock_run(prompt, *args, **kwargs):
        nonlocal captured_prompt
        captured_prompt = prompt

        class MockResult:
            output = MagicMock()

        return MockResult()

    mock_agent_instance.run.side_effect = mock_run

    _run(TestNode(), state)

    assert captured_prompt is not None
    assert "## Codebase findings (explore agent)" in captured_prompt
    assert "Service layer uses async patterns. Key file: src/services/order.py." in captured_prompt


@patch("cai.workflows.test_runner._test_writer_agent")
def test_test_node_prompt_omits_findings_when_none(
    mock_agent, state,
):
    """When state.findings is None, the TestNode prompt does NOT include
    a findings section."""
    state.findings = None
    state.implement_output = ImplementOutput(
        summary="Added order service.",
        commit_message="feat: add order service",
    )

    mock_agent_instance = MagicMock()
    mock_agent.return_value = mock_agent_instance

    captured_prompt = None

    async def mock_run(prompt, *args, **kwargs):
        nonlocal captured_prompt
        captured_prompt = prompt

        class MockResult:
            output = MagicMock()

        return MockResult()

    mock_agent_instance.run.side_effect = mock_run

    _run(TestNode(), state)

    assert captured_prompt is not None
    assert "## Codebase findings (explore agent)" not in captured_prompt


@patch("cai.workflows.test_runner._test_writer_agent")
def test_test_node_prompt_includes_reference_files(
    mock_agent, state, tmp_path,
):
    """When reference_files_section() returns content, the TestNode prompt
    includes the ## Reference files section."""
    state.implement_output = ImplementOutput(
        summary="Added order service.",
        commit_message="feat: add order service",
    )

    # Create a real reference file so reference_files_section() returns content
    ref_file = tmp_path / "src" / "services" / "order.py"
    ref_file.parent.mkdir(parents=True, exist_ok=True)
    ref_file.write_text("def create_order():\n    pass\n")
    state.reference_files = ["src/services/order.py"]

    mock_agent_instance = MagicMock()
    mock_agent.return_value = mock_agent_instance

    captured_prompt = None

    async def mock_run(prompt, *args, **kwargs):
        nonlocal captured_prompt
        captured_prompt = prompt

        class MockResult:
            output = MagicMock()

        return MockResult()

    mock_agent_instance.run.side_effect = mock_run

    _run(TestNode(), state)

    assert captured_prompt is not None
    assert "## Reference files" in captured_prompt
    assert "### src/services/order.py" in captured_prompt


@patch("cai.workflows.test_runner._test_writer_agent")
def test_test_node_prompt_omits_reference_files_when_empty(
    mock_agent, state,
):
    """When reference_files is empty, the TestNode prompt does NOT include
    a reference files section."""
    state.implement_output = ImplementOutput(
        summary="Added order service.",
        commit_message="feat: add order service",
    )
    state.reference_files = []

    mock_agent_instance = MagicMock()
    mock_agent.return_value = mock_agent_instance

    captured_prompt = None

    async def mock_run(prompt, *args, **kwargs):
        nonlocal captured_prompt
        captured_prompt = prompt

        class MockResult:
            output = MagicMock()

        return MockResult()

    mock_agent_instance.run.side_effect = mock_run

    _run(TestNode(), state)

    assert captured_prompt is not None
    assert "## Reference files" not in captured_prompt


@patch("cai.workflows.test_runner._test_writer_agent")
def test_findings_and_reference_files_order(
    mock_agent, state, tmp_path,
):
    """When both findings and reference files are present, the findings
    section appears before the reference files section."""
    state.findings = ExploreOutput(
        summary="Key finding.",
        related_files=[],
    )
    state.implement_output = ImplementOutput(
        summary="Added order service.",
        commit_message="feat: add order service",
    )

    # Create a real reference file so reference_files_section() returns content
    ref_file = tmp_path / "src" / "example.py"
    ref_file.parent.mkdir(parents=True, exist_ok=True)
    ref_file.write_text("def foo():\n    return 42\n")
    state.reference_files = ["src/example.py"]

    mock_agent_instance = MagicMock()
    mock_agent.return_value = mock_agent_instance

    captured_prompt = None

    async def mock_run(prompt, *args, **kwargs):
        nonlocal captured_prompt
        captured_prompt = prompt

        class MockResult:
            output = MagicMock()

        return MockResult()

    mock_agent_instance.run.side_effect = mock_run

    _run(TestNode(), state)

    assert captured_prompt is not None
    summary_idx = captured_prompt.index("## Implementation summary")
    findings_idx = captured_prompt.index("## Codebase findings (explore agent)")
    ref_idx = captured_prompt.index("## Reference files")

    assert summary_idx < findings_idx < ref_idx, (
        "Findings must appear after implementation summary and before reference files"
    )
