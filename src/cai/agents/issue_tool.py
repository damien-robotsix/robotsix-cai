"""``raise_issue`` — tool for pro agents to create GitHub issues when blocked.

When a pro agent encounters a blocker it cannot resolve on its own, it
calls this tool to file a GitHub issue with the details. The tool
builds an ``IssueMeta``, writes a temporary JSON+MD pair, and delegates
to ``push()`` / ``CaiBot`` to create the issue on GitHub. It returns a
confirmation string with the new issue number and URL so the agent can
reference it in follow-up work.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

from pydantic_ai import RunContext, Tool

from cai.github.bot import CaiBot
from cai.github.issues import IssueMeta, push


_AGENT_RAISED_LABEL = "cai:agent-raised"


async def raise_issue(
    ctx: RunContext,
    repo: str,
    title: str,
    body: str,
    labels: list[str] | None = None,
) -> str:
    """Create a GitHub issue when the agent encounters a blocker.

    Args:
        repo: Full repository name (e.g. ``owner/repo``).
        title: Issue title.
        body: Issue body (markdown).
        labels: Labels to apply. Defaults to ``["cai:human-review"]``.

    The ``cai:agent-raised`` label is always added to the final label
    set so issue provenance is grep-able from the GitHub UI/CLI.

    Returns:
        Confirmation string with the new issue number and URL.
    """
    bot = CaiBot()
    final_labels = list(labels) if labels else ["cai:human-review"]
    if _AGENT_RAISED_LABEL not in final_labels:
        final_labels.append(_AGENT_RAISED_LABEL)
    meta = IssueMeta(
        repo=repo,
        title=title,
        labels=final_labels,
    )

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        json_path = tmp / "issue.json"
        md_path = tmp / "issue.md"
        json_path.write_text(meta.model_dump_json(indent=2) + "\n")
        md_path.write_text(body)

        issue = push(bot, json_path)

    return (
        f"Issue created: #{issue.number} — {issue.title}\n"
        f"{issue.html_url}"
    )


RAISE_ISSUE_TOOL = Tool(raise_issue)
