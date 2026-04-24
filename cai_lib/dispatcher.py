"""FSM dispatcher — single entry point that routes issues and PRs through
flat inline driver pipelines.

State IS the program counter: when an issue is labelled ``:refining``, the
refine handler runs; if that run crashes, the next tick picks up the same
``:refining`` issue and runs the same handler. Resume is free because
every handler is written to be safely re-enterable.

Public API:

- :func:`dispatch_issue` — given an issue number, fetch it, derive its
  state, hand off to :func:`drive_issue` (or to the specialised MERGED /
  HUMAN_NEEDED handler).
- :func:`dispatch_pr` — same for PRs, via :func:`drive_pr`.
- :func:`dispatch_drain` — list every open issue and PR in a handled
  state; pick the oldest (by ``createdAt``) and drive it end-to-end.
  Used by ``cai cycle``.

``drive_issue`` / ``drive_pr`` are the single-step drivers: each call
fires the Pattern A entry transition inline (if any) before invoking
the handler for the current state. The outer loop in
:func:`_drive_target_to_completion` re-fetches state and loops for
multi-step walks.

Registries (``_build_issue_registry`` / ``_build_pr_registry``)
collapse to the minimum: the common entry state routes to the driver,
and MERGED / HUMAN_NEEDED (issue) and PR_HUMAN_NEEDED (PR) keep
specialised handlers. Every other actionable state falls through to
the driver via the actionable-state fallback in :func:`dispatch_issue`
/ :func:`dispatch_pr`.
"""
from __future__ import annotations

import re
import subprocess
import sys
from typing import Callable, NamedTuple, Optional, Union

from cai_lib.config import LABEL_HUMAN_SOLVED, LABEL_PARENT, REPO
from cai_lib.fsm import (
    Confidence,
    IssueState, PRState,
    get_issue_state, get_pr_state,
)
from cai_lib.github import (
    _gh_json,
    _acquire_remote_lock,
    _release_remote_lock,
    _set_labels,
    _set_pr_labels,
    blocking_issue_numbers,
    open_blockers,
)
from cai_lib.issues import list_sub_issues
from cai_lib.subagent import set_current_fsm_state


# ---------------------------------------------------------------------------
# Structured handler return shape (issue #1124 infrastructure)
# ---------------------------------------------------------------------------

class HandlerResult(NamedTuple):
    """Structured return for FSM handlers.

    Fields:
      * ``trigger`` — FSM transition name to fire via
        :func:`cai_lib.fsm_transitions.fire_trigger`. The empty string
        is a no-op sentinel: :func:`_driver_fire` applies any
        ``artifacts["extra_add"]`` / ``artifacts["extra_remove"]``
        labels inline via ``_set_labels`` / ``_set_pr_labels`` and
        skips the FSM call entirely.
      * ``confidence`` — forwarded to ``fire_trigger`` as
        ``confidence=`` (``None`` for ungated transitions).
      * ``divert_reason`` — forwarded to ``fire_trigger`` as
        ``divert_reason=`` (empty string when ``None``).
      * ``artifacts`` — freeform bag. Keys recognised by
        :func:`_driver_fire`: ``extra_remove`` (tuple of label names
        forwarded to ``fire_trigger``), ``extra_add`` (used only by
        the empty-trigger sentinel), ``reason_extra`` (forwarded to
        ``fire_trigger`` as ``reason_extra=`` when present).
      * ``stop_driving`` — set by a handler to signal the inner drive
        loop in :func:`_drive_target_to_completion` that it should
        stop driving this target within the same tick (even if the
        state changed).
    """
    trigger: str
    confidence: Optional[Confidence] = None
    divert_reason: Optional[str] = None
    artifacts: Optional[dict] = None
    stop_driving: bool = False


def _driver_fire(
    number: int,
    result: "HandlerResult",
    *,
    is_pr: bool,
    current_labels: Optional[list[str]] = None,
    current_pr: Optional[dict] = None,
    log_prefix: str = "cai dispatch",
) -> tuple[bool, bool]:
    """Translate a :class:`HandlerResult` into a :func:`fire_trigger` call.

    Empty-string ``trigger`` is the no-op sentinel: apply the
    ``artifacts["extra_add"]`` / ``artifacts["extra_remove"]`` labels
    inline via ``_set_labels`` (``is_pr=False``) or ``_set_pr_labels``
    (``is_pr=True``), skipping ``fire_trigger`` entirely. When both
    lists are empty the function returns ``(True, False)`` without any
    gh call. On a successful label call returns ``(True, False)``; on
    failure returns ``(False, False)``.

    Non-empty ``trigger`` routes through :func:`fire_trigger` and
    retries once on ``ok is False`` — mirrors the
    ``for _attempt in range(2)`` double-retry pattern in
    ``_park_in_progress_at_human_needed``
    (``cai_lib/actions/implement.py``). Returns the ``(ok, diverted)``
    tuple produced by the final ``fire_trigger`` call.

    Returns ``(ok, diverted)`` so callers can distinguish a clean
    transition (``ok=True, diverted=False``) from a confidence-gate
    divert to HUMAN_NEEDED (``ok=True, diverted=True``) from a
    refusal (``ok=False, diverted=False``).
    """
    artifacts = result.artifacts or {}

    if result.trigger == "":
        extra_add = list(artifacts.get("extra_add", ()))
        extra_remove = list(artifacts.get("extra_remove", ()))
        if not extra_add and not extra_remove:
            return True, False
        if is_pr:
            ok = _set_pr_labels(
                number, add=extra_add, remove=extra_remove,
                log_prefix=log_prefix,
            )
        else:
            ok = _set_labels(
                number, add=extra_add, remove=extra_remove,
                log_prefix=log_prefix,
            )
        return (bool(ok), False)

    from cai_lib.fsm import fire_trigger

    fire_kwargs: dict = dict(
        is_pr=is_pr,
        confidence=result.confidence,
        divert_reason=result.divert_reason or "",
        extra_remove=tuple(artifacts.get("extra_remove", ())),
        current_labels=current_labels,
        current_pr=current_pr,
        log_prefix=log_prefix,
    )
    reason_extra = artifacts.get("reason_extra")
    if reason_extra:
        fire_kwargs["reason_extra"] = reason_extra

    outcome: tuple[bool, bool] = (False, False)
    for _attempt in range(2):
        outcome = fire_trigger(number, result.trigger, **fire_kwargs)
        if outcome[0]:
            return outcome
    return outcome


def _build_ordering_gate() -> dict[int, tuple[int, int]]:
    """Return a map ``child_number -> (parent_number, prior_open_sibling)``
    for every sub-issue whose position in its parent's ordered sub-issues
    list is preceded by a still-open sibling.

    Sub-issues absent from the map are either step 1 under their parent,
    have no still-open prior sibling, or have no parent at all — i.e. not
    gated. The dispatcher uses this map to skip dispatching a sub-issue
    until the immediately prior one closes, reproducing the previous
    title-regex gate semantics without relying on title formatting.
    """
    try:
        parents = _gh_json([
            "issue", "list",
            "--repo", REPO,
            "--label", LABEL_PARENT,
            "--state", "all",
            "--json", "number",
            "--limit", "200",
        ]) or []
    except subprocess.CalledProcessError as e:
        print(f"[cai dispatch] gh issue list (parents) failed:\n{e.stderr}",
              file=sys.stderr)
        return {}

    gate: dict[int, tuple[int, int]] = {}
    subtree_cache: dict[int, list[dict]] = {}

    def _cached_sub_issues(num: int) -> list[dict]:
        if num not in subtree_cache:
            subtree_cache[num] = list_sub_issues(num)
        return subtree_cache[num]

    for parent in parents:
        parent_num = parent.get("number")
        if parent_num is None:
            continue
        last_open_sibling: Optional[int] = None
        for sub in _cached_sub_issues(parent_num):
            child_num = sub.get("number")
            if child_num is None:
                continue
            if last_open_sibling is not None:
                gate[child_num] = (parent_num, last_open_sibling)
            if sub.get("state") == "open":
                last_open_sibling = child_num

    # Second pass: propagate gates from nested parents down to their
    # descendants. When a sub-issue P is itself a parent (carries
    # LABEL_PARENT) and P is gated (gate[P] is set), then every
    # descendant of P that is not already locally gated should inherit
    # P's blocker so the picker skips them too.
    parent_nums = {p["number"] for p in parents if p.get("number") is not None}
    _propagate_visited: set[int] = set()

    def _propagate(p: int, inherited: tuple[int, int]) -> None:
        if p in _propagate_visited:
            return
        _propagate_visited.add(p)
        for sub in _cached_sub_issues(p):
            child_num = sub.get("number")
            if child_num is None:
                continue
            if child_num not in gate:
                gate[child_num] = inherited
            if child_num in parent_nums:
                _propagate(child_num, inherited)

    for p_num, blocker in list(gate.items()):
        if p_num in parent_nums:
            _propagate(p_num, blocker)

    return gate


# ---------------------------------------------------------------------------
# Actionable state sets (hardcoded, decoupled from registry derivation)
# ---------------------------------------------------------------------------
#
# These drive the queue picker (:func:`_pick_oldest_actionable_target`)
# and the inner drive-loop termination in
# :func:`_drive_target_to_completion`. Kept in a frozenset so the picker
# sees the full state set regardless of which states are in the
# collapsed registry.

_ACTIONABLE_ISSUE_STATES: frozenset[IssueState] = frozenset({
    IssueState.RAISED,
    IssueState.TRIAGING,
    IssueState.REFINING,
    IssueState.NEEDS_EXPLORATION,
    IssueState.REFINED,
    IssueState.SPLITTING,
    IssueState.PLANNING,
    IssueState.PLANNED,
    IssueState.PLAN_APPROVED,
    IssueState.IN_PROGRESS,
    IssueState.APPLYING,
    IssueState.APPLIED,
    IssueState.PR,
    IssueState.MERGED,
    IssueState.HUMAN_NEEDED,
})

_ACTIONABLE_PR_STATES: frozenset[PRState] = frozenset({
    PRState.OPEN,
    PRState.REVIEWING_CODE,
    PRState.REVISION_PENDING,
    PRState.REVIEWING_DOCS,
    PRState.APPROVED,
    PRState.REBASING,
    PRState.CI_FAILING,
    PRState.PR_HUMAN_NEEDED,
})


# ---------------------------------------------------------------------------
# Pattern A entry transitions fired by ``drive_issue`` / ``drive_pr``
# ---------------------------------------------------------------------------
#
# ``drive_issue`` fires the mapped transition before calling the
# handler for its pre-entry state. Example: RAISED fires
# ``raise_to_triaging`` so the issue is at :triaging when
# ``handle_triage`` runs. States absent from the map have no entry
# transition (resume paths or states that are already at their
# working label).

_ISSUE_ENTRY_TRANSITIONS: dict[IssueState, str] = {
    IssueState.RAISED:        "raise_to_triaging",
    IssueState.REFINED:       "refined_to_splitting",
    IssueState.PLAN_APPROVED: "approved_to_in_progress",
}

# ``drive_pr`` fires the mapped transition before calling the handler
# for its pre-entry state. Example: REVISION_PENDING fires
# ``revision_pending_to_reviewing_code`` so the PR is at
# :reviewing-code by the time ``handle_revise`` force-pushes its
# commit. The semantic shift (fired pre-handler rather than post-push)
# means a crashing subagent leaves the PR at :reviewing-code; the
# re-review on the next tick routes it back to REVISION_PENDING when
# it spots the still-unaddressed comments.

_PR_ENTRY_TRANSITIONS: dict[PRState, str] = {
    PRState.REVISION_PENDING: "revision_pending_to_reviewing_code",
}


# Transitions present in ``cai_lib/fsm_transitions.py`` that are
# unreachable from the collapsed-registry dispatch flow. Kept here as
# documentation so the catalog-trim follow-up (#1129) can audit them
# and decide which to delete.
_UNREACHABLE_ISSUE_TRANSITIONS: tuple[str, ...] = (
    "raise_to_refining",       # superseded by raise_to_triaging entry
    "refined_to_planning",     # superseded by refined_to_splitting entry
)

_UNREACHABLE_PR_TRANSITIONS: tuple[str, ...] = (
    "reviewing_code_to_ci_failing",
    "revision_pending_to_ci_failing",
    "reviewing_docs_to_ci_failing",
    "reviewing_docs_to_reviewing_code",
)


# ---------------------------------------------------------------------------
# Handler tables
# ---------------------------------------------------------------------------

IssueHandler = Callable[[dict], Union[int, HandlerResult]]
PRHandler    = Callable[[dict], HandlerResult]


def _build_drive_issue_handlers() -> dict[IssueState, IssueHandler]:
    """Lazy handler lookup for :func:`drive_issue`.

    Keyed by the **pre-entry** state. Handlers that share an enum (e.g.
    ``handle_triage`` for RAISED + TRIAGING) appear twice. Deferred
    imports mirror :func:`_build_issue_registry` so handlers can
    ``import *`` FSM helpers without creating an import cycle.
    ``IssueState.PR`` is intentionally absent — ``drive_issue`` handles
    it with the inlined bounce logic (:func:`_resolve_pr_state`).
    """
    from cai_lib.actions.triage    import handle_triage
    from cai_lib.actions.refine    import handle_refine
    from cai_lib.actions.explore   import handle_explore
    from cai_lib.actions.split     import handle_split
    from cai_lib.actions.plan      import handle_plan, handle_plan_gate
    from cai_lib.actions.implement import handle_implement
    from cai_lib.actions.maintain  import handle_maintain, handle_applied

    return {
        IssueState.RAISED:            handle_triage,
        IssueState.TRIAGING:          handle_triage,
        IssueState.REFINING:          handle_refine,
        IssueState.NEEDS_EXPLORATION: handle_explore,
        IssueState.REFINED:           handle_split,
        IssueState.SPLITTING:         handle_split,
        IssueState.PLANNING:          handle_plan,
        IssueState.PLANNED:           handle_plan_gate,
        IssueState.PLAN_APPROVED:     handle_implement,
        IssueState.IN_PROGRESS:       handle_implement,
        IssueState.APPLYING:          handle_maintain,
        IssueState.APPLIED:           handle_applied,
    }


def _build_drive_pr_handlers() -> dict[PRState, PRHandler]:
    """Lazy handler lookup for :func:`drive_pr`.

    Keyed by the **pre-entry** state. Handlers that share an enum would
    appear twice; currently every PR-side handler maps one state.
    """
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
    }


_DRIVE_ISSUE_HANDLERS: Optional[dict[IssueState, IssueHandler]] = None
_DRIVE_PR_HANDLERS:    Optional[dict[PRState, PRHandler]]       = None


def _drive_issue_handlers() -> dict[IssueState, IssueHandler]:
    global _DRIVE_ISSUE_HANDLERS
    if _DRIVE_ISSUE_HANDLERS is None:
        _DRIVE_ISSUE_HANDLERS = _build_drive_issue_handlers()
    return _DRIVE_ISSUE_HANDLERS


def _drive_pr_handlers() -> dict[PRState, PRHandler]:
    global _DRIVE_PR_HANDLERS
    if _DRIVE_PR_HANDLERS is None:
        _DRIVE_PR_HANDLERS = _build_drive_pr_handlers()
    return _DRIVE_PR_HANDLERS


# ---------------------------------------------------------------------------
# Collapsed dispatch registries
# ---------------------------------------------------------------------------
#
# The collapsed registry holds only the entry state (RAISED / OPEN) —
# which routes to the driver — and the terminal / parked-resume states
# (MERGED / HUMAN_NEEDED / PR_HUMAN_NEEDED) that need specialised
# handlers. Every other actionable state falls through to the driver
# via the actionable-state fallback in :func:`dispatch_issue` /
# :func:`dispatch_pr`.

def _build_issue_registry() -> dict[IssueState, IssueHandler]:
    from cai_lib.actions.confirm   import handle_confirm
    from cai_lib.cmd_unblock       import handle_human_needed

    return {
        IssueState.RAISED:       drive_issue,
        IssueState.HUMAN_NEEDED: handle_human_needed,
        IssueState.MERGED:       handle_confirm,
    }


def _build_pr_registry() -> dict[PRState, PRHandler]:
    from cai_lib.cmd_unblock import handle_pr_human_needed

    return {
        PRState.OPEN:            drive_pr,
        PRState.PR_HUMAN_NEEDED: handle_pr_human_needed,
    }


# Pre-merge PR states from which the dispatcher can divert into REBASING
# when it sees ``mergeable == "CONFLICTING"`` / ``mergeStateStatus == "DIRTY"``.
# Maps each from-state to the canonical ``*_to_rebasing`` transition name.
# REBASING itself, MERGED, and OPEN are intentionally
# excluded — REBASING is already running it; OPEN doesn't have a label
# so fire_trigger wouldn't have one to remove.
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
    """Set of issue states that are actionable by the dispatcher."""
    return set(_ACTIONABLE_ISSUE_STATES)


def actionable_pr_states() -> set[PRState]:
    """Set of PR states that are actionable by the dispatcher."""
    return set(_ACTIONABLE_PR_STATES)


# ---------------------------------------------------------------------------
# Inlined PR-bounce helpers (used by ``drive_issue`` at IssueState.PR)
# ---------------------------------------------------------------------------
#
# When an issue reaches :pr-open, ``drive_issue`` either follows the
# linked open PR (the happy path) or recovers from an orphaned label by
# inspecting the recently-closed PRs for that branch and routing the
# issue to the appropriate recovery state.

_BRANCH_PREFIX_TEMPLATE = "auto-improve/{n}-"


def _find_open_linked_pr(issue_number: int) -> Optional[dict]:
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


def _find_recent_closed_linked_pr(issue_number: int) -> Optional[dict]:
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
    matches.sort(
        key=lambda pr: pr.get("closedAt") or pr.get("mergedAt") or "",
        reverse=True,
    )
    return matches[0]


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
    most recent ``closed`` event.
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


def _was_merged(pr: dict) -> bool:
    return bool(pr.get("mergedAt")) or pr.get("state") == "MERGED"


def _resolve_pr_state(issue: dict) -> Union[int, HandlerResult]:
    """Decide what to do for an issue at ``IssueState.PR``.

    Happy path: an open linked PR exists → bounce via
    :func:`dispatch_pr`. Otherwise scan recently-closed PRs for the
    branch and route the issue to the appropriate recovery transition
    (merged → ``pr_to_merged``; bot-closed unmerged → ``pr_to_refined``;
    human-closed unmerged or orphan → ``pr_to_human_needed``).
    """
    issue_number = issue["number"]

    open_pr = _find_open_linked_pr(issue_number)
    if open_pr is not None:
        return dispatch_pr(open_pr["number"])

    closed_pr = _find_recent_closed_linked_pr(issue_number)
    if closed_pr is not None:
        if _was_merged(closed_pr):
            print(
                f"[cai dispatch] issue #{issue_number}: linked PR "
                f"#{closed_pr['number']} merged but issue still at :pr-open — "
                f"advancing pr_to_merged",
                flush=True,
            )
            return HandlerResult(trigger="pr_to_merged")

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
            return HandlerResult(trigger="pr_to_refined")

        actor_str = close_actor or "unknown"
        print(
            f"[cai dispatch] issue #{issue_number}: linked PR "
            f"#{closed_pr['number']} closed unmerged by {actor_str} "
            f"(our login: {our_login or 'unknown'}) — diverting "
            f"pr_to_human_needed",
            flush=True,
        )
        return HandlerResult(
            trigger="pr_to_human_needed",
            divert_reason=(
                f"Linked PR #{closed_pr['number']} was closed "
                f"unmerged by `{actor_str}` (our login: "
                f"`{our_login or 'unknown'}`). Because the closer "
                f"is not this container, the close was a deliberate "
                f"human decision — a human must decide the next "
                f"move for this issue."
            ),
        )

    print(
        f"[cai dispatch] issue #{issue_number}: no PR found (open or recently "
        f"closed) for branch auto-improve/{issue_number}-* — diverting "
        f"pr_to_human_needed",
        flush=True,
    )
    return HandlerResult(
        trigger="pr_to_human_needed",
        divert_reason=(
            f"Issue was at `:pr-open` but no PR (open or recently "
            f"closed) could be found for branch "
            f"`auto-improve/{issue_number}-*`. The label was applied "
            f"without provenance — a human must decide whether to "
            f"reopen a PR or revert the issue to a pre-PR state."
        ),
    )


# ---------------------------------------------------------------------------
# Drivers — one handler step with inline Pattern A entry transition
# ---------------------------------------------------------------------------

def drive_issue(issue: dict) -> int:
    """One dispatch step for an issue at a non-registry actionable state.

    Fires the Pattern A entry transition inline (if ``state`` is in
    :data:`_ISSUE_ENTRY_TRANSITIONS`), then invokes the handler for
    ``state`` from :func:`_drive_issue_handlers`. The handler's return
    value — ``int`` or :class:`HandlerResult` — is adapted to the
    dispatcher's ``int`` exit code. ``IssueState.PR`` is handled
    inline via :func:`_resolve_pr_state` (the old ``handle_pr_bounce``
    is gone).
    """
    issue_number = issue["number"]
    label_names = [lb["name"] for lb in issue.get("labels", [])]
    state = get_issue_state(label_names)
    if state is None:
        return 0

    # PR state is handled inline — no entry transition, no handler table.
    if state == IssueState.PR:
        rc = _resolve_pr_state(issue)
        if isinstance(rc, HandlerResult):
            ok, _ = _driver_fire(
                issue_number, rc,
                is_pr=False, current_labels=label_names,
            )
            return 0 if ok else 1
        return rc

    # Fire Pattern A entry transition if applicable.
    entry = _ISSUE_ENTRY_TRANSITIONS.get(state)
    if entry is not None:
        from cai_lib.fsm import fire_trigger
        ok, diverted = fire_trigger(
            issue_number, entry,
            current_labels=label_names,
            log_prefix="cai dispatch",
        )
        if not ok:
            print(
                f"[cai dispatch] issue #{issue_number}: entry {entry} "
                "failed", file=sys.stderr, flush=True,
            )
            return 1
        if diverted:
            return 0

    handler = _drive_issue_handlers().get(state)
    if handler is None:
        print(
            f"[cai dispatch] issue #{issue_number} at {state.name} — "
            "no drive handler", flush=True,
        )
        return 0

    print(
        f"[cai dispatch] issue #{issue_number} at {state.name} → "
        f"{handler.__name__}", flush=True,
    )
    # Issue #1203: stamp the FSM state on every cost-log row produced by
    # the handler (via _run_claude_p) so downstream readers can group by
    # funnel position without parsing the free-form ``category`` field.
    with set_current_fsm_state(state.name):
        rc = handler(issue)
    if isinstance(rc, HandlerResult):
        ok, _ = _driver_fire(
            issue_number, rc,
            is_pr=False, current_labels=label_names,
        )
        return 0 if ok else 1
    return rc


def drive_pr(pr: dict) -> int:
    """One dispatch step for a PR at a non-registry actionable state.

    Fires the Pattern A entry transition inline (if ``state`` is in
    :data:`_PR_ENTRY_TRANSITIONS`), then invokes the handler for
    ``state`` from :func:`_drive_pr_handlers`. All PR-side handlers
    return :class:`HandlerResult`; the result is threaded through
    :func:`_driver_fire`.
    """
    pr_number = pr["number"]
    state = get_pr_state(pr)
    if state is None:
        return 0

    entry = _PR_ENTRY_TRANSITIONS.get(state)
    if entry is not None:
        from cai_lib.fsm import fire_trigger
        ok, diverted = fire_trigger(
            pr_number, entry,
            is_pr=True, current_pr=pr,
            log_prefix="cai dispatch",
        )
        if not ok:
            print(
                f"[cai dispatch] PR #{pr_number}: entry {entry} failed",
                file=sys.stderr, flush=True,
            )
            return 1
        if diverted:
            return 0

    handler = _drive_pr_handlers().get(state)
    if handler is None:
        print(
            f"[cai dispatch] PR #{pr_number} at {state.name} — "
            "no drive handler", flush=True,
        )
        return 0

    print(
        f"[cai dispatch] PR #{pr_number} at {state.name} → "
        f"{handler.__name__}", flush=True,
    )
    # Issue #1203: stamp the FSM state on every cost-log row produced by
    # the handler (via _run_claude_p) so downstream readers can group by
    # funnel position without parsing the free-form ``category`` field.
    with set_current_fsm_state(state.name):
        result = handler(pr)
    ok, _ = _driver_fire(
        pr_number, result,
        is_pr=True, current_pr=pr,
    )
    return 0 if ok else 1


# ---------------------------------------------------------------------------
# Dispatch entry points
# ---------------------------------------------------------------------------

def dispatch_issue(issue_number: int) -> int:
    """Dispatch a single issue by number.

    Fetches the issue, derives state from labels, looks up the handler
    in the collapsed registry; for any other actionable state, falls
    back to :func:`drive_issue`. Returns 0 if the issue is terminal /
    unlabelled / parked-without-resume.
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
        if state in _ACTIONABLE_ISSUE_STATES:
            handler = drive_issue
        else:
            print(f"[cai dispatch] issue #{issue_number} at {state.name} — "
                  "no handler (terminal / parked)", flush=True)
            return 0

    print(f"[cai dispatch] issue #{issue_number} at {state.name} → {handler.__name__}",
          flush=True)
    if not _acquire_remote_lock("issue", issue_number):
        print(f"[cai dispatch] issue #{issue_number}: lock busy, yielding",
              flush=True)
        return 0
    try:
        # Issue #1203: stamp the FSM state on every cost-log row the
        # handler produces so downstream readers can group by funnel
        # position without parsing the free-form ``category`` field.
        # Covers MERGED / HUMAN_NEEDED handlers that bypass ``drive_issue``.
        with set_current_fsm_state(state.name):
            rc = handler(issue)
        if isinstance(rc, HandlerResult):
            ok, _ = _driver_fire(
                issue_number, rc,
                is_pr=False, current_labels=label_names,
            )
            return 0 if ok else 1
        return rc
    finally:
        _release_remote_lock("issue", issue_number)


def dispatch_pr(pr_number: int) -> int:
    """Dispatch a single PR by number.

    Fetches the PR, derives state from labels. If the PR is mergeable
    against main, runs the registered handler for its state (or
    :func:`drive_pr` for any actionable state not in the collapsed
    registry). If a rebase against main is needed, applies the
    matching ``*_to_rebasing`` transition first and routes to
    ``handle_rebase`` regardless of the pipeline label — the rebase
    handler always exits to REVIEWING_CODE so the next tick re-reviews
    the rebased SHA.
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
        from cai_lib.fsm import fire_trigger
        from cai_lib.actions.rebase import handle_rebase
        entry = _REBASE_ENTRY_TRANSITIONS[state]
        print(f"[cai dispatch] PR #{pr_number} at {state.name} has "
              f"mergeable={pr.get('mergeable')} / "
              f"mergeStateStatus={pr.get('mergeStateStatus')} → "
              f"{entry} → handle_rebase", flush=True)
        if not _acquire_remote_lock("pr", pr_number):
            print(f"[cai dispatch] PR #{pr_number}: lock busy, yielding",
                  flush=True)
            return 0
        try:
            fire_trigger(pr_number, entry, is_pr=True, current_pr=pr,
                         log_prefix="cai dispatch")
            result = handle_rebase(pr)
            ok, _ = _driver_fire(
                pr_number, result,
                is_pr=True, current_pr=pr,
            )
            return 0 if ok else 1
        finally:
            _release_remote_lock("pr", pr_number)

    handler = _pr_registry().get(state)
    if handler is None:
        if state in _ACTIONABLE_PR_STATES:
            handler = drive_pr
        else:
            print(f"[cai dispatch] PR #{pr_number} at {state.name} — "
                  "no handler (terminal / parked)", flush=True)
            return 0

    print(f"[cai dispatch] PR #{pr_number} at {state.name} → {handler.__name__}",
          flush=True)
    if not _acquire_remote_lock("pr", pr_number):
        print(f"[cai dispatch] PR #{pr_number}: lock busy, yielding",
              flush=True)
        return 0
    try:
        # Issue #1203: stamp the FSM state on every cost-log row the
        # handler produces so downstream readers can group by funnel
        # position without parsing the free-form ``category`` field.
        # Covers PR_HUMAN_NEEDED handlers that bypass ``drive_pr``.
        with set_current_fsm_state(state.name):
            result = handler(pr)
        if isinstance(result, HandlerResult):
            ok, _ = _driver_fire(
                pr_number, result,
                is_pr=True, current_pr=pr,
            )
            return 0 if ok else 1
        return result
    finally:
        _release_remote_lock("pr", pr_number)


def _pick_oldest_actionable_target(
    skip: Optional[set[tuple[str, int]]] = None,
) -> Optional[tuple[str, int]]:
    """Return ``(kind, number)`` of the oldest open issue/PR in a state with
    a registered handler, or ``None`` if the queue is empty.

    ``kind`` is ``"issue"`` or ``"pr"``. Sort key is the GitHub
    ``createdAt`` timestamp (oldest first), so PRs that have been around
    longer get the next tick — keeps in-flight work ahead of fresh intake.

    Issues and PRs carrying a ``blocked-on:<N>`` label are skipped if issue
    ``#<N>`` is still open. Candidates with open blockers remain suppressed
    until the blocker closes (the label is never auto-removed).

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

    gate = _build_ordering_gate()

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
    blocker_cache: dict[int, bool] = {}

    for issue in issues:
        label_names = [lb["name"] for lb in issue.get("labels", [])]
        state = get_issue_state(label_names)
        if state is not None and state in issue_states:
            if ("issue", issue["number"]) in skip:
                continue
            blocker = gate.get(issue["number"])
            if blocker is not None:
                parent, prior = blocker
                print(
                    f"[cai dispatch] issue #{issue['number']}: prior "
                    f"sibling #{prior} under parent #{parent} still open — "
                    f"skipping until previous sub-issue is closed",
                    flush=True,
                )
                continue
            if (state == IssueState.HUMAN_NEEDED
                    and LABEL_HUMAN_SOLVED not in label_names):
                continue
            blockers = blocking_issue_numbers(issue.get("labels", []))
            if blockers:
                open_set = open_blockers(blockers, cache=blocker_cache)
                if open_set:
                    print(
                        f"[cai dispatch] issue #{issue['number']}: blocked on "
                        f"open #{sorted(open_set)[0]} "
                        f"({len(open_set)}/{len(blockers)} blocker(s) still open) "
                        f"— skipping",
                        flush=True,
                    )
                    continue
            candidates.append((issue.get("createdAt", ""), "issue", issue["number"]))

    for pr in prs:
        state = get_pr_state(pr)
        if state in pr_states:
            if ("pr", pr["number"]) in skip:
                continue
            if state == PRState.PR_HUMAN_NEEDED:
                pr_label_names = [
                    (lb.get("name") if isinstance(lb, dict) else lb)
                    for lb in pr.get("labels", [])
                ]
                if LABEL_HUMAN_SOLVED not in pr_label_names:
                    continue
            blockers = blocking_issue_numbers(pr.get("labels", []))
            if blockers:
                open_set = open_blockers(blockers, cache=blocker_cache)
                if open_set:
                    print(
                        f"[cai dispatch] PR #{pr['number']}: blocked on "
                        f"open #{sorted(open_set)[0]} "
                        f"({len(open_set)}/{len(blockers)} blocker(s) still open) "
                        f"— skipping",
                        flush=True,
                    )
                    continue
            candidates.append((pr.get("createdAt", ""), "pr", pr["number"]))

    if not candidates:
        return None

    candidates.sort(key=lambda c: c[0])
    _, kind, number = candidates[0]
    return (kind, number)


# Default cap on how many **distinct targets** a single drain pass will
# drive. Each target is driven end-to-end (through as many state
# transitions as its handlers will advance it, plus issue↔PR hops)
# before the outer loop picks the next target, so the cap is an upper
# bound on unique issues/PRs touched per tick, not on handler invocations.
_DEFAULT_DRAIN_MAX_ITER = 50

# Safety guard against a driver that would never reach a terminal/blocked
# state. The longest legitimate chain is roughly:
# RAISED → TRIAGING → REFINING → REFINED → PLANNING → PLANNED →
# PLAN_APPROVED → IN_PROGRESS → PR → (PR states: OPEN → REVIEWING_CODE →
# REVIEWING_DOCS → APPROVED → MERGED) → issue MERGED → SOLVED (~16 steps).
_INNER_LOOP_CAP = 24

# CI-pending poll: the user chose "poll briefly" rather than giving up
# the tick immediately when a PR's state hasn't changed because CI is
# still running.
_CI_POLL_MAX_SECONDS = 60
_CI_POLL_INTERVAL_SECONDS = 10


def _fetch_issue_state(number: int) -> Optional[IssueState]:
    """Return the current IssueState for ``number``, or None when the issue
    is closed / missing its FSM label. Lightweight (labels + state only)."""
    try:
        issue = _gh_json([
            "issue", "view", str(number),
            "--repo", REPO,
            "--json", "labels,state",
        ])
    except subprocess.CalledProcessError as e:
        print(f"[cai dispatch] gh issue view #{number} failed:\n{e.stderr}",
              file=sys.stderr)
        return None
    if issue.get("state") == "CLOSED":
        return None
    labels = [lb["name"] for lb in issue.get("labels", [])]
    return get_issue_state(labels)


def _fetch_pr_state_info(number: int) -> Optional[tuple[PRState, dict]]:
    """Return ``(state, pr_dict)`` for ``number``, or None on fetch error.

    The returned dict carries enough for post-dispatch decisions:
    ``mergedAt``, ``headRefName``, and ``statusCheckRollup`` for
    CI-pending detection.
    """
    try:
        pr = _gh_json([
            "pr", "view", str(number),
            "--repo", REPO,
            "--json",
            "number,labels,state,mergedAt,headRefName,statusCheckRollup",
        ])
    except subprocess.CalledProcessError as e:
        print(f"[cai dispatch] gh pr view #{number} failed:\n{e.stderr}",
              file=sys.stderr)
        return None
    return get_pr_state(pr), pr


def _pr_ci_pending(pr: dict) -> bool:
    """True when at least one check in ``statusCheckRollup`` is still running
    (queued / in-progress / pending). CheckRun uses status/conclusion;
    StatusContext uses state — we cover both."""
    rollup = pr.get("statusCheckRollup") or []
    for check in rollup:
        status = (check.get("status") or "").upper()
        state = (check.get("state") or "").upper()
        if status in ("QUEUED", "IN_PROGRESS", "WAITING", "PENDING", "REQUESTED"):
            return True
        if state in ("PENDING", "EXPECTED"):
            return True
    return False


def _issue_number_from_pr_branch(pr: dict) -> Optional[int]:
    """Parse the issue number from an ``auto-improve/<N>-...`` branch."""
    head = pr.get("headRefName", "") or ""
    m = re.match(r"auto-improve/(\d+)-", head)
    return int(m.group(1)) if m else None


def _drive_target_to_completion(
    kind: str, number: int,
    touched: set[tuple[str, int]],
) -> int:
    """Drive a single target through state transitions until terminal or blocked.

    The inner loop:
      1. Fetches the entity's current state.
      2. Returns when the state has no registered handler (terminal like
         SOLVED / MERGED; or HUMAN_NEEDED / PR_HUMAN_NEEDED without
         ``human:solved``, which the picker filters out).
      3. Otherwise calls the registered handler (via :func:`dispatch_issue`
         or :func:`dispatch_pr`).
      4. Re-fetches state to detect progress.
      5. Hops across the issue↔PR boundary when an issue advances to the
         ``PR`` state (follow the linked PR) or when a PR merges (follow
         back to the issue so ``confirm`` runs in the same tick).
      6. Treats "state unchanged after a handler call" as blocked — the
         handler saw no work to advance. One exception: PRs whose CI is
         still running get polled for up to ``_CI_POLL_MAX_SECONDS`` (the
         user chose brief poll over giving up the tick).

    Every ``(kind, number)`` visited is added to ``touched`` so the outer
    drain won't re-pick the same target later in the same tick.

    Lock lifecycle: an outer ``try/finally`` wraps the whole drive so a
    single ``_release_remote_lock`` covers every exit path (returns *and*
    uncaught exceptions). The only manual release/acquire pair sits at
    the issue↔PR hop, where the old target must be released before the
    new one is acquired (a ``finally``-only pattern can't sequence that).
    The inner per-dispatch acquire/release wrappers in ``dispatch_*``
    are no-ops while ``held`` is set thanks to the refcount in
    ``_HELD_LOCKS``.
    """
    import time
    import traceback

    worst_rc = 0
    ci_polled = False
    held: tuple[str, int] | None = None

    try:
        touched.add((kind, number))
        if not _acquire_remote_lock(kind, number):
            print(f"[cai dispatch] {kind} #{number}: lock busy at drive "
                  "entry, yielding", flush=True)
            return worst_rc
        held = (kind, number)

        for _ in range(_INNER_LOOP_CAP):
            touched.add((kind, number))

            # --- Pre-dispatch state ---
            if kind == "issue":
                pre_state = _fetch_issue_state(number)
                if pre_state is None:
                    return worst_rc
                if pre_state not in actionable_issue_states():
                    print(f"[cai dispatch] issue #{number} at "
                          f"{pre_state.name} — terminal/parked, drive done",
                          flush=True)
                    return worst_rc
            else:
                info = _fetch_pr_state_info(number)
                if info is None:
                    return worst_rc
                pre_state, _pre_pr = info
                if pre_state not in actionable_pr_states():
                    print(f"[cai dispatch] PR #{number} at "
                          f"{pre_state.name} — terminal/parked, drive done",
                          flush=True)
                    return worst_rc

            # --- Dispatch one handler step ---
            try:
                rc = dispatch_issue(number) if kind == "issue" else dispatch_pr(number)
            except Exception:
                traceback.print_exc()
                print(f"[cai dispatch] handler for {kind} #{number} raised; "
                      "stopping drive", flush=True)
                return max(worst_rc, 1)
            worst_rc = max(worst_rc, rc)

            # --- Post-dispatch state ---
            if kind == "issue":
                post_state = _fetch_issue_state(number)
                if post_state is None:
                    return worst_rc
                # Issue→PR hop: issue advanced to PR state — drive the linked PR.
                if post_state == IssueState.PR:
                    linked_pr = _find_open_linked_pr(number)
                    linked = linked_pr["number"] if linked_pr else None
                    if linked is not None and ("pr", linked) not in touched:
                        print(f"[cai dispatch] issue #{number} advanced to PR — "
                              f"following PR #{linked}", flush=True)
                        if held is not None:
                            _release_remote_lock(*held)
                            held = None
                        kind, number = "pr", linked
                        if not _acquire_remote_lock(kind, number):
                            print(f"[cai dispatch] {kind} #{number}: lock "
                                  "busy after issue→PR hop, yielding",
                                  flush=True)
                            return worst_rc
                        held = (kind, number)
                        ci_polled = False
                        continue
                    return worst_rc
                if post_state == pre_state:
                    print(f"[cai dispatch] issue #{number} at "
                          f"{post_state.name}: no state change — blocked, "
                          f"moving on", flush=True)
                    return worst_rc
                continue

            # kind == "pr"
            post_info = _fetch_pr_state_info(number)
            if post_info is None:
                return worst_rc
            post_state, post_pr = post_info

            # PR merged → hop back to the linked issue (now at MERGED) so
            # confirm runs in the same drive.
            if post_pr.get("mergedAt") or post_state == PRState.MERGED:
                issue_num = _issue_number_from_pr_branch(post_pr)
                if issue_num is not None and ("issue", issue_num) not in touched:
                    print(f"[cai dispatch] PR #{number} merged — following "
                          f"back to issue #{issue_num}", flush=True)
                    if held is not None:
                        _release_remote_lock(*held)
                        held = None
                    kind, number = "issue", issue_num
                    if not _acquire_remote_lock(kind, number):
                        print(f"[cai dispatch] {kind} #{number}: lock "
                              "busy after PR→issue hop, yielding",
                              flush=True)
                        return worst_rc
                    held = (kind, number)
                    ci_polled = False
                    continue
                return worst_rc

            if post_state != pre_state:
                ci_polled = False
                continue

            # No state change on a PR. Brief CI poll before giving up.
            if not ci_polled and _pr_ci_pending(post_pr):
                print(f"[cai dispatch] PR #{number} at {post_state.name}: CI "
                      f"pending — polling up to {_CI_POLL_MAX_SECONDS}s",
                      flush=True)
                waited = 0
                while waited < _CI_POLL_MAX_SECONDS:
                    time.sleep(_CI_POLL_INTERVAL_SECONDS)
                    waited += _CI_POLL_INTERVAL_SECONDS
                    info = _fetch_pr_state_info(number)
                    if info is None:
                        return worst_rc
                    _, polled_pr = info
                    if not _pr_ci_pending(polled_pr):
                        print(f"[cai dispatch] PR #{number}: CI settled after "
                              f"{waited}s — retrying dispatch", flush=True)
                        break
                ci_polled = True
                continue

            print(f"[cai dispatch] PR #{number} at {post_state.name}: no state "
                  f"change and no CI to wait on — blocked, moving on",
                  flush=True)
            return worst_rc

        print(f"[cai dispatch] inner driver hit cap "
              f"({_INNER_LOOP_CAP}) on {kind} #{number}", flush=True)
        return max(worst_rc, 1)
    finally:
        if held is not None:
            _release_remote_lock(*held)


def dispatch_drain(max_iter: int = _DEFAULT_DRAIN_MAX_ITER) -> int:
    """Drain the actionable queue, driving each target end-to-end.

    Each tick picks the oldest actionable target, then drives it through
    its state transitions (following issue↔PR hops) until it reaches a
    terminal state (SOLVED), a parked state without ``human:solved``
    (HUMAN_NEEDED / PR_HUMAN_NEEDED), or is genuinely blocked (handler ran without
    advancing state, and for PRs, CI isn't pending). Then moves to the
    next actionable target.

    This replaces the earlier "one handler step per target per drain"
    behavior, which could take many cron ticks to walk a single issue
    from RAISED to SOLVED. The new behavior is: one issue drives to
    completion in one tick, monopolizing the tick if needed. The cron
    cadence (CAI_CYCLE_SCHEDULE) still bounds how often a drain starts.

    Stops when:
      - the queue is empty,
      - ``max_iter`` distinct targets have been driven (safety cap),
      - every remaining actionable item has already been touched this tick.

    Returns the worst exit code seen across driver runs (0 if all succeeded
    or the queue was empty from the start).
    """
    touched: set[tuple[str, int]] = set()
    worst_rc = 0

    for i in range(max_iter):
        target = _pick_oldest_actionable_target(skip=touched)
        if target is None:
            print(f"[cai dispatch] drain complete after {i} target(s) "
                  "driven: queue empty", flush=True)
            return worst_rc

        kind, number = target
        print(f"[cai dispatch] driving {kind} #{number} end-to-end",
              flush=True)
        rc = _drive_target_to_completion(kind, number, touched)
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
