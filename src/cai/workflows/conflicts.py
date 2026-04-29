"""``cai-resolve-conflicts`` CLI: rebase a PR onto its base branch and let
the resolve_step agent resolve conflicts step-by-step.

The flow is:

1. Prepare the PR workspace (clones the head branch).
2. Fetch ``origin`` and ``git rebase origin/<base>``.
3. While the rebase stops at a conflict, hand the current commit's diff
   plus the conflicted files to the ``resolve_step`` agent, then
   ``git rebase --continue``.
4. Run a sanity test pass once the rebase finishes.
5. On rebase or sanity failure, fall back to ``ImplementNode`` with a
   conflict-resolution body so the broader implement agent can take it
   home (the existing graceful-degradation path).
6. Push ``--force`` so the PR's diff view shows head-vs-base only,
   without a noisy merge commit.

Prints a JSON summary on stdout.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from functools import lru_cache
from pathlib import Path

from pydantic_ai.usage import UsageLimits
from pydantic_deep import DeepAgentDeps, LocalBackend

from cai.agents.loader import AGENT_DIR, build_deep_agent, parse_agent_md
from cai.git import (
    commit,
    conflicted_paths,
    current_rebase_step,
    fetch,
    merge_no_commit,
    push_branch,
    rebase_abort,
    rebase_continue,
    rebase_in_progress,
    rebase_onto,
    stage_all,
)
from cai.github.bot import CaiBot
from cai.github.issues import IssueMeta
from cai.github.repo import (
    PRWorkspace,
    is_pull_request,
    parse_pr_ref,
    prepare_pr_workspace,
)
from cai.log import langfuse_workflow, session_id_for_pr
from cai.workflows.fsm import solve_graph
from cai.workflows.implement import ImplementNode
from cai.workflows.state import IssueState, ResolveStepOutput
from cai.workflows.test_runner import _run_tests

_MERGE_AUTHOR_NAME = "cai-bot"
_MERGE_AUTHOR_EMAIL = "cai-bot@users.noreply.github.com"

RESOLVE_STEP_AGENT_DEFINITION = AGENT_DIR / "resolve_step.md"


@lru_cache(maxsize=1)
def _resolve_step_agent():
    config, instructions = parse_agent_md(RESOLVE_STEP_AGENT_DEFINITION)
    return build_deep_agent(config, instructions, output_type=ResolveStepOutput)


def _resolve_step_deps(repo_root: Path) -> DeepAgentDeps:
    return DeepAgentDeps(
        backend=LocalBackend(
            root_dir=str(repo_root),
            allowed_directories=[str(repo_root)],
        )
    )


def _step_prompt(
    pr_title: str,
    pr_body: str,
    step: dict,
    conflicted: list[str],
) -> str:
    listing = "\n".join(f"- `{p}`" for p in conflicted)
    short_sha = step["sha"][:8]
    return (
        "Resolve the conflict markers left by the current rebase step.\n\n"
        "## PR\n\n"
        f"**Title**: {pr_title}\n\n"
        f"{pr_body}\n\n"
        "## Commit being replayed\n\n"
        f"`{short_sha}` — {step['subject']}\n\n"
        "Original diff this commit is trying to apply:\n\n"
        "```diff\n"
        f"{step['diff']}\n"
        "```\n\n"
        "## Conflicted files\n\n"
        f"{listing}\n\n"
        "Edit each conflicted file so it contains the correct merged "
        "content, removing every `<<<<<<<`, `=======`, and `>>>>>>>` "
        "marker. Do not modify files outside the list above."
    )


def _has_conflict_markers(repo_root: Path, paths: list[str]) -> bool:
    """Return True if any listed file still contains marker lines.

    Reads file-by-file rather than running ``git diff --check`` so the
    check works whether or not the agent has staged its edits.
    """
    for rel in paths:
        full = repo_root / rel
        if not full.exists():
            continue
        text = full.read_text(errors="ignore")
        if any(m in text for m in ("<<<<<<<", "=======", ">>>>>>>")):
            return True
    return False


async def _run_resolve_step(repo_root: Path, prompt: str) -> None:
    await _resolve_step_agent().run(
        prompt,
        deps=_resolve_step_deps(repo_root),
        usage_limits=UsageLimits(request_limit=30),
    )


def _rebase_loop(workspace: PRWorkspace) -> tuple[bool, list[str]]:
    """Drive ``git rebase`` to completion via the resolve_step agent.

    Returns ``(ok, touched_files)`` where ``ok`` is True when the rebase
    finished cleanly (with the agent's help on conflicting steps) and
    ``touched_files`` is the union of files the agent had to resolve.
    On any failure (agent gives up, markers remain, unexpected git
    error) the rebase is aborted and ``ok`` is False.
    """
    fetch(workspace.repo_root, env={"GIT_TERMINAL_PROMPT": "0"})
    touched: list[str] = []
    try:
        finished = rebase_onto(
            workspace.repo_root, f"origin/{workspace.base_branch}"
        )
    except Exception:
        rebase_abort(workspace.repo_root)
        return False, touched

    while not finished:
        step = current_rebase_step(workspace.repo_root)
        conflicts = conflicted_paths(workspace.repo_root)
        if step is None or not conflicts:
            rebase_abort(workspace.repo_root)
            return False, touched

        prompt = _step_prompt(
            workspace.title, workspace.body, step, conflicts
        )
        try:
            asyncio.run(_run_resolve_step(workspace.repo_root, prompt))
        except Exception:
            rebase_abort(workspace.repo_root)
            return False, touched

        if _has_conflict_markers(workspace.repo_root, conflicts):
            rebase_abort(workspace.repo_root)
            return False, touched

        for path in conflicts:
            if path not in touched:
                touched.append(path)
        stage_all(workspace.repo_root)
        try:
            finished = rebase_continue(workspace.repo_root)
        except Exception:
            rebase_abort(workspace.repo_root)
            return False, touched

    return True, touched


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


def _conflict_body(base: str, head: str, conflicted: list[str]) -> str:
    """Synthetic implementation-plan body for the implement-agent fallback."""
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
        f"this branch. After resolving, the file must contain no "
        f"`<<<<<<<`, `=======`, or `>>>>>>>` sequences and the project's "
        f"tests must pass.\n\n"
        "### Files to change\n\n"
        f"{listing}\n"
    )


def _test_fix_body(
    base: str, head: str, touched: list[str], failure: str
) -> str:
    """Body used when the rebase succeeded but sanity tests fail."""
    listing = "\n".join(f"- `{p}`" for p in touched) if touched else "_(no per-step files recorded)_"
    return (
        f"# Fix tests after rebasing `{base}` into `{head}`\n\n"
        f"The PR was rebased onto `{base}` and conflicts were resolved "
        f"per-step. The sanity test pass that runs after the rebase failed "
        f"— investigate and patch the code so the tests pass.\n\n"
        f"## Files reconciled during the rebase\n\n{listing}\n\n"
        "## Test failure output\n\n"
        f"```\n{failure}\n```\n"
    )


def _fall_back_to_implement(
    bot: CaiBot,
    workspace: PRWorkspace,
    body: str,
) -> None:
    """Run the standard solve_graph from ``ImplementNode`` against ``workspace``."""
    workspace.body_path.write_text(body)
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
    solve_graph.run_sync(ImplementNode(), state=state)


def _merge_for_fallback(workspace: PRWorkspace) -> list[str]:
    """Merge ``origin/<base>`` and commit (markers and all) for fallback.

    The fallback path uses the same scaffolding cai-resolve-conflicts had
    before the rebase rewrite: stage everything, commit so the PR has a
    stable HEAD, then let ImplementNode rewrite the working tree on top.
    """
    fetch(workspace.repo_root, env={"GIT_TERMINAL_PROMPT": "0"})
    conflicts = merge_no_commit(
        workspace.repo_root,
        f"origin/{workspace.base_branch}",
        author_name=_MERGE_AUTHOR_NAME,
        author_email=_MERGE_AUTHOR_EMAIL,
    )
    stage_all(workspace.repo_root)
    msg = (
        f"merge: {workspace.base_branch} into {workspace.head_branch} "
        "(conflicts pending resolution)"
        if conflicts
        else f"merge: {workspace.base_branch} into {workspace.head_branch}"
    )
    commit(
        workspace.repo_root,
        msg,
        author_name=_MERGE_AUTHOR_NAME,
        author_email=_MERGE_AUTHOR_EMAIL,
    )
    return conflicts


def solve_conflicts(bot: CaiBot, workspace: PRWorkspace) -> dict:
    """Rebase the PR onto its base and resolve conflicts step-by-step.

    Returns a status dict whose ``mode`` is one of:

    * ``"clean"`` — rebase finished with no agent involvement.
    * ``"rebased"`` — rebase finished, agent resolved one or more steps,
      sanity tests pass, branch force-pushed.
    * ``"implement_fallback"`` — rebase or sanity step failed; the
      branch state was patched up via ``ImplementNode`` instead.
    """
    pr_ref = f"{workspace.repo}#{workspace.number}"
    with langfuse_workflow(
        "cai-resolve-conflicts",
        input={
            "pr": pr_ref,
            "base": workspace.base_branch,
            "head": workspace.head_branch,
        },
        metadata={"repo": workspace.repo, "pr_number": workspace.number},
        session_id=session_id_for_pr(workspace.number, workspace.head_branch),
    ):
        ok, touched = _rebase_loop(workspace)

        if not ok:
            # Make sure we're not stuck mid-rebase before falling back.
            if rebase_in_progress(workspace.repo_root):
                rebase_abort(workspace.repo_root)
            conflicted = _merge_for_fallback(workspace)
            _push(bot, workspace)
            if not conflicted:
                # Merge happened to be clean even though the rebase failed.
                return {"mode": "clean", "conflicted_files": []}
            body = _conflict_body(
                workspace.base_branch, workspace.head_branch, conflicted
            )
            _fall_back_to_implement(bot, workspace, body)
            return {
                "mode": "implement_fallback",
                "reason": "rebase_failed",
                "conflicted_files": conflicted,
            }

        if not touched:
            # No conflicts — base was already a strict ancestor or the
            # rebase was a no-op. Push so the branch is up to date.
            _push(bot, workspace)
            return {"mode": "clean", "conflicted_files": []}

        passed, details = _run_tests(workspace.repo_root)
        if not passed:
            body = _test_fix_body(
                workspace.base_branch,
                workspace.head_branch,
                touched,
                details,
            )
            _fall_back_to_implement(bot, workspace, body)
            return {
                "mode": "implement_fallback",
                "reason": "tests_failed",
                "conflicted_files": touched,
            }

        _push(bot, workspace)
        return {"mode": "rebased", "conflicted_files": touched}


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="cai-resolve-conflicts",
        description=(
            "Rebase a pull request onto its base branch with the "
            "resolve_step agent handling each conflicting step, run a "
            "sanity test pass, then force-push. Falls back to the "
            "implement agent if the rebase or tests fail. Prints a JSON "
            "summary on stdout."
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
