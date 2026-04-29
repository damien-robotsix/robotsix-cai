"""``cai-resolve-conflicts`` CLI: rebase a PR onto its base branch and let
the resolve_step agent resolve conflicts step-by-step.

The flow is:

1. Prepare the PR workspace (clones the head branch).
2. Fetch ``origin`` and ``git rebase origin/<base>``.
3. While the rebase stops at a conflict, hand the current commit's diff
   plus the conflicted files to the ``resolve_step`` agent, then
   ``git rebase --continue``.
4. Run a sanity test pass once the rebase finishes.
5. Push ``--force`` so the PR's diff view shows head-vs-base only,
   without a noisy merge commit.

If the rebase or sanity tests fail, the workflow raises immediately with
a clear error so the failure is visible rather than silently falling back
to a doomed implement loop.

Prints a JSON summary on stdout.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import traceback
from functools import lru_cache
from pathlib import Path

from pydantic_ai.usage import UsageLimits
from pydantic_deep import DeepAgentDeps, LocalBackend

from cai.agents.loader import AGENT_DIR, build_deep_agent, parse_agent_md
from cai.git import (
    conflicted_paths,
    current_rebase_step,
    fetch,
    push_branch,
    rebase_abort,
    rebase_continue,
    rebase_in_progress,
    rebase_onto,
    rebase_skip,
    stage_all,
)
from cai.github.bot import CaiBot
from cai.github.repo import (
    PRWorkspace,
    is_pull_request,
    parse_pr_ref,
    prepare_pr_workspace,
)
from cai.log import langfuse_workflow, session_id_for_pr
from cai.workflows.state import ResolveStepOutput
from cai.workflows.test_runner import _run_tests

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
    """Return True if any listed file still contains unresolved conflict blocks.

    Uses the same parser as conflict_list (<<<<<<< / ======= / >>>>>>> triples)
    so orphaned marker lines from nested conflicts — left over when a previous
    failed rebase was committed — don't cause false positives.
    """
    from cai.agents.conflict_tools import _parse_conflicts
    for rel in paths:
        full = repo_root / rel
        if not full.exists():
            continue
        lines = full.read_text(errors="ignore").splitlines(keepends=True)
        if _parse_conflicts(lines):
            return True
    return False


def _strip_orphaned_markers(repo_root: Path, paths: list[str]) -> None:
    """Remove marker lines that sit outside any conflict block.

    When a previous failed rebase was committed, the branch can contain
    nested conflict markers.  After the agent resolves all proper blocks,
    orphaned ``=======`` / ``>>>>>>>`` lines that were outside those blocks
    remain.  Strip them so the committed content is clean.
    """
    for rel in paths:
        full = repo_root / rel
        if not full.exists():
            continue
        lines = full.read_text(errors="ignore").splitlines(keepends=True)
        clean = [
            l for l in lines
            if not (
                l.startswith("<<<<<<<")
                or l.startswith("=======")
                or l.startswith(">>>>>>>")
            )
        ]
        if len(clean) != len(lines):
            full.write_text("".join(clean))


async def _run_resolve_step(repo_root: Path, prompt: str) -> None:
    await _resolve_step_agent().run(
        prompt,
        deps=_resolve_step_deps(repo_root),
        usage_limits=UsageLimits(request_limit=30),
    )


async def _rebase_loop_async(workspace: PRWorkspace) -> tuple[bool, list[str]]:
    """Drive ``git rebase`` to completion via the resolve_step agent.

    Returns ``(ok, touched_files)`` where ``ok`` is True when the rebase
    finished cleanly (with the agent's help on conflicting steps) and
    ``touched_files`` is the union of files the agent had to resolve.
    On any failure (agent gives up, markers remain, unexpected git
    error) the rebase is aborted and ``ok`` is False.

    Runs in a single event loop so repeated agent calls within the same
    rebase session share one asyncio context.  (Calling ``asyncio.run()``
    once per rebase step creates a new event loop each time, which can
    disrupt background tasks started by pydantic-ai or its OTel integration
    and cause silent failures on the second or later step.)
    """
    fetch(workspace.repo_root, env={"GIT_TERMINAL_PROMPT": "0"})
    touched: list[str] = []
    try:
        finished = rebase_onto(
            workspace.repo_root, f"origin/{workspace.base_branch}"
        )
    except Exception:
        print("[rebase-loop] rebase_onto failed:", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        rebase_abort(workspace.repo_root)
        return False, touched

    step_n = 0
    while not finished:
        step_n += 1
        step = current_rebase_step(workspace.repo_root)
        conflicts = conflicted_paths(workspace.repo_root)
        print(
            f"[rebase-loop] step {step_n}: sha={step['sha'][:8] if step else None} "
            f"conflicts={conflicts}",
            file=sys.stderr,
        )
        if step is None or not conflicts:
            print(
                f"[rebase-loop] step {step_n}: aborting — step={step is not None} conflicts={conflicts}",
                file=sys.stderr,
            )
            rebase_abort(workspace.repo_root)
            return False, touched

        prompt = _step_prompt(
            workspace.title, workspace.body, step, conflicts
        )
        try:
            await _run_resolve_step(workspace.repo_root, prompt)
        except Exception:
            print(f"[rebase-loop] step {step_n}: resolve_step agent failed:", file=sys.stderr)
            traceback.print_exc(file=sys.stderr)
            rebase_abort(workspace.repo_root)
            return False, touched

        _strip_orphaned_markers(workspace.repo_root, conflicts)

        if _has_conflict_markers(workspace.repo_root, conflicts):
            print(
                f"[rebase-loop] step {step_n}: markers remain after agent in {conflicts}",
                file=sys.stderr,
            )
            rebase_abort(workspace.repo_root)
            return False, touched

        for path in conflicts:
            if path not in touched:
                touched.append(path)
        stage_all(workspace.repo_root)
        try:
            finished = rebase_continue(workspace.repo_root)
            print(f"[rebase-loop] step {step_n}: rebase_continue → finished={finished}", file=sys.stderr)
        except Exception:
            print(f"[rebase-loop] step {step_n}: rebase_continue raised:", file=sys.stderr)
            traceback.print_exc(file=sys.stderr)
            rebase_abort(workspace.repo_root)
            return False, touched

        # rebase_continue returned False but no conflicted paths means the
        # commit became empty after resolution (its change was already in
        # main).  Skip the empty commit so the rebase can advance.
        if not finished and not conflicted_paths(workspace.repo_root):
            print(
                f"[rebase-loop] step {step_n}: empty commit after resolution, skipping",
                file=sys.stderr,
            )
            try:
                finished = rebase_skip(workspace.repo_root)
                print(f"[rebase-loop] step {step_n}: rebase_skip → finished={finished}", file=sys.stderr)
            except Exception:
                print(f"[rebase-loop] step {step_n}: rebase_skip raised:", file=sys.stderr)
                traceback.print_exc(file=sys.stderr)
                rebase_abort(workspace.repo_root)
                return False, touched

    return True, touched


def _rebase_loop(workspace: PRWorkspace) -> tuple[bool, list[str]]:
    """Sync entry point — runs the async rebase loop in a single event loop."""
    return asyncio.run(_rebase_loop_async(workspace))


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
    """Rebase the PR onto its base and resolve conflicts step-by-step.

    Returns a status dict whose ``mode`` is one of:

    * ``"clean"`` — rebase finished with no agent involvement.
    * ``"rebased"`` — rebase finished, agent resolved one or more steps,
      sanity tests pass, branch force-pushed.

    Raises ``RuntimeError`` if the rebase loop fails (e.g. resolve_step
    could not clear all markers) or if sanity tests fail after the rebase.
    The caller sees a clear failure rather than a silent fallback to a
    doomed implement loop.
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
            if rebase_in_progress(workspace.repo_root):
                rebase_abort(workspace.repo_root)
            raise RuntimeError(
                f"Rebase of {pr_ref} onto {workspace.base_branch!r} failed. "
                "The resolve_step agent could not clear all conflict markers. "
                "Manual intervention required."
            )

        if not touched:
            _push(bot, workspace)
            return {"mode": "clean", "conflicted_files": []}

        passed, details = _run_tests(workspace.repo_root)
        if not passed:
            raise RuntimeError(
                f"Rebase of {pr_ref} onto {workspace.base_branch!r} succeeded "
                "but the sanity test pass failed. Fix the tests before retrying.\n\n"
                f"Test output:\n{details}"
            )

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
