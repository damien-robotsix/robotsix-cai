"""Handler for issues in IssueState.PR — bounces to the PR submachine.

An issue is in ``:pr-open`` state while its linked PR is being processed
by the PR pipeline. The issue-side handler looks up the linked PR and
dispatches it; the issue itself has no work to do until the PR merges
(at which point the ``pr_to_merged`` / ``merged_to_solved`` transitions
advance it).
"""
import re
import subprocess
import sys

from cai_lib.config import REPO
from cai_lib.github import _gh_json


def handle_pr_bounce(issue: dict) -> int:
    """Find the PR linked to *issue* and dispatch it.

    Called when an issue is at :pr-open. The linked PR's branch is
    ``auto-improve/<issue_number>-*``; we fetch the open PR with that
    head and hand it off to :func:`cai_lib.dispatcher.dispatch_pr`.
    """
    from cai_lib.dispatcher import dispatch_pr  # local to avoid import cycle

    issue_number = issue["number"]
    prefix = f"auto-improve/{issue_number}-"
    try:
        prs = _gh_json([
            "pr", "list",
            "--repo", REPO,
            "--state", "open",
            "--json", "number,headRefName",
            "--limit", "50",
        ]) or []
    except subprocess.CalledProcessError as e:
        print(
            f"[cai dispatch] gh pr list failed for issue #{issue_number}:\n{e.stderr}",
            file=sys.stderr,
        )
        return 1

    linked = [p for p in prs if p.get("headRefName", "").startswith(prefix)]
    if not linked:
        print(
            f"[cai dispatch] issue #{issue_number} at :pr-open has no linked open PR; "
            "nothing to dispatch",
            flush=True,
        )
        return 0

    return dispatch_pr(linked[0]["number"])
