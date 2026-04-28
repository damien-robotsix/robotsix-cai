from __future__ import annotations

import argparse
import sys
from pathlib import Path

from pydantic import BaseModel

from cai.agents.loader import AGENT_DIR, load_agent_from_md
from cai.github.bot import CaiBot
from cai.log.observability import langfuse_workflow, setup_langfuse


class ProposedIssue(BaseModel):
    title: str
    body: str


class AuditOutput(BaseModel):
    issues: list[ProposedIssue]


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
    
    agent = load_agent_from_md(
        AGENT_DIR / "audit.md",
        output_type=AuditOutput,
    )
    
    bot = CaiBot()
    repo_obj = bot.repo(args.repo)
    
    prompt = f"Please audit the recent workflow traces. Analyze them and draft improvements as proposed issues."
    if unknown:
        prompt += f" Additional context: {' '.join(unknown)}"

    with langfuse_workflow("cai-audit", metadata={"repo": args.repo}):
        result = agent.run_sync(prompt)
        
        output: AuditOutput = result.data
        if not output.issues:
            print("No issues proposed by the audit agent.", file=sys.stderr)
            return
            
        for issue in output.issues:
            print(f"Creating issue: {issue.title}")
            created = repo_obj.create_issue(
                title=issue.title,
                body=issue.body,
                labels=["cai:audit"],
            )
            print(f"Created: {created.html_url}")

if __name__ == "__main__":
    main()
