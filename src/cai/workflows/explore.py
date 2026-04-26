from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_ai.usage import UsageLimits
from pydantic_ai_backends.permissions.types import OperationPermissions, PermissionRule, PermissionRuleset
from pydantic_deep import DeepAgentDeps, LocalBackend
from pydantic_graph import BaseNode, GraphRunContext

from cai.agents.loader import AGENT_DIR, build_deep_agent, parse_agent_md
from cai.log import setup_langfuse
from cai.workflows.refine import RefineNode
from cai.workflows.state import ExploreOutput, IssueState

AGENT_DEFINITION = AGENT_DIR / "explore.md"


@lru_cache(maxsize=1)
def _explore_agent():
    setup_langfuse()
    config, instructions = parse_agent_md(AGENT_DEFINITION)
    return build_deep_agent(config, instructions, output_type=ExploreOutput)


_EXCLUDED_PATH_RULES = [
    PermissionRule(pattern="**/__pycache__/**", action="deny"),
    PermissionRule(pattern="**/pycache/**", action="deny"),
    PermissionRule(pattern="**/__pycache__", action="deny"),
    PermissionRule(pattern="**/*.pyc", action="deny"),
    PermissionRule(pattern="**/dist/**", action="deny"),
    PermissionRule(pattern="**/*.egg-info/**", action="deny"),
    PermissionRule(pattern="**/.git/**", action="deny"),
    PermissionRule(pattern="**/node_modules/**", action="deny"),
]

_READ_ONLY_PERMISSIONS = PermissionRuleset(
    default="allow",
    read=OperationPermissions(default="allow", rules=_EXCLUDED_PATH_RULES),
    glob=OperationPermissions(default="allow", rules=_EXCLUDED_PATH_RULES),
    grep=OperationPermissions(default="allow", rules=_EXCLUDED_PATH_RULES),
    ls=OperationPermissions(default="allow", rules=_EXCLUDED_PATH_RULES),
    write=OperationPermissions(default="deny"),
    edit=OperationPermissions(default="deny"),
    execute=OperationPermissions(default="deny"),
)


def _deps(repo_root: Path) -> DeepAgentDeps:
    return DeepAgentDeps(
        backend=LocalBackend(
            root_dir=str(repo_root),
            allowed_directories=[str(repo_root)],
            permissions=_READ_ONLY_PERMISSIONS,
        )
    )


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
            deps=_deps(state.repo_root),
            usage_limits=UsageLimits(request_limit=50),
        )
        state.findings = result.output
        return RefineNode()
