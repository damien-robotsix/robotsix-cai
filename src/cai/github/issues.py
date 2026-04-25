"""Round-trip GitHub issues to/from local JSON+MD pairs.

Each issue is two sibling files: ``<n>.json`` for metadata, ``<n>.md`` for
the body. ``pull`` writes both; ``push`` reads them back and applies the
change. New issues are created when the JSON has no ``number``; the assigned
number is then written back so a subsequent push updates rather than creates.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from github.GithubObject import NotSet
from github.Issue import Issue
from github.Repository import Repository
from pydantic import BaseModel, Field

from .bot import CaiBot


class IssueMeta(BaseModel):
    repo: str
    number: int | None = None
    title: str
    state: Literal["open", "closed"] = "open"
    state_reason: Literal["completed", "not_planned", "reopened"] | None = None
    labels: list[str] = Field(default_factory=list)
    assignees: list[str] = Field(default_factory=list)
    milestone: str | None = None


def _meta_paths(directory: Path, number: int) -> tuple[Path, Path]:
    return directory / f"{number}.json", directory / f"{number}.md"


def _write_meta(path: Path, meta: IssueMeta) -> None:
    path.write_text(json.dumps(meta.model_dump(), indent=2) + "\n")


def _resolve_milestone(repo: Repository, title: str | None):
    if title is None:
        return None
    for ms in repo.get_milestones(state="all"):
        if ms.title == title:
            return ms
    raise ValueError(f"milestone {title!r} not found in {repo.full_name}")


def pull(bot: CaiBot, repo: str, number: int, directory: Path) -> tuple[Path, Path]:
    """Fetch issue ``number`` from ``repo`` into ``<directory>/<n>.{json,md}``."""
    issue = bot.repo(repo).get_issue(number)
    meta = IssueMeta(
        repo=repo,
        number=number,
        title=issue.title,
        state=issue.state,
        state_reason=getattr(issue, "state_reason", None),
        labels=[label.name for label in issue.labels],
        assignees=[user.login for user in issue.assignees],
        milestone=issue.milestone.title if issue.milestone else None,
    )
    directory.mkdir(parents=True, exist_ok=True)
    json_path, md_path = _meta_paths(directory, number)
    _write_meta(json_path, meta)
    md_path.write_text(issue.body or "")
    return json_path, md_path


def push(bot: CaiBot, json_path: Path) -> Issue:
    """Apply the issue described by ``json_path`` and its sibling ``.md``.

    Creates if ``number`` is null, updates otherwise. On creation, the new
    number is written back to ``json_path`` in place.
    """
    json_path = Path(json_path)
    meta = IssueMeta.model_validate_json(json_path.read_text())
    md_path = json_path.with_suffix(".md")
    if not md_path.exists():
        raise FileNotFoundError(f"missing issue body file: {md_path}")
    body = md_path.read_text()

    repo_obj = bot.repo(meta.repo)
    milestone = _resolve_milestone(repo_obj, meta.milestone)
    state_reason = meta.state_reason if meta.state_reason else NotSet

    if meta.number is None:
        kwargs: dict = {"title": meta.title, "body": body}
        if meta.labels:
            kwargs["labels"] = meta.labels
        if meta.assignees:
            kwargs["assignees"] = meta.assignees
        if milestone is not None:
            kwargs["milestone"] = milestone
        issue = repo_obj.create_issue(**kwargs)
        meta.number = issue.number
        _write_meta(json_path, meta)
        if meta.state == "closed":
            issue.edit(state="closed", state_reason=state_reason)
        return issue

    issue = repo_obj.get_issue(meta.number)
    issue.edit(
        title=meta.title,
        body=body,
        state=meta.state,
        labels=meta.labels,
        assignees=meta.assignees,
        # None clears milestone; NotSet would mean "don't change".
        milestone=milestone,
        state_reason=state_reason,
    )
    return issue
