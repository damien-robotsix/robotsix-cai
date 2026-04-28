"""``cai-resolve-conflicts`` CLI: merge ``base`` into a PR and let the
implement agent resolve the resulting markers.

The flow is:

1. Prepare the PR workspace (clones the head branch).
2. Fetch ``origin`` and merge ``origin/<base>`` with ``--no-ff --no-commit``.
3. If the merge is clean, push the merge commit and exit (no agent work).
4. If the merge conflicts, stage everything (conflict markers and all),
   commit the in-progress merge, and push so the PR visibly shows the
   conflicted state. Then enter the standard ``solve_graph`` at
   ``ImplementNode`` with a synthetic body that lists the conflicted
   files and instructs the agent to resolve the markers. ``PRNode``
   pushes the resolution commit on top of the merge commit.

Prints a JSON summary on stdout.
"""
from __future__ import annotations

import argparse
import json
import sys

from cai.git import commit, fetch, merge_no_commit, push_branch, stage_all
from cai.github.bot import CaiBot
from cai.github.issues import IssueMeta
from cai.github.repo import (
    PRWorkspace,
    is_pull_request,
    parse_pr_ref,
    prepare_pr_workspace,
)
<<<<<<< HEAD
<<<<<<< HEAD
from cai.log import langfuse_workflow
=======
from cai.log import langfuse_workflow, session_id_for_pr
>>>>>>> origin/main
=======
from cai.log import langfuse_workflow, session_id_for_pr
>>>>>>> origin/main
from cai.workflows.fsm import solve_graph
from cai.workflows.implement import ImplementNode
from cai.workflows.state import IssueState

_MERGE_AUTHOR_NAME = "cai-bot"
_MERGE_AUTHOR_EMAIL = "cai-bot@users.noreply.github.com"


def _conflict_body(base: str, head: str, conflicted: list[str]) -> str:
    """Synthetic implementation-plan body for the implement agent."""
    listing = "\n".join(f"- `{p}`" for p in conflicted)
    return (
        f"# Resolve merge conflicts: `{base}` into `{head}`\n\n"
        f"`{base}` was merged into this branch and the following files were "
        f"left with unresolved conflict markers (`<<<<<<<`, `=======`, "
        f"`>>>>>>>`):\n\n"
        f"{listing}\n\n"
        "## Plan\n\n"
        f"For each file above, edit it to remove every conflict marker and "
        f"keep the correct combination of changes from both `{base}` and "
        f"this branch. Read the surrounding code and recent commits to "
        f"decide intent — favour preserving behaviour from both sides "
        f"unless one side clearly supersedes the other. After resolving, "
        f"the file must contain no `<<<<<<<`, `=======`, or `>>>>>>>` "
        f"sequences and the project's tests must pass.\n\n"
        "### Files to change\n\n"
        f"{listing}\n"
    )


def _do_merge(workspace: PRWorkspace) -> list[str]:
    """Fetch and merge ``origin/<base>`` into the head branch.

    Returns the list of conflicted paths after the merge attempt; an empty
    list means the merge was clean. In the conflict case the in-progress
    merge is committed (markers and all) so the resulting commit can be
    pushed and the agent has a stable HEAD to edit against.
    """
    fetch(workspace.repo_root, env={"GIT_TERMINAL_PROMPT": "0"})
    conflicts = merge_no_commit(
        workspace.repo_root,
        f"origin/{workspace.base_branch}",
        author_name=_MERGE_AUTHOR_NAME,
        author_email=_MERGE_AUTHOR_EMAIL,
    )
    stage_all(workspace.repo_root)
    if conflicts:
        msg = (
            f"merge: {workspace.base_branch} into {workspace.head_branch} "
            "(conflicts pending resolution)"
        )
    else:
        msg = f"merge: {workspace.base_branch} into {workspace.head_branch}"
    commit(
        workspace.repo_root,
        msg,
        author_name=_MERGE_AUTHOR_NAME,
        author_email=_MERGE_AUTHOR_EMAIL,
    )
    return conflicts


def _push(bot: CaiBot, workspace: PRWorkspace) -> None:
    token = bot.token_for(workspace.repo)
    remote_url = (
        f"https://x-access-token:{token}@github.com/{workspace.repo}.git"
    )
    push_branch(
        workspace.repo_root,
        remote_url,
        workspace.head_branch,
        env={"GIT_TERMINAL_PROMPT": "0"},
    )


def solve_conflicts(bot: CaiBot, workspace: PRWorkspace) -> dict:
    """Merge the PR base into the PR and resolve conflicts via the graph.

    Returns a small status dict describing what happened (``mode`` is
    either ``"clean"`` when the merge needed no agent work or
    ``"resolved"`` when the implement step was run).
    """
    conflicts = _do_merge(workspace)
    _push(bot, workspace)
    if not conflicts:
        return {"mode": "clean", "conflicted_files": []}

    workspace.body_path.write_text(
        _conflict_body(workspace.base_branch, workspace.head_branch, conflicts)
    )
    meta = IssueMeta(
        repo=workspace.repo,
        number=workspace.number,
        title=workspace.title,
    )
    state = IssueState(
        bot=bot,
        meta=meta,
        body_path=workspace.body_path.resolve(),
        repo_root=workspace.repo_root.resolve(),
        branch_name=workspace.head_branch,
        pr_number=workspace.number,
    )
    state.new_meta = meta
    pr_ref = f"{workspace.repo}#{workspace.number}"
    with langfuse_workflow(
        "cai-resolve-conflicts",
        input={
            "pr": pr_ref,
            "base": workspace.base_branch,
            "head": workspace.head_branch,
            "conflicted_files": conflicts,
        },
        metadata={"repo": workspace.repo, "pr_number": workspace.number},
<<<<<<< HEAD
<<<<<<< HEAD
=======
        session_id=session_id_for_pr(workspace.number, workspace.head_branch),
>>>>>>> origin/main
=======
        session_id=session_id_for_pr(workspace.number, workspace.head_branch),
>>>>>>> origin/main
    ):
        solve_graph.run_sync(ImplementNode(), state=state)
    return {"mode": "resolved", "conflicted_files": conflicts}


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="cai-resolve-conflicts",
        description=(
            "Merge a pull request's base branch into its head and let the "
            "implement agent resolve any conflict markers. Pushes both the "
            "in-progress merge commit and the resolution commit. Prints a "
            "JSON summary on stdout."
        ),
    )
    parser.add_argument(
        "ref",
        help="PR reference, formatted as owner/repo#number.",
    )
    args = parser.parse_args()

    parsed = parse_pr_ref(args.ref)
    if parsed is None:
        parser.error(f"expected owner/repo#number, got {args.ref!r}")
    repo, number = parsed

    bot = CaiBot()
    if not is_pull_request(bot, repo, number):
        parser.error(f"{args.ref} is not a pull request")

    workspace = prepare_pr_workspace(bot, repo, number)
    result = solve_conflicts(bot, workspace)
    json.dump(
        {"pr": f"{repo}#{number}", "branch": workspace.head_branch, **result},
        sys.stdout,
        indent=2,
    )
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
