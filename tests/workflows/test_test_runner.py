from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

from pydantic_ai.usage import UsageLimits

from cai.workflows.state import ExploreOutput, ImplementOutput
from cai.workflows.test_runner import TestNode


def _run(node, state):
    ctx = MagicMock()
    ctx.state = state
    return asyncio.run(node.run(ctx))


# ---------------------------------------------------------------------------
# TestNode
# ---------------------------------------------------------------------------


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


@patch("cai.workflows.test_runner._run_tests", return_value=(True, ""))
@patch("cai.workflows.test_runner._test_writer_agent")
def test_test_node_request_limit(
    mock_agent, mock_run_tests, state,
):
    """TestNode passes UsageLimits with request_limit=20 to the test_writer agent."""
    state.implement_output = ImplementOutput(
        summary="s", commit_message="c", required_checks=[], replies=[]
    )

    mock_agent_instance = MagicMock()
    mock_agent.return_value = mock_agent_instance

    async def mock_run(prompt, *args, **kwargs):
        class MockResult:
            output = MagicMock()
        return MockResult()

    mock_agent_instance.run.side_effect = mock_run

    _run(TestNode(), state)

    mock_agent_instance.run.assert_called_once()
    _, kwargs = mock_agent_instance.run.call_args
    assert "usage_limits" in kwargs
    assert isinstance(kwargs["usage_limits"], UsageLimits)
    assert kwargs["usage_limits"].request_limit == 20
    )

    mock_agent_instance = MagicMock()
    mock_agent.return_value = mock_agent_instance

# ---------------------------------------------------------------------------
# TestNode
# ---------------------------------------------------------------------------


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


@patch("cai.workflows.test_runner._run_tests", return_value=(True, ""))
@patch("cai.workflows.test_runner._test_writer_agent")
def test_test_node_request_limit(
    mock_agent, mock_run_tests, state,
):
    """TestNode passes UsageLimits with request_limit=20 to the test_writer agent."""
    state.implement_output = ImplementOutput(
        summary="s", commit_message="c", required_checks=[], replies=[]
    )
        return MockResult()

    mock_agent_instance.run.side_effect = mock_run

    _run(TestNode(), state)

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

    mock_agent_instance = MagicMock()
    mock_agent.return_value = mock_agent_instance

    captured_prompt = None

    async def mock_run(prompt, *args, **kwargs):
        nonlocal captured_prompt
        captured_prompt = prompt
    async def mock_run(prompt, *args, **kwargs):
        class MockResult:
            output = MagicMock()
        return MockResult()

    mock_agent_instance.run.side_effect = mock_run

    _run(TestNode(), state)

    assert captured_prompt is not None
    assert "## Reference files" in captured_prompt
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


@patch("cai.workflows.test_runner._run_tests", return_value=(True, ""))
@patch("cai.workflows.test_runner._test_writer_agent")
def test_test_node_prompt_includes_reference_files_when_present(
    mock_agent, mock_run_tests, state, tmp_path,
):
    """The prompt includes the reference_files_section() when reference files exist."""
    state.implement_output = ImplementOutput(
        summary="s", commit_message="c", required_checks=[], replies=[]
    )

    # Create a real reference file so reference_files_section() returns content
    ref_file = tmp_path / "refs" / "config.py"
    ref_file.parent.mkdir(parents=True, exist_ok=True)
    ref_file.write_text("CONFIG = {'key': 'value'}\n")
    state.reference_files = ["refs/config.py"]
    )
    state.reference_files = []

    mock_agent_instance = MagicMock()
    mock_agent.return_value = mock_agent_instance

    captured_prompt = None

    async def mock_run(prompt, *args, **kwargs):
        nonlocal captured_prompt
        captured_prompt = prompt
    mock_agent_instance.run.assert_called_once()
    _, kwargs = mock_agent_instance.run.call_args
    assert "usage_limits" in kwargs
    assert isinstance(kwargs["usage_limits"], UsageLimits)
    assert kwargs["usage_limits"].request_limit == 20


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


@patch("cai.workflows.test_runner._run_tests", return_value=(True, ""))
@patch("cai.workflows.test_runner._test_writer_agent")
def test_test_node_prompt_includes_reference_files_when_present(
    mock_agent, mock_run_tests, state, tmp_path,
):
    """The prompt includes the reference_files_section() when reference files exist."""
    state.implement_output = ImplementOutput(
        summary="s", commit_message="c", required_checks=[], replies=[]
    )

    # Create a real reference file so reference_files_section() returns content
    ref_file = tmp_path / "refs" / "config.py"
    ref_file.parent.mkdir(parents=True, exist_ok=True)
    ref_file.write_text("CONFIG = {'key': 'value'}\n")
    state.reference_files = ["refs/config.py"]
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
        summary="Findings summary.",
        related_files=[],
    )
    state.implement_output = ImplementOutput(
        summary="Added order service.",
        commit_message="feat: add order service",
    )

    # Create a reference file
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

    mock_agent_instance = MagicMock()
    mock_agent.return_value = mock_agent_instance

    captured_prompt = None

    async def mock_run(prompt, *args, **kwargs):
        nonlocal captured_prompt
        captured_prompt = prompt
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
    assert "refs/config.py" in captured_prompt
    assert "CONFIG = {'key': 'value'}" in captured_prompt


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
        return MockResult()

    mock_agent_instance.run.side_effect = mock_run

    _run(TestNode(), state)

    assert captured_prompt is not None
@patch("cai.workflows.test_runner._test_writer_agent")
def test_findings_and_reference_files_order(
    mock_agent, state, tmp_path,
):
    """When both findings and reference files are present, the findings
    section appears before the reference files section."""
    state.findings = ExploreOutput(
        summary="Findings summary.",
        related_files=[],
    )
    state.implement_output = ImplementOutput(
        summary="Added order service.",
        commit_message="feat: add order service",
    )

    # Create a reference file
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
    summary_idx = captured_prompt.index("## Implementation summary")
    findings_idx = captured_prompt.index("## Codebase findings (explore agent)")
    ref_idx = captured_prompt.index("## Reference files")

    assert summary_idx < findings_idx < ref_idx, (
        "Findings must appear after implementation summary and before reference files"
    )


@patch("cai.workflows.test_runner._run_tests", return_value=(True, ""))
@patch("cai.workflows.test_runner._test_writer_agent")
def test_test_node_transitions_to_python_review_when_python_check(
    mock_agent, mock_run_tests, state,
):
    """When required_checks includes 'python', TestNode transitions to PythonReviewNode."""
    state.implement_output = ImplementOutput(
        summary="s", commit_message="c", required_checks=["python"], replies=[]
    )

    mock_agent_instance = MagicMock()
    mock_agent.return_value = mock_agent_instance

    async def mock_run(prompt, *args, **kwargs):
        class MockResult:
            output = MagicMock()
        return MockResult()

    mock_agent_instance.run.side_effect = mock_run

    from cai.workflows.python_review import PythonReviewNode
    result = _run(TestNode(), state)
    assert isinstance(result, PythonReviewNode)


@patch("cai.workflows.test_runner._run_tests", return_value=(True, ""))
@patch("cai.workflows.test_runner._test_writer_agent")
def test_test_node_transitions_to_docs_when_documentation_check(
    mock_agent, mock_run_tests, state,
):
    """When required_checks includes 'documentation', TestNode transitions to DocsNode."""
    state.implement_output = ImplementOutput(
        summary="s", commit_message="c", required_checks=["documentation"], replies=[]
    )

    mock_agent_instance = MagicMock()
    mock_agent.return_value = mock_agent_instance

    async def mock_run(prompt, *args, **kwargs):
        class MockResult:
            output = MagicMock()
        return MockResult()

    mock_agent_instance.run.side_effect = mock_run

    from cai.workflows.docs import DocsNode
    result = _run(TestNode(), state)
    assert isinstance(result, DocsNode)


@patch("cai.workflows.test_runner._run_tests", return_value=(True, ""))
@patch("cai.workflows.test_runner._test_writer_agent")
def test_test_node_transitions_to_pr_when_no_checks(
    mock_agent, mock_run_tests, state,
):
    """When required_checks is empty, TestNode transitions to PRNode."""
    state.implement_output = ImplementOutput(
        summary="s", commit_message="c", required_checks=[], replies=[]
    )

    mock_agent_instance = MagicMock()
    mock_agent.return_value = mock_agent_instance

    async def mock_run(prompt, *args, **kwargs):
        class MockResult:
            output = MagicMock()
        return MockResult()

    mock_agent_instance.run.side_effect = mock_run

    from cai.workflows.pr import PRNode
    result = _run(TestNode(), state)
    assert isinstance(result, PRNode)


@patch("cai.workflows.test_runner._run_tests", return_value=(False, "FAILURE"))
@patch("cai.workflows.test_runner._test_writer_agent")
def test_test_node_retries_via_implement_on_failure(
    mock_agent, mock_run_tests, state,
):
    """When tests fail and retry_count < 1, TestNode returns ImplementNode and increments retry count."""
    state.implement_output = ImplementOutput(
        summary="s", commit_message="c", required_checks=[], replies=[]
    )
    state.test_retry_count = 0

    mock_agent_instance = MagicMock()
    mock_agent.return_value = mock_agent_instance

    async def mock_run(prompt, *args, **kwargs):
        class MockResult:
            output = MagicMock()
        return MockResult()

    mock_agent_instance.run.side_effect = mock_run

    from cai.workflows.implement import ImplementNode
    result = _run(TestNode(), state)

    assert isinstance(result, ImplementNode)
    assert state.test_retry_count == 1
    assert state.tests_passed is False
    assert state.test_failure_details == "FAILURE"


@patch("cai.workflows.test_runner._run_tests", return_value=(True, ""))
@patch("cai.workflows.test_runner._test_writer_agent")
def test_test_node_sets_test_output(
    mock_agent, mock_run_tests, state,
):
    """TestNode stores the agent result in state.test_output."""
    state.implement_output = ImplementOutput(
        summary="s", commit_message="c", required_checks=[], replies=[]
    )

    mock_agent_instance = MagicMock()
    mock_agent.return_value = mock_agent_instance

    class FakeOutput:
        summary = "Wrote 3 tests"
        commit_message = "Add tests for foo"

    async def mock_run(prompt, *args, **kwargs):
        class MockResult:
            output = FakeOutput()
        return MockResult()

    mock_agent_instance.run.side_effect = mock_run

    _run(TestNode(), state)

    assert state.test_output is not None
    assert state.test_output.summary == "Wrote 3 tests"
    assert state.test_output.commit_message == "Add tests for foo"
