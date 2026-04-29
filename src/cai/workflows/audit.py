"""``cai-audit`` CLI: ask the audit agent to mine recent traces and file issues.

The pipeline runs as a graph: RunAuditNode → CreateIssuesNode. RunAuditNode
short-circuits to End when the agent proposes nothing. CreateIssuesNode
runs the issue-deduplicator agent per proposed item to decide whether to
create a new issue, append a comment to an existing one, or discard.

Two audit modes are supported:
  --mode cost    Audit the most costly session of the last 10 issue-solving runs.
  --mode errors  Audit the 10 most recent traces that contain error-level observations.

In both modes all trace context is pre-fetched into the prompt so the audit
agent can delegate straight to trace_analyst without spending tokens on
listing tools.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import typing
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import lru_cache

from pydantic import BaseModel, model_validator
from pydantic_graph import BaseNode, End, Graph, GraphRunContext

from pydantic_deep import create_default_deps

from cai.agents.loader import AGENT_DIR, build_deep_agent, load_agent_from_md, parse_agent_md
from cai.github.bot import CaiBot
from cai.log.observability import langfuse_workflow, setup_langfuse
from cai.log.traces import _TRACES


class ProposedIssue(BaseModel):
    title: str
    body: str
    last_detected_at: str | None = None  # ISO timestamp of the most recent relevant trace


class DedupeOutput(BaseModel):
    action: typing.Literal["new", "discard", "append"]
    target_issue_number: int | None
    reason: str


class AuditOutput(BaseModel):
    issues: list[ProposedIssue]

    @model_validator(mode="before")
    @classmethod
    def _drop_nulls(cls, v: object) -> object:
        if isinstance(v, dict) and "issues" in v:
            v = {**v, "issues": [i for i in (v["issues"] or []) if i is not None]}
        return v


@dataclass
class AuditState:
    bot: CaiBot
    repo: str
    prompt: str
    output: AuditOutput | None = field(default=None)


@lru_cache(maxsize=1)
def _audit_agent():
    config, instructions = parse_agent_md(AGENT_DIR / "audit.md")
    return build_deep_agent(config, instructions, output_type=AuditOutput)


@lru_cache(maxsize=1)
def _dedupe_agent():
    return load_agent_from_md(
        AGENT_DIR / "issue_deduplicator.md", output_type=DedupeOutput
    )


class RunAuditNode(BaseNode[AuditState, None, AuditOutput]):
    """Run the audit agent against pre-fetched trace context."""

    async def run(
        self, ctx: GraphRunContext[AuditState]
    ) -> "CreateIssuesNode | End[AuditOutput]":
        result = await _audit_agent().run(ctx.state.prompt, deps=create_default_deps())
        output: AuditOutput = result.output
        ctx.state.output = output
        if not output.issues:
            print("No issues proposed by the audit agent.", file=sys.stderr)
            return End(output)
        return CreateIssuesNode()


class CreateIssuesNode(BaseNode[AuditState, None, AuditOutput]):
    """Per proposed issue: check recent commits, dedupe, then create/append/discard."""

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

            recent_commits_text = _recent_commits_since(repo_obj, issue.last_detected_at)

            dedupe_prompt = (
                f"Proposed issue title: {issue.title}\n"
                f"Proposed issue body: {issue.body}\n\n"
                f"Currently open issues:\n{open_issues_summary}"
                + recent_commits_text
            )
            dedupe_decision: DedupeOutput = (await dedupe_agent.run(dedupe_prompt)).output

            if dedupe_decision.action == "discard":
                print(f"Discarding issue '{issue.title}': {dedupe_decision.reason}")
                continue

            if (
                dedupe_decision.action == "append"
                and dedupe_decision.target_issue_number is not None
            ):
                target_issue = repo_obj.get_issue(dedupe_decision.target_issue_number)
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
                print(
                    f"Warning: Deduplicator suggested appending '{issue.title}' "
                    f"but provided no target_issue_number. "
                    f"Reason: {dedupe_decision.reason}. "
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


def _recent_commits_since(repo_obj: object, last_detected_at: str | None) -> str:
    """Return a formatted block of commits pushed after ``last_detected_at``.

    Returns an empty string when the timestamp is missing or the fetch fails.
    The block instructs the dedupe agent to discard the issue if any commit
    appears to address it.
    """
    if not last_detected_at:
        return ""
    try:
        since_dt = datetime.fromisoformat(
            last_detected_at.replace("Z", "+00:00")
        ).replace(tzinfo=timezone.utc)
        commits = list(repo_obj.get_commits(since=since_dt))[:20]  # type: ignore[attr-defined]
        if not commits:
            return ""
        lines = "\n".join(
            f"  {c.sha[:8]} {c.commit.message.splitlines()[0]}" for c in commits
        )
        return (
            f"\n\nCommits merged after the problem was last detected "
            f"({last_detected_at[:19]}):\n{lines}\n"
            f"If any of these commits appears to already address this issue, "
            f"set action to 'discard'."
        )
    except Exception as exc:
        print(f"Warning: could not fetch recent commits: {exc}", file=sys.stderr)
        return ""


audit_graph: Graph[AuditState, None, AuditOutput] = Graph(
    nodes=[RunAuditNode, CreateIssuesNode]
)


# ---------------------------------------------------------------------------
# Prompt builders — pre-fetch everything so the agent needs no listing tools
# ---------------------------------------------------------------------------

def _build_cost_prompt(unknown: list[str]) -> str:
    """Prompt for --mode cost: most expensive of last 10 issue-solving sessions."""
    session = _TRACES.most_costly_solve_session(n=10)
    if session is None:
        print("No issue-solving sessions found in Langfuse.", file=sys.stderr)
        sys.exit(1)

    traces = _TRACES.list_session_traces(session["session_id"])
    rows = [
        f"{'ID':<36} {'NAME':<25} {'TIMESTAMP':<22} {'COST':>9} {'LATENCY':>9}",
        "-" * 105,
    ]
    for t in traces:
        ts = (t["timestamp"] or "?")[:19]
        cost = f"${t['cost']:.4f}" if t["cost"] else "N/A"
        lat = f"{t['latency']:.1f}s" if t["latency"] else "N/A"
        rows.append(f"{t['id']:<36} {t['name']:<25} {ts:<22} {cost:>9} {lat:>9}")

    prompt = (
        f"Audit session {session['session_id']} — the most costly of the last 10 "
        f"issue-solving sessions (total cost: ${session['total_cost']:.4f}).\n\n"
        "Traces in this session:\n"
        + "\n".join(rows)
        + "\n\nDelegate deep inspection of interesting traces to trace_analyst. "
        "Draft improvements as proposed issues. "
        "Set last_detected_at to the ISO timestamp of the relevant trace for each issue."
    )
    if unknown:
        prompt += f"\n\nAdditional context: {' '.join(unknown)}"
    return prompt


def _build_errors_prompt(unknown: list[str]) -> str:
    """Prompt for --mode errors: 10 most recent traces with error-level observations."""
    failures = _TRACES.list_failures(limit=10)
    if not failures:
        print("No recent failures found in Langfuse.", file=sys.stderr)
        sys.exit(1)

    lines = [f"Recent failures ({len(failures)} traces with errors):"]
    for f in failures:
        ts = (f["timestamp"] or "?")[:19]
        lines.append(f"\n[{ts}] {f['name']}  trace_id={f['id']}")
        for e in f["errors"]:
            lines.append(f"  Failed step: {e['name']}")
            if e.get("status_message"):
                lines.append(f"    Message: {e['status_message'][:300]}")
            if e.get("output"):
                lines.append(f"    Output:  {e['output'][:200]}")

    prompt = (
        "Audit the following recent failures in Langfuse traces.\n\n"
        + "\n".join(lines)
        + "\n\nDelegate deep inspection of specific traces to trace_analyst. "
        "Draft improvements as proposed issues. "
        "Set last_detected_at to the ISO timestamp of the most recent relevant "
        "failure for each issue."
    )
    if unknown:
        prompt += f"\n\nAdditional context: {' '.join(unknown)}"
    return prompt


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
    parser.add_argument(
        "--mode",
        choices=["cost", "errors"],
        default="cost",
        help=(
            "Audit mode: 'cost' analyses the most expensive of the last 10 "
            "issue-solving sessions; 'errors' analyses the 10 most recent "
            "traces that contain error-level observations."
        ),
    )
    args, unknown = parser.parse_known_args()

    setup_langfuse()

    prompt = (
        _build_cost_prompt(unknown)
        if args.mode == "cost"
        else _build_errors_prompt(unknown)
    )
    state = AuditState(bot=CaiBot(), repo=args.repo, prompt=prompt)

    session_id = f"audit-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}"

    async def _run() -> None:
        with langfuse_workflow(
            "cai-audit",
            metadata={"repo": args.repo, "mode": args.mode},
            session_id=session_id,
        ):
            await audit_graph.run(RunAuditNode(), state=state)

    asyncio.run(_run())


if __name__ == "__main__":
    main()
