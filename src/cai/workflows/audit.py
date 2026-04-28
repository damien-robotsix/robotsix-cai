from __future__ import annotations

import argparse
import sys
import typing
from pathlib import Path

from pydantic import BaseModel

from cai.agents.loader import AGENT_DIR, load_agent_from_md
from cai.github.bot import CaiBot
from cai.log.observability import langfuse_workflow, setup_langfuse


class ProposedIssue(BaseModel):
    title: str
    body: str


class DedupeOutput(BaseModel):
    action: typing.Literal["new", "discard", "append"]
    target_issue_number: int | None
    reason: str


class AuditOutput(BaseModel):
    action: typing.Literal["new", "discard", "append"]
    target_issue_number: int | None
    reason: str


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
    
    dedupe_agent = load_agent_from_md(
        AGENT_DIR / "issue_deduplicator.md",
        output_type=DedupeOutput,
    )

    # Fetch open issues
    open_issues = repo_obj.get_issues(state="open")
    open_issues_summary = "\n".join(
        f"#{issue.number}: {issue.title}" for issue in open_issues
    )
    if not open_issues_summary:
        open_issues_summary = "No open issues."

    prompt = "Please audit the recent workflow traces. Analyze them and draft improvements as proposed issues."
    if unknown:
        prompt += f" Additional context: {' '.join(unknown)}"

    with langfuse_workflow("cai-audit", metadata={"repo": args.repo}):
        result = agent.run_sync(prompt)
        
        output: AuditOutput = result.data
        if not output.issues:
            print("No issues proposed by the audit agent.", file=sys.stderr)
            return
            
        for issue in output.issues:
            print(f"Evaluating proposed issue: {issue.title}")
            
            dedupe_prompt = (
                f"Proposed issue title: {issue.title}\n"
                f"Proposed issue body: {issue.body}\n\n"
                f"Currently open issues:\n"
                f"{open_issues_summary}"
            )
            
            dedupe_result = dedupe_agent.run_sync(dedupe_prompt)
            dedupe_decision: DedupeOutput = dedupe_result.data
            
            if dedupe_decision.action == "discard":
                print(f"Discarding issue '{issue.title}': {dedupe_decision.reason}")
            elif dedupe_decision.action == "append":
                if dedupe_decision.target_issue_number is None:
                    print(
                        f"Warning: Deduplicator agent suggested appending '{issue.title}' "
                        f"but didn't provide a target_issue_number. Reason: {dedupe_decision.reason}. "
                        "Falling back to creating a new issue.", file=sys.stderr
                    )
                    created = repo_obj.create_issue(
                        title=issue.title,
                        body=issue.body,
                        labels=["cai:audit"],
                    )
                    print(f"Created: {created.html_url}")
                else:
                    target_issue = repo_obj.get_issue(dedupe_decision.target_issue_number)
                    print(f"Appending issue '{issue.title}' to #{target_issue.number}: {dedupe_decision.reason}")
                    target_issue.create_comment(
                        f"**Additional proposed issue details:**\n\n**Title**: {issue.title}\n\n**Body**:\n{issue.body}"
                    )
            else:
                # new
                created = repo_obj.create_issue(
                    title=issue.title,
                    body=issue.body,
                    labels=["cai:audit"],
                )
                print(f"Created: {created.html_url}")

if __name__ == "__main__":
    main()
