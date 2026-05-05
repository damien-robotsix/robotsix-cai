"""``raise_ticket`` — file work as a draft Project (v2) item.

Replaces the older ``raise_issue`` tool. Pro agents call this when they
encounter a finding that should be triaged/solved later: instead of
opening a GitHub issue, the work is filed as a draft on the configured
``robotsix-cai`` Project with a ``Type`` (``code-change`` / ``analysis``)
and a ``Status`` (``Backlog`` for triage, ``Ready`` to auto-trigger
solve).

Falls back to creating a regular GitHub issue when the project
integration is not configured — keeps existing dev environments working
until they wire up ``PROJECT_OWNER`` / ``PROJECT_NUMBER`` in ``app.env``.
"""
from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Literal

from pydantic_ai import RunContext, Tool

from cai.github.bot import CaiBot
from cai.github.issues import IssueMeta, push
from cai.github.projects import create_draft_ticket, is_enabled

_AGENT_RAISED_LABEL = "cai:agent-raised"


async def raise_ticket(
    ctx: RunContext,
    title: str,
    body: str,
    type: Literal["code-change", "analysis"],
    status: Literal["Backlog", "Ready"] = "Backlog",
    repo: str | None = None,
) -> str:
    """File a finding as a draft Project ticket.

    Args:
        title: Ticket title.
        body: Ticket body (markdown).
        type: ``code-change`` for work that needs a PR, ``analysis`` for
              audit/classification work that ends in a comment.
        status: ``Backlog`` (default — needs human triage) or ``Ready``
                (auto-trigger solve on the next polling cycle).
        repo: Fallback target if the project integration is not
              configured. Defaults to ``PROJECT_DEFAULT_REPO``.

    Returns:
        Confirmation string with the ticket ID (or issue number, on the
        legacy fallback path).
    """
    bot = CaiBot()

    if is_enabled(bot):
        item_id = create_draft_ticket(
            bot, title=title, body=body, type=type, status=status
        )
        return (
            f"Ticket created: {item_id} (Type={type}, Status={status})\n"
            f"On project {bot.project_owner}/{bot.project_number}"
        )

    # Fallback: no project configured. File a regular issue with a label
    # encoding the type so a later migration can backfill tickets.
    target_repo = repo or bot.project_default_repo
    if not target_repo:
        raise RuntimeError(
            "raise_ticket: project integration disabled and no fallback repo "
            "(set PROJECT_DEFAULT_REPO or pass repo=...)"
        )
    labels = [_AGENT_RAISED_LABEL, f"cai:type:{type}"]
    if status == "Ready":
        labels.append("cai:raised")
    else:
        labels.append("cai:human-review")
    meta = IssueMeta(repo=target_repo, title=title, labels=labels)
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        json_path = tmp / "issue.json"
        md_path = tmp / "issue.md"
        json_path.write_text(meta.model_dump_json(indent=2) + "\n")
        md_path.write_text(body)
        issue = push(bot, json_path)
    return (
        f"Ticket fallback (project not configured): issue #{issue.number} "
        f"in {target_repo} — {issue.title}\n{issue.html_url}"
    )


RAISE_TICKET_TOOL = Tool(raise_ticket)
