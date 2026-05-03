from __future__ import annotations

from functools import lru_cache

from pydantic_ai.usage import UsageLimits
from pydantic_graph import BaseNode, GraphRunContext

from cai.agents.loader import build_deep_agent, parse_agent_md, resolve_agent_path
from cai.log.observability import traced_agent_run
from cai.workflows._deps import repo_deps
from cai.workflows.state import IssueState, PythonReviewOutput


@lru_cache(maxsize=1)
def _python_review_agent():
    config, instructions = parse_agent_md(resolve_agent_path("python_review"))
    return build_deep_agent(config, instructions, output_type=PythonReviewOutput)


class PythonReviewNode(BaseNode[IssueState]):
    async def run(self, ctx: GraphRunContext[IssueState]) -> GitHubWorkflowReviewNode:
        from cai.workflows.github_workflow_review import GitHubWorkflowReviewNode

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
        if state.implement_output.files_changed:
            files_list = "\n".join(f"- {f}" for f in state.implement_output.files_changed)
            prompt += f"\n\n## Files changed by implement\n\n{files_list}"
        reference_section = state.reference_files_section()
        if reference_section:
            prompt += "\n\n" + reference_section

        result = await traced_agent_run(
            "python_review",
            _python_review_agent(),
            prompt,
            deps=repo_deps(state.repo_root, write_dirs=[state.repo_root]),
            usage_limits=UsageLimits(request_limit=100),
        )
        state.python_review_output = result.output
        return GitHubWorkflowReviewNode()
