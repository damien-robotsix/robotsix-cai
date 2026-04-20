"""Handler for issues in IssueState.PR — bounces to the PR submachine
or recovers from an orphaned ``:pr-open`` label.

An issue is in ``:pr-open`` state while its linked PR is being processed
by the PR pipeline. The handler:

  1. Looks for an open PR with branch ``auto-improve/<issue_number>-*``
     and dispatches it (the normal happy path).
  2. If no open PR exists, scans recent closed PRs for the same branch:
     - Closed-merged but issue still at ``:pr-open`` → apply
       ``pr_to_merged`` so :func:`cai_lib.actions.confirm.handle_confirm`
       runs on the next tick.
     - Closed-unmerged: inspect *who* closed it via the issue timeline
       event. If a human (or bot account other than ours) closed it, the
       PR was rejected on purpose — divert to ``pr_to_human_needed`` so
       a human decides next steps. If the cai container itself closed
       it (matched on ``gh api user``'s login), apply ``pr_to_refined``
       so the issue re-flows through plan / implement.
  3. If no PR (open or recently closed) is found at all → apply
     ``pr_to_human_needed``. The label was applied without provenance
     and only a human can decide what to do.

Step (2) and (3) replace the old behavior of returning 0 silently, which
left the issue stuck at ``:pr-open`` forever and starved the dispatcher
loop guard.
"""
import subprocess
import sys
from typing import Optional

from cai_lib.config import REPO
from cai_lib.fsm import apply_transition
from cai_lib.github import _gh_json


_BRANCH_PREFIX_TEMPLATE = "auto-improve/{n}-"


def _our_gh_login() -> Optional[str]:
    """Return the authenticated GitHub login (the cai container's identity)."""
    try:
        out = _gh_json(["api", "user", "--jq", ".login"])
    except subprocess.CalledProcessError as e:
        print(
            f"[cai dispatch] gh api user failed (cannot determine our login):\n"
            f"{e.stderr}",
            file=sys.stderr,
        )
        return None
    if isinstance(out, str):
        return out.strip() or None
    if isinstance(out, dict):
        return (out.get("login") or "").strip() or None
    return None


def _pr_close_actor(pr_number: int) -> Optional[str]:
    """Return the GitHub login of whoever last closed PR #pr_number, or None.

    Walks the issue timeline newest-first and returns the actor of the
    most recent ``closed`` event. ``None`` means we couldn't determine —
    callers should treat that as "unknown actor".
    """
    try:
        events = _gh_json([
            "api",
            f"repos/{REPO}/issues/{pr_number}/timeline",
            "--paginate",
        ]) or []
    except subprocess.CalledProcessError as e:
        print(
            f"[cai dispatch] gh api timeline failed for PR #{pr_number}:\n"
            f"{e.stderr}",
            file=sys.stderr,
        )
        return None
    if not isinstance(events, list):
        return None
    closed_events = [e for e in events if (e.get("event") == "closed")]
    if not closed_events:
        return None
    latest = closed_events[-1]
    actor = latest.get("actor") or {}
    login = actor.get("login")
    return login or None


def _find_open_linked_pr(issue_number: int) -> dict | None:
    """Return the first open PR whose head branch starts with ``auto-improve/<N>-``."""
    prefix = _BRANCH_PREFIX_TEMPLATE.format(n=issue_number)
    try:
        prs = _gh_json([
            "pr", "list",
            "--repo", REPO,
            "--state", "open",
            "--json", "number,headRefName",
            "--limit", "100",
        ]) or []
    except subprocess.CalledProcessError as e:
        print(
            f"[cai dispatch] gh pr list (open) failed for issue #{issue_number}:\n"
            f"{e.stderr}",
            file=sys.stderr,
        )
        return None

    for pr in prs:
        if pr.get("headRefName", "").startswith(prefix):
            return pr
    return None


def _find_recent_closed_linked_pr(issue_number: int) -> dict | None:
    """Return the most recent closed PR whose branch matches ``auto-improve/<N>-``.

    Includes ``state`` and ``mergedAt`` so the caller can tell merged vs
    closed-unmerged apart.
    """
    prefix = _BRANCH_PREFIX_TEMPLATE.format(n=issue_number)
    try:
        prs = _gh_json([
            "pr", "list",
            "--repo", REPO,
            "--state", "closed",
            "--json", "number,headRefName,state,mergedAt,closedAt",
            "--limit", "200",
        ]) or []
    except subprocess.CalledProcessError as e:
        print(
            f"[cai dispatch] gh pr list (closed) failed for issue #{issue_number}:\n"
            f"{e.stderr}",
            file=sys.stderr,
        )
        return None

    matches = [pr for pr in prs if pr.get("headRefName", "").startswith(prefix)]
    if not matches:
        return None
    # Pick the most recent — by closedAt desc; mergedAt as fallback.
    matches.sort(
        key=lambda pr: pr.get("closedAt") or pr.get("mergedAt") or "",
        reverse=True,
    )
    return matches[0]


def _was_merged(pr: dict) -> bool:
    return bool(pr.get("mergedAt")) or pr.get("state") == "MERGED"


def handle_pr_bounce(issue: dict) -> int:
    """Bounce to the linked PR if open; otherwise recover the issue's state.

    See module docstring for the recovery decision tree.
    """
    from cai_lib.dispatcher import dispatch_pr  # local to avoid import cycle

    issue_number = issue["number"]
    label_names = [lb["name"] for lb in issue.get("labels", [])]

    # 1. Open PR? Bounce to it.
    open_pr = _find_open_linked_pr(issue_number)
    if open_pr is not None:
        return dispatch_pr(open_pr["number"])

    # 2. No open PR. Check recently closed PRs to choose a recovery path.
    closed_pr = _find_recent_closed_linked_pr(issue_number)
    if closed_pr is not None:
        if _was_merged(closed_pr):
            print(
                f"[cai dispatch] issue #{issue_number}: linked PR "
                f"#{closed_pr['number']} merged but issue still at :pr-open — "
                f"advancing pr_to_merged",
                flush=True,
            )
            ok = apply_transition(
                issue_number, "pr_to_merged",
                current_labels=label_names,
                log_prefix="cai dispatch",
            )
            return 0 if ok else 1

        # Closed unmerged — inspect who closed it. If it's us (the bot),
        # safely re-plan. If it's a human or another account, the close
        # was a deliberate decision and a human owns the next move.
        close_actor = _pr_close_actor(closed_pr["number"])
        our_login = _our_gh_login()
        bot_closed = (
            close_actor is not None
            and our_login is not None
            and close_actor == our_login
        )
        if bot_closed:
            print(
                f"[cai dispatch] issue #{issue_number}: linked PR "
                f"#{closed_pr['number']} closed unmerged by us "
                f"({close_actor}) — reverting pr_to_refined",
                flush=True,
            )
            ok = apply_transition(
                issue_number, "pr_to_refined",
                current_labels=label_names,
                log_prefix="cai dispatch",
            )
        else:
            actor_str = close_actor or "unknown"
            print(
                f"[cai dispatch] issue #{issue_number}: linked PR "
                f"#{closed_pr['number']} closed unmerged by {actor_str} "
                f"(our login: {our_login or 'unknown'}) — diverting "
                f"pr_to_human_needed",
                flush=True,
            )
            ok = apply_transition(
                issue_number, "pr_to_human_needed",
                current_labels=label_names,
                log_prefix="cai dispatch",
                divert_reason=(
                    f"Linked PR #{closed_pr['number']} was closed "
                    f"unmerged by `{actor_str}` (our login: "
                    f"`{our_login or 'unknown'}`). Because the closer "
                    f"is not this container, the close was a deliberate "
                    f"human decision — a human must decide the next "
                    f"move for this issue."
                ),
            )
        return 0 if ok else 1

    # 3. No PR found at all — orphaned :pr-open label, needs a human.
    print(
        f"[cai dispatch] issue #{issue_number}: no PR found (open or recently "
        f"closed) for branch auto-improve/{issue_number}-* — diverting "
        f"pr_to_human_needed",
        flush=True,
    )
    ok = apply_transition(
        issue_number, "pr_to_human_needed",
        current_labels=label_names,
        log_prefix="cai dispatch",
        divert_reason=(
            f"Issue was at `:pr-open` but no PR (open or recently "
            f"closed) could be found for branch "
            f"`auto-improve/{issue_number}-*`. The label was applied "
            f"without provenance — a human must decide whether to "
            f"reopen a PR or revert the issue to a pre-PR state."
        ),
    )
    return 0 if ok else 1
