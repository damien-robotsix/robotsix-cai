"""``cai-sourcing`` CLI: scan the open-source ecosystem for transferable tools.

The pipeline runs as a graph: RunSourcingNode → CreateIssuesNode.
RunSourcingNode short-circuits to End when the agent proposes nothing.
CreateIssuesNode runs the issue-deduplicator agent per proposed item to
decide whether to create a new issue, append a comment to an existing
one, or discard.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import lru_cache

from pydantic import BaseModel
from pydantic_graph import BaseNode, End, Graph, GraphRunContext

from cai.agents.loader import build_deep_agent, parse_agent_md, resolve_agent_path
from cai.github.bot import CaiBot
from cai.log.observability import langfuse_workflow, setup_langfuse
from cai.workflows.audit import ProposedIssue, _create_issues_from_proposals


def _labels_for_confidence(confidence: int) -> list[str]:
    routing = "cai:raised" if confidence >= 9 else "cai:human-review"
    return ["cai:sourcing", routing]


class SourcingOutput(BaseModel):
    issues: list[ProposedIssue]


class _SourcingInnerOutput(BaseModel):
    """Output shape the agent produces. Mirrors SourcingOutput but lives
    alongside it so the agent's schema doesn't carry the inline-refs
    machinery (which confuses OpenRouter structured-output routing)."""
    issues: list[ProposedIssue]


@dataclass
class SourcingState:
    bot: CaiBot
    repo: str
    prompt: str
    output: SourcingOutput | None = field(default=None)


@lru_cache(maxsize=1)
def _sourcing_agent():
    config, instructions = parse_agent_md(resolve_agent_path("sourcing"))
    return build_deep_agent(config, instructions, output_type=_SourcingInnerOutput)


class RunSourcingNode(BaseNode[SourcingState, None, SourcingOutput]):
    """Run the sourcing agent to discover transferable tools."""

    async def run(
        self, ctx: GraphRunContext[SourcingState]
    ) -> "CreateIssuesNode | End[SourcingOutput]":
        result = await _sourcing_agent().run(ctx.state.prompt)
        inner: _SourcingInnerOutput = result.output
        output = SourcingOutput(issues=inner.issues)
        ctx.state.output = output
        if not output.issues:
            print("No tools proposed by the sourcing agent.", file=sys.stderr)
            return End(output)
        return CreateIssuesNode()


class CreateIssuesNode(BaseNode[SourcingState, None, SourcingOutput]):
    """Per proposed issue: check recent commits, dedupe, then create/append/discard."""

    async def run(self, ctx: GraphRunContext[SourcingState]) -> End[SourcingOutput]:
        assert ctx.state.output is not None
        await _create_issues_from_proposals(
            bot=ctx.state.bot,
            repo_name=ctx.state.repo,
            issues=ctx.state.output.issues,
            labels_for_confidence=_labels_for_confidence,
        )
        return End(ctx.state.output)


sourcing_graph: Graph[SourcingState, None, SourcingOutput] = Graph(
    nodes=[RunSourcingNode, CreateIssuesNode]
)


# ---------------------------------------------------------------------------
# Prompt builder
# ---------------------------------------------------------------------------

def _build_sourcing_prompt() -> str:
    """Build the prompt asking the sourcing agent to scan for transferable tools."""
    return (
        "Scan the open-source ecosystem for tools, libraries, and frameworks "
        "that could be adopted by this project.\n\n"
        "The project covers these areas:\n"
        "- AI agent frameworks (pydantic-ai, pydantic-deep, OpenRouter integration)\n"
        "- GitHub automation (PyGithub-based bot, issue/PR workflows)\n"
        "- Observability (Langfuse tracing, cost tracking, error analysis)\n"
        "- Code analysis (jscpd duplication detection, architecture audits)\n"
        "- CI/CD (Docker-based containerized runners, GitHub Actions)\n\n"
        "For each area, research what the broader ecosystem offers:\n"
        "- Better alternatives to current dependencies\n"
        "- New entrants gaining traction that weren't available at the last scan\n"
        "- Approaches this project hasn't considered\n"
        "- Tools that similar projects (AI coding agents, GitHub bots) are adopting\n\n"
        "Use web_search to find candidates across each category. Use web_fetch "
        "to evaluate promising results — read READMEs, check GitHub repos for "
        "stars and recent commits, verify licenses.\n\n"
        "For each tool worth proposing:\n"
        "- Name the tool and link its repository and homepage\n"
        "- State its license\n"
        "- Explain what it would replace or add to this project\n"
        "- Describe the integration surface or migration path\n"
        "- Set last_detected_at to the current ISO timestamp\n"
        "- Score confidence per the rubric in your instructions\n\n"
        "Return a SourcingOutput with one ProposedIssue per tool or tool family. "
        "Be selective — only propose tools that are actively maintained "
        "(commits within the last 3 months) and have a compatible license "
        "(MIT, Apache-2.0, BSD, or similar)."
    )


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        prog="cai-sourcing",
        description="Scan the open-source ecosystem for transferable tools and file GitHub issues.",
    )
    parser.add_argument(
        "--repo",
        required=True,
        help="Target GitHub repository for creating issues, e.g., owner/repo.",
    )
    args, _unknown = parser.parse_known_args()

    setup_langfuse()

    bot = CaiBot()
    prompt = _build_sourcing_prompt()

    state = SourcingState(
        bot=bot,
        repo=args.repo,
        prompt=prompt,
    )

    from cai.workflows.registry import by_slug, CliArgs
    cli_args = CliArgs(repo=args.repo)
    session_id = by_slug("sourcing").session_id(cli_args)

    async def _run() -> None:
        with langfuse_workflow(
            "cai-sourcing",
            metadata={"repo": args.repo},
            session_id=session_id,
        ):
            await sourcing_graph.run(RunSourcingNode(), state=state)

    asyncio.run(_run())


if __name__ == "__main__":
    main()
