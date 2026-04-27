"""``cai-address`` CLI: pick reviewer comments on a PR and resolve them.

For each unresolved review thread on the given pull request that is not
authored by ``cai[bot]``, run the address agent. The agent either fixes
the code (one commit per thread) or replies with reasoning. Threads with
landed fixes are resolved on GitHub. A single push at the end carries
all the new commits to the PR head branch.
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict

from cai.github.bot import CaiBot
from cai.github.repo import parse_pr_ref, prepare_pr_workspace
from cai.workflows.address_loop import address_pr


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="cai-address",
        description=(
            "Address reviewer comments on a pull request. Walks each "
            "unresolved review thread (excluding ones authored by "
            "cai[bot]) and either commits a fix or replies with "
            "reasoning. Prints a JSON summary of per-thread outcomes."
        ),
    )
    parser.add_argument(
        "pr",
        help="Pull request reference, formatted as owner/repo#number.",
    )
    args = parser.parse_args()

    ref = parse_pr_ref(args.pr)
    if ref is None:
        parser.error(f"expected owner/repo#number, got {args.pr!r}")
    repo, number = ref

    bot = CaiBot()
    workspace = prepare_pr_workspace(bot, repo, number)
    results = address_pr(bot, workspace)

    json.dump(
        {
            "pr": f"{repo}#{number}",
            "branch": workspace.head_branch,
            "threads": [asdict(r) for r in results],
        },
        sys.stdout,
        indent=2,
    )
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
