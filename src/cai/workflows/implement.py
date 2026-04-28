from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_ai.usage import UsageLimits
from pydantic_deep import DeepAgentDeps, LocalBackend
from pydantic_graph import BaseNode, GraphRunContext

from cai.agents.loader import AGENT_DIR, build_deep_agent, parse_agent_md
from cai.git import checkout_branch
from cai.workflows.state import ImplementOutput, IssueState

AGENT_DEFINITION = AGENT_DIR / "implement.md"


@lru_cache(maxsize=1)
def _implement_agent():
    config, instructions = parse_agent_md(AGENT_DEFINITION)
    return build_deep_agent(config, instructions, output_type=ImplementOutput)


def _deps(repo_root: Path) -> DeepAgentDeps:
    return DeepAgentDeps(
        backend=LocalBackend(
            root_dir=str(repo_root),
            allowed_directories=[str(repo_root)],
        )
    )


def _branch_name(number: int) -> str:
    return f"cai/solve-{number}"


class ImplementNode(BaseNode[IssueState]):
    async def run(self, ctx: GraphRunContext[IssueState]) -> DocsNode | PRNode:
        from cai.workflows.docs import DocsNode

        state = ctx.state
        assert state.new_meta is not None
        assert state.new_meta.number is not None

        branch = _branch_name(state.new_meta.number)
        checkout_branch(state.repo_root, branch)
        state.branch_name = branch

        body = state.body_path.read_text()
        meta_json = state.new_meta.model_dump_json(indent=2)

        prompt = (
            "Implement the code changes described in this GitHub issue.\n\n"
            "Make all necessary changes to fully resolve the issue according to the plan.\n"
            "Return:\n"
            "- summary: a concise description of the changes you made\n"
            "- commit_message: a clear commit message for these changes\n"
            "- required_checks: list of checks needed for this MR (e.g. ['documentation'])\n\n"
            f"## Issue metadata\n\n{meta_json}\n\n"
            f"## Issue body (implementation plan)\n\n{body}"
        )
        reference_section = state.reference_files_section()
        if reference_section:
            prompt += "\n\n" + reference_section
        result = await _implement_agent().run(
            prompt,
            deps=_deps(state.repo_root),
            usage_limits=UsageLimits(request_limit=100),
        )
        state.implement_output = result.output
        if "documentation" in state.implement_output.required_checks:
            return DocsNode()
        return PRNode()
