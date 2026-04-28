"""``cai-solve`` CLI: drive an issue or PR through the unified workflow.

For an issue ref, the graph runs end-to-end: ExploreNode → RefineNode →
ImplementNode → TestNode → … → PRNode (which opens a new PR).

For a PR ref, the same graph is entered at ImplementNode with the
unresolved review threads in the prompt; PRNode pushes the bundled
commit, posts per-thread replies, and resolves the threads it fixed.

Prints a JSON summary on stdout.
"""
from __future__ import annotations

import argparse
import json
import sys

from cai.github.bot import CaiBot
from cai.github.repo import (
    is_pull_request,
    parse_issue_ref,
    prepare_pr_workspace,
    prepare_workspace,
)
from cai.workflows.fsm import solve_issue, solve_pr


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="cai-solve",
        description=(
            "Drive a GitHub issue or pull request through the cai workflow. "
            "Issue refs are explored, refined, implemented, and pushed as a "
            "new PR. PR refs enter the workflow at the implement step with "
            "their unresolved review threads in the prompt — the agent "
            "writes a bundled commit and posts/resolves replies in place. "
            "Prints a JSON summary on stdout."
        ),
    )
    parser.add_argument(
        "ref",
        help="Issue or PR reference, formatted as owner/repo#number.",
    )
    args = parser.parse_args()

    parsed = parse_issue_ref(args.ref)
    if parsed is None:
        parser.error(f"expected owner/repo#number, got {args.ref!r}")
    repo, number = parsed

    bot = CaiBot()
    if is_pull_request(bot, repo, number):
        workspace = prepare_pr_workspace(bot, repo, number)
        meta = solve_pr(bot, workspace)
        json.dump(
            {
                "mode": "pr",
                "pr": f"{repo}#{number}",
                "branch": workspace.head_branch,
                "meta": meta.model_dump(),
            },
            sys.stdout,
            indent=2,
        )
    else:
        workspace = prepare_workspace(bot, repo, number)
        new_meta, pr_url = solve_issue(bot, workspace)
        json.dump(
            {
                "mode": "issue",
                "meta": new_meta.model_dump(),
                "pr_url": pr_url,
            },
            sys.stdout,
            indent=2,
        )
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
