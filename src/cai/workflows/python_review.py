from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_ai.usage import UsageLimits
from pydantic_deep import DeepAgentDeps, LocalBackend
from pydantic_graph import BaseNode, GraphRunContext

from cai.agents.loader import AGENT_DIR, build_deep_agent, parse_agent_md
from cai.workflows.state import IssueState, PythonReviewOutput

AGENT_DEFINITION = AGENT_DIR / "python_review.md"


@lru_cache(maxsize=1)
def _python_review_agent():
    config, instructions = parse_agent_md(AGENT_DEFINITION)
    return build_deep_agent(config, instructions, output_type=PythonReviewOutput)


def _deps(repo_root: Path) -> DeepAgentDeps:
    return DeepAgentDeps(
        backend=LocalBackend(
            root_dir=str(repo_root),
            allowed_directories=[str(repo_root)],
        )
    )


class PythonReviewNode(BaseNode[IssueState]):
    async def run(self, ctx: GraphRunContext[IssueState]) -> TestSanityNode:
        from cai.workflows.test_runner import TestSanityNode

        state = ctx.state
        assert state.new_meta is not None
        assert state.implement_output is not None

        meta_json = state.new_meta.model_dump_json(indent=2)

        prompt = (
            "Review the Python files changed by the implementation agent.\n\n"
            "Fix only Critical and Warning issues from the rubric. "
            "Leave the commit_message empty if you made no changes.\n\n"
            f"## Issue metadata\n\n{meta_json}\n\n"
            f"## Implementation summary\n\n{state.implement_output.summary}\n\n"
            f"## Implementation commit message\n\n{state.implement_output.commit_message}"
        )

        result = await _python_review_agent().run(
            prompt,
            deps=_deps(state.repo_root),
            usage_limits=UsageLimits(request_limit=50),
        )
        state.python_review_output = result.output
        return TestSanityNode()
