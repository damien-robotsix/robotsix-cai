"""``cai-trace-followup`` CLI: daily check whether trace-investigation issues reproduced.

Lists open issues labelled ``cai:trace-investigation``, parses each body to
recover the original trace IDs, first-observed date, and trace filter hint,
then runs the ``trace_followup`` agent (which delegates to ``trace_analyst``)
to decide whether the symptom appeared again in yesterday's Langfuse traces.

When reproduction is confirmed: posts a comment with the new trace IDs and
updates a ``**Last reproduced**`` line in the issue body. The
``**Last checked**`` line is always updated so a human can tell when the
issue was last evaluated even if it never reproduces.
"""
from __future__ import annotations

import argparse
import asyncio
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from functools import lru_cache

from pydantic import BaseModel, Field
from pydantic_graph import BaseNode, End, Graph, GraphRunContext

from cai.agents.loader import build_deep_agent, parse_agent_md, resolve_agent_path
from cai.github.bot import CaiBot
from cai.log.observability import langfuse_workflow, setup_langfuse


_TRACE_INVESTIGATION_LABEL = "cai:trace-investigation"


class ReproductionResult(BaseModel):
    reproduced: bool = Field(
        description=(
            "True only when at least one of yesterday's Langfuse traces "
            "clearly exhibits the symptom described on the issue."
        )
    )
    supporting_trace_ids: list[str] = Field(
        default_factory=list,
        description=(
            "Yesterday's trace IDs that reproduce the symptom. Empty when "
            "reproduced is False."
        ),
    )
    notes: str = Field(
        description=(
            "Two or three sentences naming what was looked at and why the "
            "agent concluded yes/no."
        )
    )


@dataclass
class _IssueContext:
    """Parsed metadata extracted from a trace-investigation issue body."""
    number: int
    title: str
    body: str
    original_trace_ids: list[str]
    first_observed: str | None
    trace_filter: str | None


@dataclass
class TraceFollowupState:
    bot: CaiBot
    repo: str
    issues_processed: int = 0
    reproductions: int = 0
    results: list[tuple[int, ReproductionResult]] = field(default_factory=list)


@lru_cache(maxsize=1)
def _trace_followup_agent():
    config, instructions = parse_agent_md(resolve_agent_path("trace_followup"))
    return build_deep_agent(config, instructions, output_type=ReproductionResult)


# ---------------------------------------------------------------------------
# Body parsing / editing helpers
# ---------------------------------------------------------------------------

# Used both to read existing metadata lines from the body and to update them
# in place. Each captured value is the right-hand side of `**Key**: value`.
_FIRST_OBSERVED_RE = re.compile(r"^\*\*First observed\*\*:\s*(.*)$", re.MULTILINE)
_TRACE_FILTER_RE = re.compile(r"^\*\*Trace filter\*\*:\s*(.*)$", re.MULTILINE)
_TRACE_BULLET_RE = re.compile(r"^- `([^`]+)`\s*$", re.MULTILINE)


def _parse_issue_metadata(body: str) -> dict:
    """Extract trace-investigation metadata from an issue body.

    Looks only inside the ``## Relevant Traces`` section so unrelated mentions
    of trace IDs elsewhere in the body don't pollute the result.
    """
    if "## Relevant Traces" not in body:
        return {"trace_ids": [], "first_observed": None, "trace_filter": None}
    section = body[body.index("## Relevant Traces"):]
    first_observed_m = _FIRST_OBSERVED_RE.search(section)
    trace_filter_m = _TRACE_FILTER_RE.search(section)
    return {
        "trace_ids": _TRACE_BULLET_RE.findall(section),
        "first_observed": first_observed_m.group(1).strip() if first_observed_m else None,
        "trace_filter": trace_filter_m.group(1).strip() if trace_filter_m else None,
    }


def _set_metadata_line(body: str, key: str, value: str) -> str:
    """Insert or replace ``**{key}**: {value}`` in the issue body.

    Replaces an existing line with the same key when present. Otherwise
    inserts the line just before the first ``- `<trace_id>``` bullet so the
    new metadata sits in the same block as ``**First observed**`` and
    friends. Falls back to appending at the end of the body when no bullets
    are found (an unusual layout, but the issue body remains valid markdown).
    """
    line = f"**{key}**: {value}"
    pattern = re.compile(rf"^\*\*{re.escape(key)}\*\*:.*$", re.MULTILINE)
    if pattern.search(body):
        return pattern.sub(line, body, count=1)
    bullet_match = re.search(r"^- `[^`]+`", body, re.MULTILINE)
    if bullet_match:
        idx = bullet_match.start()
        return body[:idx] + line + "\n\n" + body[idx:]
    return body.rstrip() + "\n\n" + line + "\n"


def _format_reproduction_comment(
    result: ReproductionResult, checked_at: str
) -> str:
    """Render the comment posted on the issue when a reproduction is found."""
    bullets = "\n".join(f"- `{tid}`" for tid in result.supporting_trace_ids)
    return (
        "**Trace follow-up — reproduced.**\n\n"
        f"_Checked on {checked_at}._\n\n"
        f"{result.notes}\n\n"
        "### New supporting traces\n\n"
        f"{bullets}\n"
    )


# ---------------------------------------------------------------------------
# Graph nodes
# ---------------------------------------------------------------------------


def _yesterday_window(now: datetime | None = None) -> tuple[datetime, datetime]:
    """Return ``(start, end)`` covering the previous full UTC day.

    The end is the start of today (UTC); the start is 24 hours earlier. The
    follow-up always evaluates a closed day so successive runs operate on a
    fully-settled trace set.
    """
    now = now or datetime.now(timezone.utc)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return today_start - timedelta(days=1), today_start


def _build_followup_prompt(issue: _IssueContext, since: str, until: str) -> str:
    """Build the prompt fed to the ``trace_followup`` agent for one issue."""
    original_ids = (
        ", ".join(f"`{tid}`" for tid in issue.original_trace_ids)
        or "(none recorded)"
    )
    return (
        f"Open trace-investigation issue #{issue.number}: {issue.title}\n\n"
        f"## Issue body\n\n{issue.body}\n\n"
        f"## Follow-up scope\n\n"
        f"- First observed: {issue.first_observed or 'unknown'}\n"
        f"- Original supporting trace IDs: {original_ids}\n"
        f"- Trace filter hint: {issue.trace_filter or '(no hint provided)'}\n"
        f"- Date range to scan (UTC): {since} → {until}\n\n"
        f"Decide whether the symptom on this issue reproduced in any "
        f"Langfuse trace within the date range above. Use the trace filter "
        f"hint to scope your trace pull (failures, specific workflow, "
        f"specific agent — pick the right `traces_*` tool). For each "
        f"candidate trace, delegate to the trace_analyst subagent to "
        f"confirm whether it exhibits the symptom. Return a "
        f"ReproductionResult."
    )


class FollowupNode(BaseNode[TraceFollowupState, None, TraceFollowupState]):
    """List trace-investigation issues and run the follow-up agent on each."""

    async def run(
        self, ctx: GraphRunContext[TraceFollowupState]
    ) -> End[TraceFollowupState]:
        state = ctx.state
        repo_obj = state.bot.repo(state.repo)

        issues = list(
            repo_obj.get_issues(
                state="open", labels=[_TRACE_INVESTIGATION_LABEL]
            )
        )
        if not issues:
            print(
                "No open cai:trace-investigation issues found.", file=sys.stderr
            )
            return End(state)

        since_dt, until_dt = _yesterday_window()
        since_iso = since_dt.isoformat()
        until_iso = until_dt.isoformat()
        checked_at = datetime.now(timezone.utc).isoformat()
        agent = _trace_followup_agent()

        for issue in issues:
            body = issue.body or ""
            metadata = _parse_issue_metadata(body)
            ctx_issue = _IssueContext(
                number=issue.number,
                title=issue.title,
                body=body,
                original_trace_ids=metadata["trace_ids"],
                first_observed=metadata["first_observed"],
                trace_filter=metadata["trace_filter"],
            )

            print(
                f"Following up on #{issue.number}: {issue.title}",
                file=sys.stderr,
            )
            prompt = _build_followup_prompt(ctx_issue, since_iso, until_iso)

            try:
                result_obj = await agent.run(prompt)
            except Exception as exc:  # pragma: no cover - logged for visibility
                print(
                    f"  follow-up agent failed on #{issue.number}: {exc}",
                    file=sys.stderr,
                )
                state.issues_processed += 1
                continue

            result: ReproductionResult = result_obj.output
            state.issues_processed += 1
            state.results.append((issue.number, result))

            new_body = _set_metadata_line(body, "Last checked", checked_at)
            if result.reproduced and result.supporting_trace_ids:
                state.reproductions += 1
                new_body = _set_metadata_line(
                    new_body, "Last reproduced", checked_at
                )
                issue.create_comment(
                    _format_reproduction_comment(result, checked_at)
                )
                print(
                    f"  reproduced — {len(result.supporting_trace_ids)} "
                    f"supporting trace(s)",
                    file=sys.stderr,
                )
            else:
                print("  not reproduced", file=sys.stderr)

            if new_body != body:
                issue.edit(body=new_body)

        return End(state)


trace_followup_graph: Graph[TraceFollowupState, None, TraceFollowupState] = Graph(
    nodes=[FollowupNode]
)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="cai-trace-followup",
        description=(
            "Daily follow-up for cai:trace-investigation issues. "
            "Checks whether each open issue's symptom reproduced in "
            "yesterday's Langfuse traces and posts an update on the issue."
        ),
    )
    parser.add_argument(
        "--repo",
        required=True,
        help="GitHub repository to scan for trace-investigation issues, e.g. owner/repo.",
    )
    args, _unknown = parser.parse_known_args()

    setup_langfuse()

    bot = CaiBot()
    state = TraceFollowupState(bot=bot, repo=args.repo)

    from cai.workflows.registry import CliArgs, by_slug  # local — avoids circular import

    session_id = by_slug("trace-followup").session_id(CliArgs(repo=args.repo))

    async def _run() -> None:
        with langfuse_workflow(
            "cai-trace-followup",
            metadata={"repo": args.repo},
            session_id=session_id,
        ):
            await trace_followup_graph.run(FollowupNode(), state=state)

    asyncio.run(_run())

    print(
        f"Processed {state.issues_processed} issue(s); "
        f"{state.reproductions} reproduced.",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
