"""Idempotent label management for cai-managed repos.

Used by the auto-improve pipeline to gate which issues cai may pick up.
``ensure_labels`` is create-only: existing labels are left untouched so a
re-run never overwrites a color or description that someone tweaked in
the GitHub UI.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from .bot import CaiBot

LabelStatus = Literal["created", "exists"]


@dataclass(frozen=True)
class LabelSpec:
    name: str
    color: str  # 6-char hex, no leading '#'
    description: str = ""


def ensure_labels(
    bot: CaiBot, repo: str, specs: list[LabelSpec]
) -> dict[str, LabelStatus]:
    """Create any of ``specs`` missing from ``repo``. Existing labels untouched."""
    repo_obj = bot.repo(repo)
    existing = {label.name for label in repo_obj.get_labels()}
    result: dict[str, LabelStatus] = {}
    for spec in specs:
        if spec.name in existing:
            result[spec.name] = "exists"
            continue
        repo_obj.create_label(spec.name, spec.color, spec.description)
        result[spec.name] = "created"
    return result


def set_label(bot: CaiBot, repo: str, number: int, label: str, present: bool) -> None:
    """Idempotently add or remove ``label`` from issue/PR ``number``.

    PRs share the issues API for labels, so this works for both. No-op when
    the label is already in the desired state.
    """
    issue = bot.repo(repo).get_issue(number)
    current = {lbl.name for lbl in issue.labels}
    if present and label not in current:
        issue.add_to_labels(label)
    elif not present and label in current:
        issue.remove_from_labels(label)
