from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_ai.usage import UsageLimits
from pydantic_deep import DeepAgentDeps, LocalBackend
from pydantic_graph import BaseNode, GraphRunContext

from cai.agents.loader import build_deep_agent, parse_agent_md, resolve_agent_path

AGENT_DEFINITION = resolve_agent_path("docs")


@lru_cache(maxsize=1)
def _docs_agent():
    config, instructions = parse_agent_md(AGENT_DEFINITION)
    return build_deep_agent(config, instructions, output_type=DocsOutput)


def _deps(repo_root: Path) -> DeepAgentDeps:
    return DeepAgentDeps(
        backend=LocalBackend(
            root_dir=str(repo_root),
            allowed_directories=[str(repo_root)],
        )
    )


class DocsNode(BaseNode[IssueState]):
    async def run(self, ctx: GraphRunContext[IssueState]) -> PRNode:
        state = ctx.state
        assert state.new_meta is not None
        assert state.implement_output is not None

        body = state.body_path.read_text()
        meta_json = state.new_meta.model_dump_json(indent=2)

        prompt = (
            "Review the implementation changes and ensure the `docs/` folder is up to date.\n\n"
            "Return:\n"
            "- summary: a concise description of the documentation changes made\n"
            "- commit_message: a commit message for these changes (empty if none)\n\n"
            f"## Issue metadata\n\n{meta_json}\n\n"
            f"## Issue body (plan)\n\n{body}\n\n"
            f"## Implementation summary\n\n{state.implement_output.summary}\n\n"
            f"## Implementation commit message\n\n{state.implement_output.commit_message}"
        )

        result = await _docs_agent().run(
            prompt,
            deps=_deps(state.repo_root),
            usage_limits=UsageLimits(request_limit=50),
        )
        state.docs_output = result.output
        return PRNode()
