from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_ai.usage import UsageLimits
from pydantic_deep import DeepAgentDeps, LocalBackend
from pydantic_graph import BaseNode, GraphRunContext

from cai.agents.loader import build_deep_agent, parse_agent_md, resolve_agent_path
from cai.log.observability import traced_agent_run
from cai.workflows.pr import PRNode
from cai.workflows.state import DocsOutput, IssueState


@lru_cache(maxsize=1)
def _docs_agent():
    config, instructions = parse_agent_md(resolve_agent_path("docs"))
    return build_deep_agent(config, instructions, output_type=DocsOutput, output_retries=3)


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
            "Identify every user-visible change in the implementation summary (new/changed CLI, "
            "workflow node, agent, label, trigger, env var, integration). For each, either update "
            "the right page under `docs/` or explain in your summary why it does not need a doc "
            "change (internal refactor, already covered at a specific path, etc.). A bare "
            "\"no updates needed\" is not acceptable.\n\n"
            "Return:\n"
            "- summary: per-change justification as described above\n"
            "- commit_message: a commit message for the docs changes, or an empty string only if "
            "the summary shows every user-visible change is already covered or internal-only\n\n"
            f"## Issue metadata\n\n{meta_json}\n\n"
            f"## Issue body (plan)\n\n{body}\n\n"
            f"## Implementation summary\n\n{state.implement_output.summary}\n\n"
            f"## Implementation commit message\n\n{state.implement_output.commit_message}"
        )

        result = await traced_agent_run(
            "docs",
            _docs_agent(),
            prompt,
            deps=_deps(state.repo_root),
            usage_limits=UsageLimits(request_limit=100),
        )
        state.docs_output = result.output
        return PRNode()
