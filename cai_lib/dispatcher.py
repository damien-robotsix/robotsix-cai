"""FSM dispatcher — single entry point that routes issues and PRs to the
handler registered for their current state.

State IS the program counter: when an issue is labelled ``:refining``, the
refine handler runs; if that run crashes, the next tick picks up the same
``:refining`` issue and runs the same handler. Resume is free because
every handler is written to be safely re-enterable.

Public API:

- :func:`dispatch_issue` — given an issue number, fetch it, look up its
  state, call the matching handler.
- :func:`dispatch_pr` — same for PRs.
- :func:`dispatch_oldest_actionable` — list every open issue and PR in a
  state with a handler; pick the oldest (by ``createdAt``) and dispatch
  it. Used by ``cai cycle``.

The registries (``ISSUE_STATE_ACTIONS`` / ``PR_STATE_ACTIONS``) live here
rather than in :mod:`cai_lib.fsm` so handlers can ``import *`` FSM helpers
without creating an import cycle.
"""
from __future__ import annotations

import subprocess
import sys
from typing import Callable, Optional

from cai_lib.config import REPO
from cai_lib.fsm import (
    IssueState, PRState,
    get_issue_state, get_pr_state,
)
from cai_lib.github import _gh_json


# ---------------------------------------------------------------------------
# Handler registries
# ---------------------------------------------------------------------------
#
# Some states share a handler: e.g. both RAISED and TRIAGING route to
# ``handle_triage`` — the handler itself decides whether it's a fresh
# entry (apply the raise_to_triaging transition first) or a resume
# (skip the entry transition).
#
# States with no handler (SOLVED, HUMAN_NEEDED, PR_HUMAN_NEEDED, MERGED
# on the PR side) are terminal or parked and the dispatcher returns
# without doing anything.

IssueHandler = Callable[[dict], int]
PRHandler    = Callable[[dict], int]


def _build_issue_registry() -> dict[IssueState, IssueHandler]:
    # Deferred imports — handlers import from cai_lib.fsm, so registering
    # them at module load would create a cycle.
    from cai_lib.actions.triage    import handle_triage
    from cai_lib.actions.refine    import handle_refine
    from cai_lib.actions.explore   import handle_explore
    from cai_lib.actions.plan      import handle_plan, handle_plan_gate
    from cai_lib.actions.implement import handle_implement
    from cai_lib.actions.confirm   import handle_confirm
    from cai_lib.actions.pr_bounce import handle_pr_bounce

    return {
        IssueState.RAISED:            handle_triage,
        IssueState.TRIAGING:          handle_triage,      # resume
        IssueState.REFINING:          handle_refine,
        IssueState.NEEDS_EXPLORATION: handle_explore,
        IssueState.REFINED:           handle_plan,
        IssueState.PLANNING:          handle_plan,        # resume
        IssueState.PLANNED:           handle_plan_gate,
        IssueState.PLAN_APPROVED:     handle_implement,
        IssueState.IN_PROGRESS:       handle_implement,   # resume
        IssueState.PR:                handle_pr_bounce,
        IssueState.MERGED:            handle_confirm,
        # SOLVED, HUMAN_NEEDED → no handler
    }


def _build_pr_registry() -> dict[PRState, PRHandler]:
    from cai_lib.actions.open_pr     import handle_open_to_review
    from cai_lib.actions.review_pr   import handle_review_pr
    from cai_lib.actions.revise      import handle_revise
    from cai_lib.actions.review_docs import handle_review_docs
    from cai_lib.actions.fix_ci      import handle_fix_ci
    from cai_lib.actions.merge       import handle_merge

    return {
        PRState.OPEN:             handle_open_to_review,
        PRState.REVIEWING_CODE:   handle_review_pr,
        PRState.REVISION_PENDING: handle_revise,
        PRState.REVIEWING_DOCS:   handle_review_docs,
        PRState.APPROVED:         handle_merge,
        PRState.CI_FAILING:       handle_fix_ci,
        # MERGED, PR_HUMAN_NEEDED → no handler
    }


# Lazily built on first use to keep module import cheap and cycle-free.
_ISSUE_REGISTRY: Optional[dict[IssueState, IssueHandler]] = None
_PR_REGISTRY:    Optional[dict[PRState, PRHandler]]       = None


def _issue_registry() -> dict[IssueState, IssueHandler]:
    global _ISSUE_REGISTRY
    if _ISSUE_REGISTRY is None:
        _ISSUE_REGISTRY = _build_issue_registry()
    return _ISSUE_REGISTRY


def _pr_registry() -> dict[PRState, PRHandler]:
    global _PR_REGISTRY
    if _PR_REGISTRY is None:
        _PR_REGISTRY = _build_pr_registry()
    return _PR_REGISTRY


def actionable_issue_states() -> set[IssueState]:
    """Set of IssueStates that have a registered handler."""
    return set(_issue_registry().keys())


def actionable_pr_states() -> set[PRState]:
    """Set of PRStates that have a registered handler."""
    return set(_pr_registry().keys())


# ---------------------------------------------------------------------------
# Dispatch entry points
# ---------------------------------------------------------------------------

def dispatch_issue(issue_number: int) -> int:
    """Dispatch a single issue by number.

    Fetches the issue, derives state from labels, looks up the handler.
    Returns 0 if the issue is terminal / has no handler; otherwise the
    handler's exit code.
    """
    try:
        issue = _gh_json([
            "issue", "view", str(issue_number),
            "--repo", REPO,
            "--json", "number,title,body,labels,createdAt,comments,state",
        ])
    except subprocess.CalledProcessError as e:
        print(
            f"[cai dispatch] gh issue view #{issue_number} failed:\n{e.stderr}",
            file=sys.stderr,
        )
        return 1

    if issue.get("state") == "CLOSED":
        print(f"[cai dispatch] issue #{issue_number} is closed; nothing to dispatch",
              flush=True)
        return 0

    label_names = [lb["name"] for lb in issue.get("labels", [])]
    state = get_issue_state(label_names)
    if state is None:
        print(f"[cai dispatch] issue #{issue_number} has no FSM state label; "
              "nothing to dispatch", flush=True)
        return 0

    handler = _issue_registry().get(state)
    if handler is None:
        print(f"[cai dispatch] issue #{issue_number} at {state.name} — "
              "no handler (terminal / parked)", flush=True)
        return 0

    print(f"[cai dispatch] issue #{issue_number} at {state.name} → {handler.__name__}",
          flush=True)
    return handler(issue)


def dispatch_pr(pr_number: int) -> int:
    """Dispatch a single PR by number.

    Fetches the PR, derives state from labels + merged flag, runs handler.
    """
    try:
        pr = _gh_json([
            "pr", "view", str(pr_number),
            "--repo", REPO,
            "--json",
            "number,title,headRefName,headRefOid,labels,state,mergeable,"
            "merged,mergedAt,comments,reviews",
        ])
    except subprocess.CalledProcessError as e:
        print(
            f"[cai dispatch] gh pr view #{pr_number} failed:\n{e.stderr}",
            file=sys.stderr,
        )
        return 1

    state = get_pr_state(pr)
    handler = _pr_registry().get(state)
    if handler is None:
        print(f"[cai dispatch] PR #{pr_number} at {state.name} — "
              "no handler (terminal / parked)", flush=True)
        return 0

    print(f"[cai dispatch] PR #{pr_number} at {state.name} → {handler.__name__}",
          flush=True)
    return handler(pr)


def dispatch_oldest_actionable() -> int:
    """List every open issue and PR in a state that has a registered handler;
    pick the oldest (by ``createdAt``) and dispatch it.

    Returns the handler's exit code, or 0 if the queue is empty.
    """
    issue_states = actionable_issue_states()
    pr_states = actionable_pr_states()

    # Fetch all open auto-improve issues with their current labels.
    try:
        issues = _gh_json([
            "issue", "list",
            "--repo", REPO,
            "--label", "auto-improve",
            "--state", "open",
            "--json", "number,createdAt,labels",
            "--limit", "200",
        ]) or []
    except subprocess.CalledProcessError as e:
        print(f"[cai dispatch] gh issue list failed:\n{e.stderr}", file=sys.stderr)
        issues = []

    try:
        prs = _gh_json([
            "pr", "list",
            "--repo", REPO,
            "--state", "open",
            "--base", "main",
            "--json", "number,createdAt,labels,merged,mergedAt",
            "--limit", "100",
        ]) or []
    except subprocess.CalledProcessError as e:
        print(f"[cai dispatch] gh pr list failed:\n{e.stderr}", file=sys.stderr)
        prs = []

    candidates: list[tuple[str, str, int]] = []  # (createdAt, kind, number)

    for issue in issues:
        label_names = [lb["name"] for lb in issue.get("labels", [])]
        state = get_issue_state(label_names)
        if state is not None and state in issue_states:
            candidates.append((issue.get("createdAt", ""), "issue", issue["number"]))

    for pr in prs:
        state = get_pr_state(pr)
        if state in pr_states:
            candidates.append((pr.get("createdAt", ""), "pr", pr["number"]))

    if not candidates:
        print("[cai dispatch] no actionable issues or PRs", flush=True)
        return 0

    candidates.sort(key=lambda c: c[0])  # oldest first
    _, kind, number = candidates[0]
    if kind == "issue":
        return dispatch_issue(number)
    return dispatch_pr(number)
