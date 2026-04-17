"""GitHub API helpers for cai action wrappers."""

import json
import subprocess
import sys

from datetime import datetime, timezone

from cai_lib.config import REPO, LABEL_PR_NEEDS_HUMAN
from cai_lib.github import _gh_json
from cai_lib.subprocess_utils import _run


# IMPORTANT: only "no-action" / "summary" bot comments belong here.
# Comments that contain ACTIONABLE content for the revise subagent
# (most notably review-pr findings) must NOT be in this list — they
# need to flow through to the unaddressed set so revise can act on
# them. The "## cai pre-merge review (clean)" form is filtered (no
# findings → nothing for revise to do). The plain "## cai pre-merge
# review" form is NOT filtered because it carries `### Finding:`
# blocks that revise should address.
_BOT_COMMENT_MARKERS = (
    "## Implement subagent:",
    "## Fix subagent:",  # compat: pre-rename bot comments
    "## Revise subagent:",
    "## Revision summary",
    "## CI-fix subagent:",
    "## cai pre-merge review (clean)",
    "## cai docs review (clean)",
    "## cai docs review (applied)",
    "## cai merge verdict",
)


# Duplicates of module-level markers in cai.py. Kept in sync with the
# cai.py definitions; these copies exist so cmd_helpers is importable
# without a circular dependency on cai.py.
_NO_ADDITIONAL_CHANGES_MARKER = "## Revise subagent: no additional changes"
_REBASE_FAILED_MARKER = "## Revise subagent: rebase resolution failed"


def _gh_user_identity() -> tuple[str, str]:
    """Resolve the gh-token owner's git name and email."""
    user = _gh_json(["api", "user"])
    name = user.get("name") or user["login"]
    email = user.get("email") or f"{user['id']}+{user['login']}@users.noreply.github.com"
    return name, email


def _is_bot_comment(comment: dict) -> bool:
    """Return True if a comment body looks like it was posted by a cai subagent."""
    body = (comment.get("body") or "").lstrip()
    return any(body.startswith(m) for m in _BOT_COMMENT_MARKERS)


def _parse_iso_ts(value):
    """Parse an ISO-8601 UTC timestamp ('2026-04-10T00:23:34Z'), return datetime or None."""
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def _fetch_review_comments(pr_number: int) -> list[dict]:
    """Fetch line-by-line review comments for a PR, normalized to issue-comment shape.

    `gh pr view --json comments` only returns issue-level comments. Line-
    by-line review comments (left on specific lines in the diff) live on
    a separate REST endpoint. This helper fetches them via `gh api` and
    reshapes each one to match the issue-comment format used by the rest
    of the revise logic: `{author: {login}, createdAt, body}`.

    The body is prefixed with a `(line comment on path:line)` marker so
    the subagent knows where the comment is anchored in the diff.
    """
    try:
        result = _run(
            ["gh", "api", f"repos/{REPO}/pulls/{pr_number}/comments"],
            capture_output=True,
        )
        if result.returncode != 0:
            return []
        raw = json.loads(result.stdout)
    except (json.JSONDecodeError, ValueError):
        return []

    normalized = []
    for c in raw:
        author_login = c.get("user", {}).get("login", "")
        created_at = c.get("created_at", "")
        body = c.get("body", "")
        path = c.get("path", "")
        line_num = c.get("line") or c.get("original_line")
        if path and line_num:
            body = f"(line comment on `{path}:{line_num}`)\n\n{body}"
        elif path:
            body = f"(line comment on `{path}`)\n\n{body}"
        normalized.append({
            "author": {"login": author_login},
            "createdAt": created_at,
            "body": body,
        })
    return normalized


def _pr_set_needs_human(pr_number: int, needs: bool) -> None:
    """Add or remove the `needs-human-review` label on a PR.

    Idempotent: gh silently no-ops if the label is already in the
    requested state. Logged but not fatal on failure — labelling is a
    UX nicety, not a correctness requirement.
    """
    flag = "--add-label" if needs else "--remove-label"
    res = _run(
        ["gh", "pr", "edit", str(pr_number),
         "--repo", REPO, flag, LABEL_PR_NEEDS_HUMAN],
        capture_output=True,
    )
    if res.returncode != 0:
        action = "add" if needs else "remove"
        print(
            f"[cai merge] PR #{pr_number}: could not {action} "
            f"label `{LABEL_PR_NEEDS_HUMAN}`:\n{res.stderr}",
            file=sys.stderr,
        )
