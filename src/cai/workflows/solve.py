"""``cai-solve`` CLI: pull a GitHub issue, refine it, and push the result back.

The graph refines the issue title and body in the per-issue workspace,
then ``push`` applies the refined files to the GitHub issue.
"""
from __future__ import annotations

import argparse
import json
import sys

from cai.github.bot import CaiBot
from cai.github.repo import parse_issue_ref, prepare_workspace
from cai.workflows.fsm import refine_files


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="cai-solve",
        description=(
            "Materialize a per-issue workspace under /tmp/cai-solve, run the "
            "refine graph on it, and push the refined title and body back to "
            "the GitHub issue. Prints the updated metadata as JSON."
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
    new_meta = refine_files(bot, workspace.issue_json, repo_root=workspace.repo_root)

    json.dump(new_meta.model_dump(), sys.stdout, indent=2)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
