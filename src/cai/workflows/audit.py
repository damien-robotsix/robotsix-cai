"""``cai-audit`` CLI: ask the audit agent to mine recent traces and file issues.

The pipeline runs as a graph: RunAuditNode → CreateIssuesNode. RunAuditNode
short-circuits to End when the agent proposes nothing. CreateIssuesNode
runs the issue-deduplicator agent per proposed item to decide whether to
create a new issue, append a comment to an existing one, or discard.
"""
from __future__ import annotations

import argparse
import sys
import typing
from dataclasses import dataclass, field
from functools import lru_cache

from pydantic import BaseModel
from pydantic_graph import BaseNode, End, Graph, GraphRunContext

from cai.github.bot import CaiBot
from cai.log.observability import langfuse_workflow, setup_langfuse

from cai.agents.loader import load_agent_from_md, resolve_agent_path

class ProposedIssue(BaseModel):
    title: str
    body: str


class DedupeOutput(BaseModel):
    action: typing.Literal["new", "discard", "append"]
    target_issue_number: int | None
    reason: str


class AuditOutput(BaseModel):
    issues: list[ProposedIssue]


@dataclass
class AuditState:
    bot: CaiBot
    repo: str
    prompt: str
    output: AuditOutput | None = field(default=None)


@lru_cache(maxsize=1)
def _audit_agent():
    return load_agent_from_md(AGENT_DIR / "audit.md", output_type=AuditOutput)


@lru_cache(maxsize=1)
def _dedupe_agent():
    return load_agent_from_md(
        AGENT_DIR / "issue_deduplicator.md", output_type=DedupeOutput
    )


class RunAuditNode(BaseNode[AuditState, None, AuditOutput]):
    """Run the audit agent against recent traces."""

    async def run(
        self, ctx: GraphRunContext[AuditState]
    ) -> "CreateIssuesNode | End[AuditOutput]":
        result = _audit_agent().run_sync(ctx.state.prompt)
        output: AuditOutput = result.data
        ctx.state.output = output
        if not output.issues:
            print("No issues proposed by the audit agent.", file=sys.stderr)
            return End(output)
        return CreateIssuesNode()


class CreateIssuesNode(BaseNode[AuditState, None, AuditOutput]):
    """Per proposed issue: dedupe, then create/append/discard."""

    async def run(self, ctx: GraphRunContext[AuditState]) -> End[AuditOutput]:
        assert ctx.state.output is not None
        repo_obj = ctx.state.bot.repo(ctx.state.repo)

        open_issues = repo_obj.get_issues(state="open")
        open_issues_summary = (
            "\n".join(f"#{issue.number}: {issue.title}" for issue in open_issues)
            or "No open issues."
        )
        dedupe_agent = _dedupe_agent()

        for issue in ctx.state.output.issues:
            print(f"Evaluating proposed issue: {issue.title}")
            dedupe_prompt = (
                f"Proposed issue title: {issue.title}\n"
                f"Proposed issue body: {issue.body}\n\n"
                f"Currently open issues:\n"
                f"{open_issues_summary}"
            )
            dedupe_decision: DedupeOutput = dedupe_agent.run_sync(dedupe_prompt).data

            if dedupe_decision.action == "discard":
                print(
                    f"Discarding issue '{issue.title}': {dedupe_decision.reason}"
                )
                continue

            if (
                dedupe_decision.action == "append"
                and dedupe_decision.target_issue_number is not None
            ):
                target_issue = repo_obj.get_issue(
                    dedupe_decision.target_issue_number
                )
                print(
                    f"Appending issue '{issue.title}' to "
                    f"#{target_issue.number}: {dedupe_decision.reason}"
                )
                target_issue.create_comment(
                    "**Additional proposed issue details:**\n\n"
                    f"**Title**: {issue.title}\n\n"
                    f"**Body**:\n{issue.body}"
                )
                continue

            if dedupe_decision.action == "append":
                # action=append without a target — agent didn't pick one.
                # Falling back to creating a new issue keeps the proposal
                # rather than dropping it on the floor.
                print(
                    f"Warning: Deduplicator agent suggested appending "
                    f"'{issue.title}' but didn't provide a "
                    f"target_issue_number. Reason: {dedupe_decision.reason}. "
                    "Falling back to creating a new issue.",
                    file=sys.stderr,
                )

            created = repo_obj.create_issue(
                title=issue.title,
                body=issue.body,
                labels=["cai:audit"],
            )
            print(f"Created: {created.html_url}")

        return End(ctx.state.output)


audit_graph: Graph[AuditState, None, AuditOutput] = Graph(
    nodes=[RunAuditNode, CreateIssuesNode]
)


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="cai-audit",
        description="Run the audit agent to analyze Langfuse traces and open GitHub issues.",
    )
    parser.add_argument(
        "--repo",
        required=True,
        help="Target GitHub repository for creating issues, e.g., owner/repo.",
    )
    args, unknown = parser.parse_known_args()

    setup_langfuse()
<<<<<<< HEAD
<<<<<<< HEAD
    
    agent = load_agent_from_md(
        resolve_agent_path("audit"),
        output_type=AuditOutput,
    )
    
    bot = CaiBot()
    repo_obj = bot.repo(args.repo)
    
    prompt = f"Please audit the recent workflow traces. Analyze them and draft improvements as proposed issues."
=======

    prompt = "Please audit the recent workflow traces. Analyze them and draft improvements as proposed issues."
>>>>>>> origin/main
=======

    prompt = "Please audit the recent workflow traces. Analyze them and draft improvements as proposed issues."
>>>>>>> origin/main
    if unknown:
        prompt += f" Additional context: {' '.join(unknown)}"

    state = AuditState(bot=CaiBot(), repo=args.repo, prompt=prompt)

    with langfuse_workflow("cai-audit", metadata={"repo": args.repo}):
        audit_graph.run_sync(RunAuditNode(), state=state)


if __name__ == "__main__":
    main()
