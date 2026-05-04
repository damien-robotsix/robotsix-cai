from __future__ import annotations

import os
import subprocess
import sys
from functools import lru_cache
from pathlib import Path

from pydantic_ai.usage import UsageLimits
from pydantic_graph import BaseNode, GraphRunContext

from cai.agents.loader import build_deep_agent, parse_agent_md, resolve_agent_path
from cai.log.observability import traced_agent_run
from cai.workflows._deps import repo_deps
from cai.workflows.state import IssueState, TestOutput


_STRIP_KEYS = frozenset({
    "OPENROUTER_API_KEY",
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "LANGFUSE_PUBLIC_KEY",
    "LANGFUSE_SECRET_KEY",
})
_OUTPUT_CAP = 20_000


@lru_cache(maxsize=1)
def _test_writer_agent():
    config, instructions = parse_agent_md(resolve_agent_path("test_writer"))
    return build_deep_agent(config, instructions, output_type=TestOutput)


def _run_tests(repo_root: Path) -> tuple[bool, str]:
    """Compile-check src/ then run pytest with API keys stripped from env.

    Returns (passed, failure_details). failure_details is empty on success.
    """
    env = {k: v for k, v in os.environ.items() if k not in _STRIP_KEYS}

    compile_result = subprocess.run(
        [sys.executable, "-m", "compileall", "-q", "src"],
        capture_output=True,
        text=True,
        cwd=str(repo_root),
        env=env,
    )
    if compile_result.returncode != 0:
        output = (compile_result.stdout + compile_result.stderr).strip()
        return False, f"Compile check failed:\n{output}"

    if not (repo_root / "tests").exists():
        return True, ""

    try:
        result = subprocess.run(
            [sys.executable, "-m", "pytest", "tests", "--tb=short", "-q", "--no-header"],
            capture_output=True,
            text=True,
            cwd=str(repo_root),
            env=env,
            timeout=300,
        )
    except subprocess.TimeoutExpired:
        return False, "Tests timed out after 300s."

    passed = result.returncode in (0, 5)  # 5 = no tests collected
    details = "" if passed else (result.stdout + result.stderr)[:_OUTPUT_CAP]
    return passed, details


class TestNode(BaseNode[IssueState]):
    async def run(
        self, ctx: GraphRunContext[IssueState]
    ) -> PythonReviewNode | DocsNode | PrePushValidationNode | ImplementNode:
        from cai.workflows.docs import DocsNode
        from cai.workflows.implement import ImplementNode
        from cai.workflows.pr import PRNode
        from cai.workflows.pre_push_validate import PrePushValidationNode
        from cai.workflows.python_review import PythonReviewNode

        state = ctx.state
        assert state.new_meta is not None
        assert state.implement_output is not None

        meta_json = state.new_meta.model_dump_json(indent=2)
        prompt = (
            "Write or update pytest unit tests for the implementation described below.\n\n"
            "Tests must never call LLM APIs or require external services.\n\n"
            f"## Issue metadata\n\n{meta_json}\n\n"
            f"## Implementation summary\n\n{state.implement_output.summary}\n\n"
            f"## Implementation commit message\n\n{state.implement_output.commit_message}"
        )
        if state.implement_output.files_changed:
            files_list = "\n".join(f"- {f}" for f in state.implement_output.files_changed)
            prompt += f"\n\n## Files changed by implement\n\n{files_list}"
        if state.findings is not None:
            prompt += f"\n\n## Codebase findings (explore agent)\n\n{state.findings.summary}"
        reference_section = state.reference_files_section()
        if reference_section:
            prompt += "\n\n" + reference_section

        tests_dir = state.repo_root / "tests"
        result = await traced_agent_run(
            "test_writer",
            _test_writer_agent(),
            prompt,
            deps=repo_deps(state.repo_root, write_dirs=[tests_dir]),
            usage_limits=UsageLimits(request_limit=100),
        )
        if getattr(result.output, "exhausted", False) is True:
            raise RuntimeError(f"Agent 'test_writer' exhausted retries: {result.output.summary}")
        state.test_output = result.output

        passed, details = _run_tests(state.repo_root)
        state.tests_passed = passed
        if passed:
            state.test_failure_details = ""
        else:
            state.test_failure_details = details

        if not passed and state.test_retry_count < 1:
            state.test_retry_count += 1
            return ImplementNode()

        checks = state.implement_output.required_checks
        if "python" in checks:
            return PythonReviewNode()
        if "documentation" in checks:
            return DocsNode()
        return PrePushValidationNode()


class TestSanityNode(BaseNode[IssueState]):
    async def run(
        self, ctx: GraphRunContext[IssueState]
    ) -> DocsNode | PrePushValidationNode | ImplementNode:
        from cai.workflows.docs import DocsNode
        from cai.workflows.implement import ImplementNode
        from cai.workflows.pr import PRNode
        from cai.workflows.pre_push_validate import PrePushValidationNode

        state = ctx.state
        assert state.implement_output is not None

        passed, details = _run_tests(state.repo_root)
        state.tests_passed = passed
        if passed:
            state.test_failure_details = ""
        else:
            state.test_failure_details = details

        if not passed and state.test_retry_count < 2:
            state.test_retry_count += 1
            return ImplementNode()

        if "documentation" in state.implement_output.required_checks:
            return DocsNode()
        return PrePushValidationNode()
