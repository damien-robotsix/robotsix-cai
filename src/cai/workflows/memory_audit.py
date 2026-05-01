"""``cai-memory-audit`` CLI: sweep ``.cai/memory/`` entries and mark stale ones.

The pipeline runs as a single-node graph: ``MemoryAuditNode`` invokes the
``memory_audit`` agent to list, verify, and update memory entries. No GitHub
interaction — this is a purely local informational/cleanup workflow designed
to be run manually or on a periodic schedule.
"""
from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from pydantic import BaseModel
from pydantic_graph import BaseNode, End, Graph, GraphRunContext

from cai.agents.loader import build_deep_agent, parse_agent_md, resolve_agent_path
from cai.workflows._deps import repo_deps


class MemoryAuditOutput(BaseModel):
    """Structured summary produced by the memory_audit agent."""

    entries_checked: int
    entries_marked_stale: list[str] = []
    entries_marked_superseded: list[str] = []
    entries_unchanged: list[str] = []
    summary: str


@dataclass
class MemoryAuditState:
    repo_root: Path
    output: MemoryAuditOutput | None = field(default=None)


@lru_cache(maxsize=1)
def _memory_audit_agent():
    config, instructions = parse_agent_md(resolve_agent_path("memory_audit"))
    return build_deep_agent(config, instructions, output_type=MemoryAuditOutput)


class MemoryAuditNode(BaseNode[MemoryAuditState, None, MemoryAuditOutput]):
    """Run the memory audit agent against ``.cai/memory/``."""

    async def run(
        self, ctx: GraphRunContext[MemoryAuditState]
    ) -> End[MemoryAuditOutput]:
        deps = repo_deps(
            ctx.state.repo_root,
            write_globs=[".cai/memory/**"],
        )
        result = await _memory_audit_agent().run(
            f"Audit all entries under .cai/memory/ in the repository at "
            f"{ctx.state.repo_root}. For each entry, read its frontmatter, "
            f"verify its claims against the current codebase, and update "
            f"status fields as needed.",
            deps=deps,
        )
        output: MemoryAuditOutput = result.output
        ctx.state.output = output
        return End(output)


memory_audit_graph: Graph[MemoryAuditState, None, MemoryAuditOutput] = Graph(
    nodes=[MemoryAuditNode]
)


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="cai-memory-audit",
        description="Scan .cai/memory/ entries and mark stale or superseded ones.",
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path.cwd(),
        help="Path to the repository root (default: current directory).",
    )
    args = parser.parse_args()

    repo_root = args.repo_root.resolve()
    state = MemoryAuditState(repo_root=repo_root)

    async def _run() -> None:
        await memory_audit_graph.run(MemoryAuditNode(), state=state)

    asyncio.run(_run())

    output = state.output
    if output:
        print(f"Entries checked: {output.entries_checked}")
        print(f"Marked stale: {len(output.entries_marked_stale)}")
        for path in output.entries_marked_stale:
            print(f"  - {path}")
        print(f"Marked superseded: {len(output.entries_marked_superseded)}")
        for path in output.entries_marked_superseded:
            print(f"  - {path}")
        print(f"Unchanged: {len(output.entries_unchanged)}")
        print(f"\n{output.summary}")


if __name__ == "__main__":
    main()
