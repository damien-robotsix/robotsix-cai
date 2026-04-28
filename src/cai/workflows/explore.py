from __future__ import annotations

from functools import lru_cache

from pydantic_ai.usage import UsageLimits
from pydantic_graph import BaseNode, GraphRunContext

from cai.agents.loader import build_deep_agent, parse_agent_md, resolve_agent_path
AGENT_DEFINITION = resolve_agent_path("explore")


@lru_cache(maxsize=1)
def _explore_agent():
    config, instructions = parse_agent_md(AGENT_DEFINITION)
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
        result = await _explore_agent().run(
            prompt,
            deps=repo_deps(state.repo_root),
            usage_limits=UsageLimits(request_limit=50),
        )
        state.findings = result.output
        state.reference_files = list(result.output.related_files)
        return RefineNode()
