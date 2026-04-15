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
    from cai_lib.actions.rebase      import handle_rebase

    return {
        PRState.OPEN:             handle_open_to_review,
        PRState.REVIEWING_CODE:   handle_review_pr,
        PRState.REVISION_PENDING: handle_revise,
        PRState.REVIEWING_DOCS:   handle_review_docs,
        PRState.APPROVED:         handle_merge,
        PRState.REBASING:         handle_rebase,
        PRState.CI_FAILING:       handle_fix_ci,
        # MERGED, PR_HUMAN_NEEDED → no handler
    }


# Pre-merge PR states from which the dispatcher can divert into REBASING
# when it sees ``mergeable == "CONFLICTING"`` / ``mergeStateStatus == "DIRTY"``.
# Maps each from-state to the canonical ``*_to_rebasing`` transition name.
# REBASING itself, MERGED, PR_HUMAN_NEEDED, and OPEN are intentionally
# excluded — REBASING is already running it; OPEN doesn't have a label
# so apply_pr_transition wouldn't have one to remove.
_REBASE_ENTRY_TRANSITIONS: dict[PRState, str] = {
    PRState.REVIEWING_CODE:   "reviewing_code_to_rebasing",
    PRState.REVISION_PENDING: "revision_pending_to_rebasing",
    PRState.REVIEWING_DOCS:   "reviewing_docs_to_rebasing",
    PRState.APPROVED:         "approved_to_rebasing",
    PRState.CI_FAILING:       "ci_failing_to_rebasing",
}


def _pr_needs_rebase(pr: dict) -> bool:
    """True when the PR has merge conflicts with main."""
    return (
        pr.get("mergeable") == "CONFLICTING"
        or pr.get("mergeStateStatus") == "DIRTY"
    )


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

    Fetches the PR, derives state from labels. If the PR is mergeable
    against main, runs the registered handler for its state. If a
    rebase against main is needed, applies the matching ``*_to_rebasing``
    transition first and routes to ``handle_rebase`` regardless of the
    pipeline label — the rebase handler always exits to REVIEWING_CODE
    so the next tick re-reviews the rebased SHA.
    """
    try:
        pr = _gh_json([
            "pr", "view", str(pr_number),
            "--repo", REPO,
            "--json",
            "number,title,headRefName,headRefOid,labels,state,mergeable,"
            "mergeStateStatus,mergedAt,comments,reviews",
        ])
    except subprocess.CalledProcessError as e:
        print(
            f"[cai dispatch] gh pr view #{pr_number} failed:\n{e.stderr}",
            file=sys.stderr,
        )
        return 1

    state = get_pr_state(pr)

    # Conflict override: divert to REBASING regardless of pipeline label.
    if state in _REBASE_ENTRY_TRANSITIONS and _pr_needs_rebase(pr):
        from cai_lib.fsm import apply_pr_transition
        from cai_lib.actions.rebase import handle_rebase
        entry = _REBASE_ENTRY_TRANSITIONS[state]
        print(f"[cai dispatch] PR #{pr_number} at {state.name} has "
              f"mergeable={pr.get('mergeable')} / "
              f"mergeStateStatus={pr.get('mergeStateStatus')} → "
              f"{entry} → handle_rebase", flush=True)
        apply_pr_transition(pr_number, entry, current_pr=pr,
                            log_prefix="cai dispatch")
        return handle_rebase(pr)

    handler = _pr_registry().get(state)
    if handler is None:
        print(f"[cai dispatch] PR #{pr_number} at {state.name} — "
              "no handler (terminal / parked)", flush=True)
        return 0

    print(f"[cai dispatch] PR #{pr_number} at {state.name} → {handler.__name__}",
          flush=True)
    return handler(pr)


def _pick_oldest_actionable_target(
    skip: Optional[set[tuple[str, int]]] = None,
) -> Optional[tuple[str, int]]:
    """Return ``(kind, number)`` of the oldest open issue/PR in a state with
    a registered handler, or ``None`` if the queue is empty.

    ``kind`` is ``"issue"`` or ``"pr"``. Sort key is the GitHub
    ``createdAt`` timestamp (oldest first), so PRs that have been around
    longer get the next tick — keeps in-flight work ahead of fresh intake.

    ``skip`` is an optional set of ``(kind, number)`` tuples to exclude — used
    by :func:`dispatch_drain` to move past a target whose handler already
    failed in the current drain pass so the rest of the queue can still run.
    """
    skip = skip or set()
    issue_states = actionable_issue_states()
    pr_states = actionable_pr_states()

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
            "--json", "number,createdAt,labels,mergedAt",
            "--limit", "100",
        ]) or []
    except subprocess.CalledProcessError as e:
        print(f"[cai dispatch] gh pr list failed:\n{e.stderr}", file=sys.stderr)
        prs = []

    candidates: list[tuple[str, str, int]] = []

    for issue in issues:
        label_names = [lb["name"] for lb in issue.get("labels", [])]
        state = get_issue_state(label_names)
        if state is not None and state in issue_states:
            if ("issue", issue["number"]) in skip:
                continue
            candidates.append((issue.get("createdAt", ""), "issue", issue["number"]))

    for pr in prs:
        state = get_pr_state(pr)
        if state in pr_states:
            if ("pr", pr["number"]) in skip:
                continue
            candidates.append((pr.get("createdAt", ""), "pr", pr["number"]))

    if not candidates:
        return None

    candidates.sort(key=lambda c: c[0])
    _, kind, number = candidates[0]
    return (kind, number)


# Default cap on how many handlers a single drain pass will run. With
# per-target dedup (each ``(kind, number)`` dispatches at most once per
# drain) this cap bounds the work pass to ``max_iter`` distinct targets;
# the cron tick interval (CAI_CYCLE_SCHEDULE) is the wall-clock rate
# limit; ``dispatched_targets`` skipping is the per-drain loop backstop.
_DEFAULT_DRAIN_MAX_ITER = 50


def dispatch_drain(max_iter: int = _DEFAULT_DRAIN_MAX_ITER) -> int:
    """Drain the actionable queue: pick oldest, dispatch, repeat.

    Each ``(kind, number)`` is dispatched **at most once per drain pass**.
    After a dispatch (success, nonzero, or crash), the target is added
    to a per-drain skip set so the picker moves on to the next oldest.
    This kills the class of loop where a routing handler (``pr_bounce``)
    or an idempotent no-op handler keeps returning 0 on a target whose
    underlying state never changes (e.g. PR parked at ``PR_HUMAN_NEEDED``,
    merge handler short-circuiting on "already evaluated at this SHA").

    Stops when one of:
      - the queue is empty (no actionable issues or PRs left),
      - ``max_iter`` iterations have run (hard upper bound; the cycle's
        flock prevents overlap).

    A side effect of per-drain dedup: when a handler legitimately advances
    state and the same item is now actionable in a *new* state (e.g.
    implement advancing PLAN_APPROVED → PR), the follow-up handler fires
    on the next cron tick rather than in the same drain pass. The cron
    interval is the wall-clock rate limit anyway, so this is just a
    one-tick deferral — cheap insurance against the no-op-loop class of
    bugs.

    Returns the worst exit code seen across handlers (0 if every dispatch
    succeeded or the queue was empty from the start).
    """
    import traceback

    dispatched_targets: set[tuple[str, int]] = set()
    worst_rc = 0

    for i in range(max_iter):
        target = _pick_oldest_actionable_target(skip=dispatched_targets)
        if target is None:
            print(f"[cai dispatch] drain complete after {i} dispatch(es): "
                  "queue empty", flush=True)
            return worst_rc

        kind, number = target
        try:
            if kind == "issue":
                rc = dispatch_issue(number)
            else:
                rc = dispatch_pr(number)
        except Exception:  # noqa: BLE001 — keep draining the queue
            # Handler crashed. Record dispatch, skip this target for the
            # rest of this drain pass (#657 — one crashing item must not
            # stall the cycle), and continue with the next actionable target.
            traceback.print_exc()
            print(f"[cai dispatch] handler for {kind} #{number} raised; "
                  "skipping this target for the remainder of the drain",
                  flush=True)
            dispatched_targets.add(target)
            worst_rc = max(worst_rc, 1)
            continue
        # Every dispatch — success or failure — counts. No target runs
        # twice in the same drain.
        dispatched_targets.add(target)
        if rc > worst_rc:
            worst_rc = rc

    print(f"[cai dispatch] hit drain cap (max_iter={max_iter}); remaining "
          "actionable items will run on the next cycle tick", flush=True)
    return worst_rc


# Back-compat alias: dispatcher tests + cmd_dispatch still call
# `dispatch_oldest_actionable`. Now drains.
def dispatch_oldest_actionable() -> int:
    """Alias for :func:`dispatch_drain` — drains the actionable queue."""
    return dispatch_drain()
