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
from pydantic_ai.usage import UsageLimits
from pydantic_deep import DeepAgentDeps, LocalBackend

from cai.agents.loader import AGENT_DIR, build_deep_agent, parse_agent_md
from cai.git import commit, push_branch, stage_all
from cai.github.bot import CaiBot
from cai.github.pr import (
    ReviewThread,
    list_unresolved_threads,
    reply_to_review_comment,
    resolve_review_thread,
)
from cai.github.repo import PRWorkspace
from cai.log import langfuse_workflow

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
    return build_deep_agent(config, instructions, output_type=AddressDecision)


def _deps(repo_root: Path) -> DeepAgentDeps:
    return DeepAgentDeps(
        backend=LocalBackend(
            root_dir=str(repo_root),
            allowed_directories=[str(repo_root)],
        )
    )


def _format_thread_prompt(thread: ReviewThread) -> str:
    """Render the thread as a prompt the agent can act on."""
    line = thread.line if thread.line is not None else "(line unknown)"
    history = "\n\n".join(
        f"**{c.author}** ({c.created_at}):\n{c.body}" for c in thread.comments
    )
    return (
        "Address this PR review thread.\n\n"
        f"## File\n\n`{thread.path}` at line {line}\n\n"
        f"## Diff hunk\n\n```diff\n{thread.diff_hunk}\n```\n\n"
        f"## Conversation\n\n{history}\n\n"
        "Decide whether to fix the code or reply only, edit files if you fix, "
        "and return your decision."
    )


def _has_staged_changes(repo_root: Path) -> bool:
    repo = Repo(str(repo_root))
    return repo.is_dirty(index=True, working_tree=False, untracked_files=False)


async def _process_thread(
    thread: ReviewThread,
    repo_root: Path,
    bot: CaiBot,
    repo: str,
    pr_number: int,
) -> ThreadResult:
    prompt = _format_thread_prompt(thread)
    result = await _address_agent().run(
        prompt,
        deps=_deps(repo_root),
        usage_limits=UsageLimits(request_limit=50),
    )
    decision: AddressDecision = result.output

    committed = False
    if decision.action == "fix":
        if not decision.commit_message:
            raise ValueError(
                f"agent returned action=fix without commit_message for thread {thread.id}"
            )
        stage_all(repo_root)
        if _has_staged_changes(repo_root):
            commit(
                repo_root,
                decision.commit_message,
                author_name="cai-bot",
                author_email="cai-bot@users.noreply.github.com",
            )
            committed = True

    reply_to_review_comment(bot, repo, pr_number, thread.first_comment_id, decision.reply)

    resolved = False
    if decision.action == "fix" and committed:
        resolve_review_thread(bot, repo, thread.id)
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
    results: list[ThreadResult] = []
    any_committed = False
    for thread in threads:
        result = await _process_thread(
            thread, workspace.repo_root, bot, workspace.repo, workspace.number
        )
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
