"""``cai-resolve-conflicts`` CLI: rebase a PR onto its base branch and let
the resolve_step agent resolve conflicts step-by-step.

The flow is implemented as a ``pydantic_graph.Graph``:

* ``RebaseLoopNode`` — rebases onto ``origin/<base>``.
  When the rebase stops at a conflict, hands the current commit's diff
  plus the conflicted files to the ``resolve_step`` agent and continues.
* ``SanityTestNode`` — runs the test suite once the rebase finishes
  (skipped when the rebase needed no agent involvement). On failure the
  node hands off to ``solve_graph`` entered at ``ImplementNode`` — the
  same recovery path ``cai-solve`` uses when a sanity-test pass fails
  mid-run — so the implement agent can fix the rebased tree and PRNode
  force-pushes the result. Up to two implement retries before giving up.
* ``PushNode`` — force-pushes the head branch so the PR's diff view shows
  head-vs-base only, without a noisy merge commit.
* ``ObsoleteNode`` — taken when the rebase consumes every commit (because
  each one's change was already on base).  The PR is closed with a comment
  and labelled ``cai:obsolete``; the branch is **not** force-pushed,
  preserving the original commits behind the closed PR.

If the rebase loop itself fails, the workflow raises immediately with a
clear error rather than silently falling back to a doomed implement loop.

Prints a JSON summary on stdout.
"""
from __future__ import annotations

import asyncio
import json
import sys
import traceback
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from pydantic_ai.exceptions import UsageLimitExceeded
from pydantic_ai.usage import UsageLimits
from pydantic_deep import DeepAgentDeps, LocalBackend
from pydantic_graph import BaseNode, End, Graph, GraphRunContext

from cai.agents.loader import build_deep_agent, parse_agent_md, resolve_agent_path
from cai.git import (
    conflicted_paths,
    current_rebase_step,
    index_matches_head,
    push_branch,
    rebase_abort,
    rebase_continue,
    rebase_in_progress,
    rebase_onto,
    rebase_skip,
    rev_parse,
    stage_all,
)
from cai.github.bot import CaiBot
from cai.github.issues import IssueMeta
from cai.github.labels import LabelSpec, ensure_labels, set_label
from cai.github.repo import (
    PRWorkspace,
    is_pull_request,
    parse_ref_and_bot,
    prepare_pr_workspace,
)
from cai.log import langfuse_workflow
from cai.workflows.state import IssueState, ResolveStepOutput
from cai.workflows.test_runner import _run_tests


@lru_cache(maxsize=1)
def _resolve_step_agent():
    config, instructions = parse_agent_md(resolve_agent_path("resolve_step"))
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
    """Drive the resolve_step agent with the shared retry policy.

    Routing through ``traced_agent_run`` gets us the same
    transient-HTTP soft-retry as the rest of the workflows
    (404/402/429/5xx) plus the existing UsageLimitExceeded bump.
    """
    from cai.log.observability import traced_agent_run

    try:
        await traced_agent_run(
            "resolve_step",
            _resolve_step_agent(),
            prompt,
            deps=_resolve_step_deps(repo_root),
            usage_limits=UsageLimits(request_limit=60),
        )
    except UsageLimitExceeded:
        # traced_agent_run only bumps the limit *once*; if a single
        # bump still wasn't enough, give resolve_step one more shot
        # at 90 requests before giving up.
        await traced_agent_run(
            "resolve_step",
            _resolve_step_agent(),
            prompt,
            deps=_resolve_step_deps(repo_root),
            usage_limits=UsageLimits(request_limit=90),
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

        # rebase_continue returned False — distinguish "cherry-pick is now
        # empty" (every staged change was already on the new parent) from
        # other commit-time failures (pre-commit hook, signing, …) that
        # also leave the rebase paused but with a non-empty staged tree.
        # Only ``--skip`` in the genuinely-empty case; otherwise abort and
        # surface the failure rather than silently dropping the commit.
        if not finished and not conflicted_paths(workspace.repo_root):
            if not index_matches_head(workspace.repo_root):
                print(
                    f"[rebase-loop] step {step_n}: rebase paused with staged "
                    "changes but no merge conflicts — likely a commit hook or "
                    "signing failure, not an empty cherry-pick. Aborting "
                    "instead of skipping the commit.",
                    file=sys.stderr,
                )
                rebase_abort(workspace.repo_root)
                return False, touched
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


@dataclass
class ConflictsState:
    bot: CaiBot
    workspace: PRWorkspace
    touched: list[str] = field(default_factory=list)
    mode: str = "clean"


class RebaseLoopNode(BaseNode[ConflictsState, None, dict]):
    """Rebase the PR onto its base branch.

    The resolve_step agent is invoked for each conflicting commit until
    the rebase finishes cleanly. On any failure the rebase is aborted and
    a ``RuntimeError`` is raised so the caller sees a clear failure
    rather than a silent fallback.
    """

    async def run(
        self, ctx: GraphRunContext[ConflictsState]
    ) -> "SanityTestNode | PushNode | ObsoleteNode":
        ws = ctx.state.workspace
        # _rebase_loop is sync because it owns its own asyncio.run() to keep
        # all resolve_step agent calls in one event loop. Bridge across the
        # graph's running loop with to_thread so that loop is left untouched.
        ok, touched = await asyncio.to_thread(_rebase_loop, ws)
        if not ok:
            if rebase_in_progress(ws.repo_root):
                rebase_abort(ws.repo_root)
            raise RuntimeError(
                f"Rebase of {ws.repo}#{ws.number} onto {ws.base_branch!r} failed. "
                "The resolve_step agent could not clear all conflict markers. "
                "Manual intervention required."
            )
        # Every commit was empty after rebase: all PR changes are already on
        # base.  Force-pushing now would point the head branch at base's tip
        # and GitHub would auto-close the PR with an empty diff, throwing away
        # the original commits.  Route to ObsoleteNode instead.
        head_sha = rev_parse(ws.repo_root, "HEAD")
        base_sha = rev_parse(ws.repo_root, f"origin/{ws.base_branch}")
        if head_sha == base_sha:
            ctx.state.mode = "obsolete"
            return ObsoleteNode()
        ctx.state.touched = touched
        if not touched:
            ctx.state.mode = "clean"
            return PushNode()
        return SanityTestNode()


class SanityTestNode(BaseNode[ConflictsState, None, dict]):
    """Run the sanity test pass after the agent resolved at least one step.

    On failure, hand off to ``solve_graph`` entered at ``ImplementNode``
    (the same recovery path ``cai-solve`` uses for sanity-test failures
    mid-run): the implement agent receives the test output, fixes the
    rebased tree, ``TestSanityNode`` reruns up to two more times, and
    ``PRNode`` force-pushes the rewritten head. Our own ``PushNode`` is
    bypassed in that case since solve's ``PRNode`` already pushed.
    """

    async def run(self, ctx: GraphRunContext[ConflictsState]) -> "PushNode | End[dict]":
        # Delayed import to avoid the ``conflicts → fsm → (transitively) …``
        # chain at module import time; the cycle isn't real today but the
        # late binding keeps it that way.
        from cai.workflows.fsm import solve_graph
        from cai.workflows.implement import ImplementNode

        ws = ctx.state.workspace
        passed, details = _run_tests(ws.repo_root)
        if passed:
            ctx.state.mode = "rebased"
            return PushNode()

        ctx.state.mode = "rebased+fixed"
        meta = IssueMeta(repo=ws.repo, number=ws.number, title=ws.title)
        impl_state = IssueState(
            bot=ctx.state.bot,
            meta=meta,
            body_path=ws.body_path.resolve(),
            repo_root=ws.repo_root.resolve(),
            branch_name=ws.head_branch,
            pr_number=ws.number,
            test_failure_details=details,
        )
        impl_state.new_meta = meta
        await solve_graph.run(ImplementNode(), state=impl_state)
        return End(
            {"mode": ctx.state.mode, "conflicted_files": ctx.state.touched}
        )


class PushNode(BaseNode[ConflictsState, None, dict]):
    """Force-push the head branch and emit the JSON summary."""

    async def run(self, ctx: GraphRunContext[ConflictsState]) -> End[dict]:
        _push(ctx.state.bot, ctx.state.workspace)
        return End(
            {"mode": ctx.state.mode, "conflicted_files": ctx.state.touched}
        )


class ObsoleteNode(BaseNode[ConflictsState, None, dict]):
    """Close the PR cleanly when its changes are already on base.

    No force-push: that would reset the head branch to base's tip and GitHub
    would auto-close the PR with an empty diff.  Instead, post a comment
    explaining the state, label ``cai:obsolete``, and close the PR while
    keeping the original commits referenced by the closed PR's history.
    """

    async def run(self, ctx: GraphRunContext[ConflictsState]) -> End[dict]:
        bot = ctx.state.bot
        ws = ctx.state.workspace
        ensure_labels(
            bot,
            ws.repo,
            [
                LabelSpec(
                    name="cai:obsolete",
                    color="cccccc",
                    description="PR changes already landed on base; nothing to merge",
                ),
            ],
        )
        issue = bot.repo(ws.repo).get_issue(ws.number)
        issue.create_comment(
            f"Closing as obsolete: every commit on `{ws.head_branch}` is "
            f"already present on `{ws.base_branch}` after rebase, so this "
            "PR has nothing left to merge."
        )
        set_label(bot, ws.repo, ws.number, "cai:obsolete", present=True)
        issue.edit(state="closed")
        return End({"mode": "obsolete", "conflicted_files": []})


conflicts_graph: Graph[ConflictsState, None, dict] = Graph(
    nodes=[RebaseLoopNode, SanityTestNode, PushNode, ObsoleteNode]
)


def solve_conflicts(bot: CaiBot, workspace: PRWorkspace) -> dict:
    """Rebase the PR onto its base and resolve conflicts step-by-step.

    Returns a status dict whose ``mode`` is one of:

    * ``"clean"`` — rebase finished with no agent involvement.
    * ``"rebased"`` — rebase finished, agent resolved one or more steps,
      sanity tests pass, branch force-pushed.
    * ``"rebased+fixed"`` — rebase finished but sanity tests failed; the
      implement agent then fixed the tree (via ``solve_graph``), and the
      rewritten head was pushed by ``PRNode``.
    * ``"obsolete"`` — every commit was already on base after rebase; PR
      closed with a comment, branch left untouched.

    Raises ``RuntimeError`` only when the rebase loop itself fails (e.g.
    resolve_step could not clear all markers). Sanity-test failures are
    routed through the implement agent rather than aborting.
    """
    pr_ref = f"{workspace.repo}#{workspace.number}"
    set_label(bot, workspace.repo, workspace.number, "cai:human-review", present=False)
    state = ConflictsState(bot=bot, workspace=workspace)

    from cai.workflows.registry import by_slug, CliArgs
    cli_args = CliArgs(repo=workspace.repo, number=workspace.number, branch=workspace.head_branch)
    _conflicts_sid = by_slug("conflicts").session_id(cli_args)
    async def _drive() -> dict:
        with langfuse_workflow(
            "cai-resolve-conflicts",
            input={
                "pr": pr_ref,
                "base": workspace.base_branch,
                "head": workspace.head_branch,
            },
            metadata={"repo": workspace.repo, "pr_number": workspace.number},
            session_id=_conflicts_sid,
        ):
            result = await conflicts_graph.run(RebaseLoopNode(), state=state)
        if result.output.get("mode") != "obsolete":
            set_label(
                bot, workspace.repo, workspace.number, "cai:human-review", present=True
            )
        return result.output

    return asyncio.run(_drive())


def main() -> None:
    bot, repo, number = parse_ref_and_bot(
        "cai-resolve-conflicts",
        "Rebase a pull request onto its base branch with the "
        "resolve_step agent handling each conflicting step, run a "
        "sanity test pass, then force-push. If the sanity tests fail, "
        "hand off to the implement agent (via solve_graph) to fix the "
        "rebased tree before pushing. Prints a JSON summary on stdout.",
        ref_help="PR reference, formatted as owner/repo#number.",
    )
    if not is_pull_request(bot, repo, number):
        print(f"cai-resolve-conflicts: error: {repo}#{number} is not a pull request", file=sys.stderr)
        sys.exit(2)

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
