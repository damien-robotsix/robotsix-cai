"""Per-thread loop for ``cai-address``.

For each unresolved review thread (that wasn't authored by ``cai[bot]``):

1. Run the address agent against the live PR checkout.
2. If it chose ``fix`` and the working tree changed, commit on the PR branch.
3. Post the agent's reply on the thread.
4. If a fix landed, resolve the thread.

After the loop, if any commit was made, push the branch once.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Literal

from git import Repo
from pydantic import BaseModel, Field
from pydantic_ai.exceptions import ModelRetry
from pydantic_ai.usage import UsageLimits

from cai.agents.loader import AGENT_DIR, build_deep_agent, parse_agent_md
from cai.git import commit, push_branch, stage_all
from cai.github.bot import CaiBot
from cai.github.pr import (
    ReviewThread,
    list_resolved_threads,
    list_unresolved_threads,
    reply_to_review_comment,
    resolve_review_thread,
)
from cai.github.repo import PRWorkspace
from cai.log import langfuse_workflow
from cai.workflows._deps import repo_deps

AGENT_DEFINITION = AGENT_DIR / "address.md"


class AddressDecision(BaseModel):
    action: Literal["fix", "reply_only"] = Field(
        description="Whether the agent edited code (fix) or only wants to reply (reply_only)."
    )
    reply: str = Field(description="Message to post as a reply on the thread.")
    commit_message: str | None = Field(
        default=None,
        description="Imperative-mood commit subject. Required when action=fix.",
    )


@dataclass
class ThreadResult:
    thread_id: str
    path: str
    action: Literal["fix", "reply_only", "skipped"]
    committed: bool
    resolved: bool


@lru_cache(maxsize=1)
def _address_agent():
    config, instructions = parse_agent_md(AGENT_DEFINITION)
    agent = build_deep_agent(config, instructions, output_type=AddressDecision)

    @agent.output_validator
    async def _fix_must_edit(ctx, decision: AddressDecision) -> AddressDecision:
        # Without this guard the agent can claim ``action="fix"`` and post a
        # confident reply on the thread without ever calling Write/Edit. The
        # caller would then post the misleading reply but commit nothing.
        if decision.action != "fix":
            return decision
        repo_root = Path(ctx.deps.backend.root_dir)
        if not Repo(str(repo_root)).is_dirty(
            index=True, working_tree=True, untracked_files=True
        ):
            raise ModelRetry(
                "You returned action='fix' but the working tree has no changes "
                "— no edits were made on disk. Either invoke write_file or "
                "hashline_edit to actually make the change you described, or "
                "return action='reply_only' if a code change is not warranted."
            )
        return decision

    return agent


def _format_thread_prompt(
    thread: ReviewThread,
    workspace: PRWorkspace,
    prior_corrections: list[ReviewThread],
) -> str:
    """Render the thread as a prompt the agent can act on.

    The PR title/body frame the surrounding goal. The "Prior corrections"
    section lists already-resolved threads on the same PR so the agent
    doesn't undo earlier fixes — without this, an agent reading the
    (potentially stale) PR body could re-add code that a prior thread
    explicitly removed.
    """
    line = thread.line if thread.line is not None else "(line unknown)"
    history = "\n\n".join(
        f"**{c.author}** ({c.created_at}):\n{c.body}" for c in thread.comments
    )

    sections = [
        "Address this PR review thread.\n",
        (
            "## Pull request (background, may be stale)\n\n"
            f"**{workspace.repo}#{workspace.number}** — {workspace.title}\n\n"
            "The body below is the original plan. Prior review threads "
            "and commits on this branch may have corrected it; the "
            "current state of the code and the *Prior corrections* "
            "section are authoritative over the plan.\n\n"
            f"{workspace.body}"
        ),
    ]
    if prior_corrections:
        rendered = "\n\n".join(_render_resolved_thread(t) for t in prior_corrections)
        sections.append(
            "## Prior corrections (resolved threads on this PR)\n\n"
            "Do not undo these — the fixes already landed.\n\n" + rendered
        )
    sections.append(f"## File\n\n`{thread.path}` at line {line}")
    sections.append(f"## Diff hunk\n\n```diff\n{thread.diff_hunk}\n```")
    sections.append(f"## Conversation\n\n{history}")
    sections.append(
        "Decide whether to fix the code or reply only, edit files if you fix, "
        "and return your decision."
    )
    return "\n\n".join(sections)


def _render_resolved_thread(thread: ReviewThread) -> str:
    head = thread.comments[0]
    bot_replies = [c for c in thread.comments[1:] if c.author == "cai[bot]"]
    summary = f"- `{thread.path}` — **{head.author}**: {head.body.strip()}"
    if bot_replies:
        last = bot_replies[-1]
        summary += f"\n  - **{last.author}** (resolved): {last.body.strip()}"
    return summary


def _has_staged_changes(repo_root: Path) -> bool:
    repo = Repo(str(repo_root))
    return repo.is_dirty(index=True, working_tree=False, untracked_files=False)


async def _process_thread(
    thread: ReviewThread,
    workspace: PRWorkspace,
    bot: CaiBot,
    prior_corrections: list[ReviewThread],
) -> ThreadResult:
    prompt = _format_thread_prompt(thread, workspace, prior_corrections)
    result = await _address_agent().run(
        prompt,
        deps=repo_deps(workspace.repo_root, write_dirs=[workspace.repo_root]),
        usage_limits=UsageLimits(request_limit=50),
    )
    decision: AddressDecision = result.output

    committed = False
    if decision.action == "fix":
        if not decision.commit_message:
            raise ValueError(
                f"agent returned action=fix without commit_message for thread {thread.id}"
            )
        stage_all(workspace.repo_root)
        if _has_staged_changes(workspace.repo_root):
            commit(
                workspace.repo_root,
                decision.commit_message,
                author_name="cai-bot",
                author_email="cai-bot@users.noreply.github.com",
            )
            committed = True

    reply_to_review_comment(
        bot, workspace.repo, workspace.number, thread.first_comment_id, decision.reply
    )

    resolved = False
    if decision.action == "fix" and committed:
        resolve_review_thread(bot, workspace.repo, thread.id)
        resolved = True

    return ThreadResult(
        thread_id=thread.id,
        path=thread.path,
        action=decision.action,
        committed=committed,
        resolved=resolved,
    )


async def _run(bot: CaiBot, workspace: PRWorkspace) -> list[ThreadResult]:
    threads = list_unresolved_threads(bot, workspace.repo, workspace.number)
    prior_corrections = list_resolved_threads(bot, workspace.repo, workspace.number)
    results: list[ThreadResult] = []
    any_committed = False
    for thread in threads:
        result = await _process_thread(thread, workspace, bot, prior_corrections)
        results.append(result)
        any_committed = any_committed or result.committed

    if any_committed:
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
    return results


def address_pr(bot: CaiBot, workspace: PRWorkspace) -> list[ThreadResult]:
    """Run the per-thread loop synchronously, wrapped in a langfuse trace."""
    pr_ref = f"{workspace.repo}#{workspace.number}"
    with langfuse_workflow(
        "cai-address",
        input={"pr": pr_ref, "branch": workspace.head_branch},
        metadata={"repo": workspace.repo, "pr_number": workspace.number},
    ):
        return asyncio.run(_run(bot, workspace))
