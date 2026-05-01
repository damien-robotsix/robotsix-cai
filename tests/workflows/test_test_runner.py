from __future__ import annotations

import asyncio
import subprocess
import sys
from unittest.mock import MagicMock, patch

import pytest
from pydantic_ai.usage import UsageLimits

from cai.workflows.state import ExploreOutput, ImplementOutput
from cai.workflows.test_runner import TestNode, TestSanityNode, _run_tests


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
    """TestNode passes UsageLimits with request_limit=50 to the test_writer agent."""
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
    assert kwargs["usage_limits"].request_limit == 50


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


@patch("cai.workflows.test_runner._test_writer_agent")
def test_test_node_prompt_does_not_duplicate_reference_files(
    mock_agent, state, tmp_path,
):
    """The reference files section appears at most once in the prompt (the fix
    removed a duplicate inline f-string call to reference_files_section())."""
    state.implement_output = ImplementOutput(
        summary="Added order service.",
        commit_message="feat: add order service",
    )

    ref_file = tmp_path / "src" / "services" / "order.py"
    ref_file.parent.mkdir(parents=True, exist_ok=True)
    ref_file.write_text("def create_order():\n    pass\n")
    state.reference_files = ["src/services/order.py"]
    state.findings = ExploreOutput(
        summary="Findings summary.",
        related_files=[],
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
    # Count occurrences — there should be exactly one "## Reference files" heading
    count = captured_prompt.count("## Reference files")
    assert count == 1, (
        f"Expected exactly one '## Reference files' heading, found {count}. "
        "A duplicate inline call was removed in the fix."
    )


@patch("cai.workflows.test_runner._run_tests", return_value=(True, ""))
@patch("cai.workflows.test_runner._test_writer_agent")
def test_test_node_clears_failure_details_when_tests_pass(
    mock_agent, mock_run_tests, state,
):
    """TestNode clears test_failure_details when tests pass (stale value from prior retry)."""
    state.implement_output = ImplementOutput(
        summary="s", commit_message="c", required_checks=[], replies=[],
    )
    state.test_failure_details = "old failure"

    mock_agent_instance = MagicMock()
    mock_agent.return_value = mock_agent_instance

    async def mock_run(prompt, *args, **kwargs):
        class MockResult:
            output = MagicMock()
        return MockResult()

    mock_agent_instance.run.side_effect = mock_run

    _run(TestNode(), state)

    assert state.tests_passed is True
    assert state.test_failure_details == ""


# ---------------------------------------------------------------------------
# _run_tests
# ---------------------------------------------------------------------------


@patch("cai.workflows.test_runner.subprocess.run")
def test_run_tests_compile_failure(mock_subprocess_run, tmp_path):
    """When compileall fails, _run_tests returns (False, failure message)."""
    mock_subprocess_run.return_value = subprocess.CompletedProcess(
        args=[], returncode=1, stdout="error", stderr="",
    )

    passed, details = _run_tests(tmp_path)

    assert passed is False
    assert details.startswith("Compile check failed:")
    assert "error" in details

    # Should only have called compileall, not pytest
    mock_subprocess_run.assert_called_once()
    args, kwargs = mock_subprocess_run.call_args
    assert args[0] == [sys.executable, "-m", "compileall", "-q", "src"]
    assert kwargs["cwd"] == str(tmp_path)
    assert kwargs["capture_output"] is True
    assert kwargs["text"] is True
    assert "env" in kwargs


@patch("cai.workflows.test_runner.subprocess.run")
def test_run_tests_no_tests_dir(mock_subprocess_run, tmp_path):
    """When tests/ does not exist, _run_tests returns (True, '')."""
    mock_subprocess_run.return_value = subprocess.CompletedProcess(
        args=[], returncode=0, stdout="", stderr="",
    )

    passed, details = _run_tests(tmp_path)

    assert passed is True
    assert details == ""


@patch("cai.workflows.test_runner.subprocess.run")
def test_run_tests_pytest_passes(mock_subprocess_run, tmp_path):
    """When compile passes and pytest returns 0, _run_tests returns (True, '')."""
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()

    # First call = compileall, second = pytest
    mock_subprocess_run.side_effect = [
        subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr=""),
        subprocess.CompletedProcess(args=[], returncode=0, stdout="... OK ...", stderr=""),
    ]

    passed, details = _run_tests(tmp_path)

    assert passed is True
    assert details == ""
    assert mock_subprocess_run.call_count == 2


@patch("cai.workflows.test_runner.subprocess.run")
def test_run_tests_pytest_fails(mock_subprocess_run, tmp_path):
    """When pytest returns non-zero, _run_tests returns (False, details)."""
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()

    mock_subprocess_run.side_effect = [
        subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr=""),
        subprocess.CompletedProcess(args=[], returncode=1, stdout="FAIL", stderr="traceback"),
    ]

    passed, details = _run_tests(tmp_path)

    assert passed is False
    assert "FAIL" in details
    assert "traceback" in details


@patch("cai.workflows.test_runner.subprocess.run")
def test_run_tests_no_collected(mock_subprocess_run, tmp_path):
    """When pytest returns 5 (no tests collected), _run_tests treats it as success."""
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()

    mock_subprocess_run.side_effect = [
        subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr=""),
        subprocess.CompletedProcess(args=[], returncode=5, stdout="no tests collected", stderr=""),
    ]

    passed, details = _run_tests(tmp_path)

    assert passed is True
    assert details == ""


@patch("cai.workflows.test_runner.subprocess.run")
def test_run_tests_timeout(mock_subprocess_run, tmp_path):
    """When pytest times out, _run_tests returns (False, timeout message)."""
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()

    mock_subprocess_run.side_effect = [
        subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr=""),
        subprocess.TimeoutExpired(cmd="pytest", timeout=300),
    ]

    passed, details = _run_tests(tmp_path)

    assert passed is False
    assert "Tests timed out after 300s." in details


@patch.dict("os.environ", {"OPENROUTER_API_KEY": "sk-123", "PATH": "/usr/bin", "HOME": "/home/user"})
@patch("cai.workflows.test_runner.subprocess.run")
def test_run_tests_strips_api_keys_from_env(mock_subprocess_run, tmp_path):
    """API keys like OPENROUTER_API_KEY are stripped from the subprocess env."""
    mock_subprocess_run.return_value = subprocess.CompletedProcess(
        args=[], returncode=0, stdout="", stderr="",
    )

    _run_tests(tmp_path)

    # Check the env passed to subprocess
    env = mock_subprocess_run.call_args[1]["env"]
    assert "OPENROUTER_API_KEY" not in env
    assert "ANTHROPIC_API_KEY" not in env
    assert "OPENAI_API_KEY" not in env
    assert "LANGFUSE_PUBLIC_KEY" not in env
    assert "LANGFUSE_SECRET_KEY" not in env
    # Non-key vars should still be present
    assert env["PATH"] == "/usr/bin"
    assert env["HOME"] == "/home/user"


# ---------------------------------------------------------------------------
# TestSanityNode
# ---------------------------------------------------------------------------


@patch("cai.workflows.test_runner._run_tests", return_value=(False, "FAIL"))
def test_sanity_transitions_to_implement_on_failure_below_max(
    mock_run_tests, state,
):
    """When tests fail and retry_count < 2, TestSanityNode transitions to
    ImplementNode and increments retry_count."""
    state.implement_output = ImplementOutput(
        summary="s", commit_message="c", required_checks=[], replies=[],
    )
    state.test_retry_count = 0

    from cai.workflows.implement import ImplementNode
    result = _run(TestSanityNode(), state)

    assert isinstance(result, ImplementNode)
    assert state.test_retry_count == 1
    assert state.tests_passed is False
    assert state.test_failure_details == "FAIL"


@patch("cai.workflows.test_runner._run_tests", return_value=(False, "FAIL"))
def test_sanity_does_not_retry_when_at_max_retries(
    mock_run_tests, state,
):
    """When tests fail but retry_count >= 2, TestSanityNode does NOT retry
    via ImplementNode — it moves on to DocsNode/PRNode."""
    state.implement_output = ImplementOutput(
        summary="s", commit_message="c", required_checks=[], replies=[],
    )
    state.test_retry_count = 2

    from cai.workflows.pr import PRNode
    result = _run(TestSanityNode(), state)

    assert isinstance(result, PRNode)
    # retry_count should NOT have been incremented
    assert state.test_retry_count == 2
    assert state.tests_passed is False
    assert state.test_failure_details == "FAIL"


@patch("cai.workflows.test_runner._run_tests", return_value=(True, ""))
def test_sanity_transitions_to_docs_when_documentation_check(
    mock_run_tests, state,
):
    """When tests pass and required_checks includes 'documentation',
    TestSanityNode transitions to DocsNode."""
    state.implement_output = ImplementOutput(
        summary="s", commit_message="c", required_checks=["documentation"], replies=[],
    )
    state.test_retry_count = 2

    from cai.workflows.docs import DocsNode
    result = _run(TestSanityNode(), state)

    assert isinstance(result, DocsNode)
    assert state.tests_passed is True


@patch("cai.workflows.test_runner._run_tests", return_value=(True, ""))
def test_sanity_transitions_to_pr_when_no_checks(
    mock_run_tests, state,
):
    """When tests pass and required_checks is empty, TestSanityNode
    transitions to PRNode."""
    state.implement_output = ImplementOutput(
        summary="s", commit_message="c", required_checks=[], replies=[],
    )
    state.test_retry_count = 2

    from cai.workflows.pr import PRNode
    result = _run(TestSanityNode(), state)

    assert isinstance(result, PRNode)
    assert state.tests_passed is True


@patch("cai.workflows.test_runner._run_tests", return_value=(True, ""))
def test_sanity_sets_tests_passed_and_no_failure_details(
    mock_run_tests, state,
):
    """When tests pass, TestSanityNode sets tests_passed=True and clears
    failure details."""
    state.implement_output = ImplementOutput(
        summary="s", commit_message="c", required_checks=[], replies=[],
    )
    state.test_failure_details = "old failure"
    state.test_retry_count = 2

    _run(TestSanityNode(), state)

    assert state.tests_passed is True
    # test_failure_details should now be empty since run_tests returned ("", True)
    assert state.test_failure_details == ""
