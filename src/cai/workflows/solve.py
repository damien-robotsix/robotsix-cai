"""``cai-solve`` CLI: pull a GitHub issue, refine it, implement the fix, and open a PR.

The pipeline runs as a single graph: ExploreNode → RefineNode → ImplementNode → PRNode.
Prints a JSON object with the refined issue metadata and the PR URL.
"""
from __future__ import annotations

import argparse
import json
import sys

from cai.github.bot import CaiBot
from cai.github.repo import parse_issue_ref, prepare_workspace
from cai.workflows.fsm import solve_issue


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="cai-solve",
        description=(
            "Materialize a per-issue workspace under /tmp/cai-solve, refine the "
            "issue, implement the fix, push a branch, and open a pull request. "
            "Prints the updated metadata and PR URL as JSON."
        ),
    )
    parser.add_argument(
        "issue",
        help="Issue reference, formatted as owner/repo#number.",
    )
    args = parser.parse_args()

    ref = parse_issue_ref(args.issue)
    if ref is None:
        parser.error(f"expected owner/repo#number, got {args.issue!r}")
    repo, number = ref

    bot = CaiBot()
    workspace = prepare_workspace(bot, repo, number)
    new_meta, pr_url = solve_issue(bot, workspace)

    json.dump({"meta": new_meta.model_dump(), "pr_url": pr_url}, sys.stdout, indent=2)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
