"""cai_lib.issues — GitHub issue and sub-issue helpers.

Provides a clean API for creating issues via the REST API and managing
native sub-issue relationships (link, list, check completion).  Low-level
``gh`` invocation is delegated to the shared helpers in ``github.py`` and
``subprocess_utils.py``.

Note — staged migration:
    This module is the **infrastructure layer** for migrating from the
    convention-based parent/child tracking system (HTML-comment markers,
    manual checklists) to GitHub's native sub-issues API. Migration is
    occurring across multiple follow-up issues:

    * ``cai_lib/actions/refine.py`` — replace ``gh issue create`` + HTML
      comments + ``_update_parent_checklist()`` with :func:`create_issue`
      and :func:`link_sub_issue`.
    * ``cai_lib/actions/confirm.py`` — ✓ replaced ``<!-- parent: #N -->``
      regex lookup with title parsing (via :func:`_parse_sub_issue_step` from
      dispatcher); still needs to use :func:`list_sub_issues` to verify
      closure via native API instead of manual checklist parsing.
    * ``cai.py`` — replace the checklist-based completion check
      (~line 940–971) with :func:`all_sub_issues_closed`.

    The helper functions below are still awaiting integration into the above
    callers.
"""

import json
import sys

from cai_lib.config import REPO
from cai_lib.github import _gh_json
from cai_lib.subprocess_utils import _run


def create_issue(title: str, body: str, labels: list[str]) -> dict | None:
    """Create an issue via the REST API and return its metadata.

    Uses ``gh api POST`` instead of ``gh issue create`` so the internal
    ``id`` (needed by :func:`link_sub_issue`) is available in the same
    response without an extra round-trip.

    Returns a dict with at least ``number``, ``id``, and ``html_url``
    on success, or ``None`` on failure.
    """
    payload = json.dumps({"title": title, "body": body, "labels": labels})
    result = _run(
        ["gh", "api", "--method", "POST",
         f"repos/{REPO}/issues",
         "--input", "-"],
        input=payload,
        capture_output=True,
    )
    if result.returncode != 0:
        print(
            f"[cai] failed to create issue '{title}': {result.stderr}",
            file=sys.stderr,
        )
        return None
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return None


def link_sub_issue(parent_number: int, child_id: int) -> bool:
    """Link a child issue to a parent using GitHub's native sub-issues API.

    *child_id* is the REST-internal ``id`` (not the issue ``number``);
    use the ``id`` field from :func:`create_issue`'s return value.

    Returns True on success, False on failure.
    """
    import subprocess  # local — only needed for the except clause

    try:
        _gh_json([
            "api", "--method", "POST",
            f"repos/{REPO}/issues/{parent_number}/sub_issues",
            "-F", f"sub_issue_id={child_id}",
        ])
        return True
    except subprocess.CalledProcessError as exc:
        print(
            f"[cai] failed to link child id={child_id} "
            f"to parent #{parent_number}: {exc.stderr}",
            file=sys.stderr,
        )
        return False


def list_sub_issues(parent_number: int) -> list[dict]:
    """Return native sub-issues for *parent_number*.

    Each entry is a dict with at least ``number``, ``title``, ``state``,
    and ``id``.  Returns an empty list on failure or if there are none.
    """
    import subprocess  # local — only needed for the except clause

    try:
        result = _gh_json([
            "api", "--paginate",
            f"repos/{REPO}/issues/{parent_number}/sub_issues",
        ])
        return result if isinstance(result, list) else []
    except (subprocess.CalledProcessError, TypeError):
        return []


def all_sub_issues_closed(parent_number: int) -> bool | None:
    """Check whether every native sub-issue of *parent_number* is closed.

    Returns True if all are closed, False if any are open, or None if
    the parent has no native sub-issues.
    """
    subs = list_sub_issues(parent_number)
    if not subs:
        return None
    return all(si.get("state") == "closed" for si in subs)
