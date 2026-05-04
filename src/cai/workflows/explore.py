from __future__ import annotations

from functools import lru_cache

from pydantic_ai.usage import UsageLimits
from pydantic_graph import BaseNode, GraphRunContext

from cai.agents.loader import build_deep_agent, parse_agent_md, resolve_agent_path
from cai.log import setup_langfuse
from cai.log.observability import traced_agent_run
from cai.workflows._deps import repo_deps
from cai.workflows.refine import RefineNode
from cai.workflows.state import ExploreOutput, IssueState, save_session_state


@lru_cache(maxsize=1)
def _explore_agent():
    setup_langfuse()
    config, instructions = parse_agent_md(resolve_agent_path("explore"))
    return build_deep_agent(config, instructions, output_type=ExploreOutput)


class ExploreNode(BaseNode[IssueState]):
    async def run(self, ctx: GraphRunContext[IssueState]) -> RefineNode:
        state = ctx.state
        state.body = state.body_path.read_text()
        state.meta_json = state.meta.model_dump_json(indent=2)

        prompt = (
            "Investigate the codebase for context relevant to this GitHub issue.\n\n"
            "Return:\n"
            "- summary: a concise paragraph describing what you found\n"
            "- related_files: relative paths (from repo root) of the files most\n"
            "  relevant to this issue — include source files, tests, configs\n\n"
            f"## Issue metadata\n\n{state.meta_json}\n\n"
            f"## Issue body\n\n{state.body}"
        )
        if state.session_state is not None and state.session_state.explore_findings:
            prompt += (
                "\n\n## Prior session findings\n\n"
                "The following was discovered in a previous run on this same issue. "
                "Verify it is still accurate rather than re-discovering from scratch:\n\n"
                f"{state.session_state.explore_findings}"
            )
        result = await traced_agent_run(
            "explore",
            _explore_agent(),
            prompt,
            deps=repo_deps(state.repo_root),
            usage_limits=UsageLimits(request_limit=100),
        )
        if getattr(result.output, "exhausted", False) is True:
            raise RuntimeError(f"Agent 'explore' exhausted retries: {result.output.summary}")
        state.findings = result.output
        state.reference_files = list(result.output.related_files)
        if state.session_state is not None:
            if (
                not state.session_state.explore_findings
                or state.session_state.explore_findings != state.findings.summary
            ):
                state.session_state.explore_findings = state.findings.summary
                state.session_state.explore_files = list(result.output.related_files)
                save_session_state(state.session_state, state.body_path.parent)
        return RefineNode()
