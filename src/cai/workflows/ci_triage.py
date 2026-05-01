"""``cai-ci-triage`` CLI: triage CI failures by analyzing job logs.

Triggered by a ``workflow_run`` event when the CI workflow completes.
Fetches failed job logs from the GitHub Actions API, hands them to a
triage agent that inspects relevant code and identifies the root cause,
then calls ``raise_issue`` to file a ``cai:raised`` issue with findings.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache

import httpx
from pydantic_graph import BaseNode, End, Graph, GraphRunContext

from cai.agents.loader import build_deep_agent, parse_agent_md, resolve_agent_path
from cai.github.bot import CaiBot
from cai.log.observability import langfuse_workflow, setup_langfuse


@dataclass
class CiTriageState:
    bot: CaiBot
    repo: str
    run_id: int


@lru_cache(maxsize=1)
def _ci_triage_agent():
    config, instructions = parse_agent_md(resolve_agent_path("ci_triage"))
    return build_deep_agent(config, instructions)


class FetchAndTriageNode(BaseNode[CiTriageState, None, None]):
    """Fetch failed CI job logs, triage with an LLM agent, and file findings."""

    async def run(
        self, ctx: GraphRunContext[CiTriageState]
    ) -> End[None]:
        token = ctx.state.bot.token_for(ctx.state.repo)
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
        }

        async with httpx.AsyncClient() as client:
            # Fetch jobs for the workflow run
            jobs_url = (
                f"https://api.github.com/repos/{ctx.state.repo}/actions/runs/"
                f"{ctx.state.run_id}/jobs"
            )
            jobs_resp = await client.get(jobs_url, headers=headers)
            jobs_resp.raise_for_status()
            jobs_data = jobs_resp.json()

            failed_jobs = [
                job
                for job in jobs_data.get("jobs", [])
                if job.get("conclusion") == "failure"
            ]

            if not failed_jobs:
                print("No failed jobs found in this workflow run.", file=sys.stderr)
                return End(None)

            # Build prompt from failed job logs
            prompt_parts = [
                "The CI workflow has failed. Below are the details of the failed jobs.\n\n",
            ]

            for job in failed_jobs:
                job_name = job.get("name", "unknown")
                prompt_parts.append(f"## Job: {job_name}\n\n")

                # Find the failing step
                for step in job.get("steps", []):
                    if step.get("conclusion") in ("failure", "cancelled"):
                        step_name = step.get("name", "unknown step")
                        prompt_parts.append(f"**Failed step:** {step_name}\n\n")
                        break

                # Download job logs
                logs_url = (
                    f"https://api.github.com/repos/{ctx.state.repo}/actions/jobs/"
                    f"{job['id']}/logs"
                )
                logs_resp = await client.get(logs_url, headers=headers)
                logs_resp.raise_for_status()
                logs_text = logs_resp.text

                # Truncate logs if they're very large (keep last 8000 chars)
                if len(logs_text) > 8000:
                    logs_text = (
                        f"... (logs truncated, showing last 8000 of {len(logs_text)} characters)\n\n"
                        + logs_text[-8000:]
                    )

                prompt_parts.append(f"```\n{logs_text}\n```\n\n")

            prompt = "".join(prompt_parts)

            # Run the triage agent — it calls raise_issue to file findings
            await _ci_triage_agent().run(prompt)

        return End(None)


ci_triage_graph: Graph[CiTriageState, None, None] = Graph(
    nodes=[FetchAndTriageNode]
)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="cai-ci-triage",
        description="Fetch failed CI job logs, triage the root cause, and file a cai:raised issue.",
    )
    parser.add_argument(
        "--repo",
        required=True,
        help="Target GitHub repository, e.g., owner/repo.",
    )
    parser.add_argument(
        "--run-id",
        required=True,
        type=int,
        help="GitHub Actions workflow run ID.",
    )
    args, _unknown = parser.parse_known_args()

    setup_langfuse()

    bot = CaiBot()
    state = CiTriageState(
        bot=bot,
        repo=args.repo,
        run_id=args.run_id,
    )

    session_id = f"ci-triage-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}"

    async def _run() -> None:
        with langfuse_workflow(
            "cai-ci-triage",
            metadata={"repo": args.repo, "run_id": args.run_id},
            session_id=session_id,
        ):
            await ci_triage_graph.run(FetchAndTriageNode(), state=state)

    asyncio.run(_run())


if __name__ == "__main__":
    main()
