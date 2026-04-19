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

from cai_lib.config import LABEL_HUMAN_SOLVED, LABEL_PARENT, REPO
from cai_lib.fsm import (
    IssueState, PRState,
    get_issue_state, get_pr_state,
)
from cai_lib.github import (
    _gh_json,
    _acquire_remote_lock,
    _release_remote_lock,
    blocking_issue_numbers,
    open_blockers,
)
from cai_lib.issues import list_sub_issues


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
    # Memoize list_sub_issues calls to avoid duplicate API requests when
    # the tree is deep or shared.
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
    # descendants.  When a sub-issue P is itself a parent (carries
    # LABEL_PARENT) and P is gated (gate[P] is set), then every
    # descendant of P that is not already locally gated should inherit
    # P's blocker so the picker skips them too.
    parent_nums = {p["number"] for p in parents if p.get("number") is not None}
    _propagate_visited: set[int] = set()

    def _propagate(p: int, inherited: tuple[int, int]) -> None:
        """Walk descendants of ``p`` and assign ``inherited`` to any that
        are not yet gated.  Always recurse into nested-parent children so
        that arbitrarily deep subtrees are covered.  A visited guard
        prevents re-entering the same node if the tree branches."""
        if p in _propagate_visited:
            return
        _propagate_visited.add(p)
        for sub in _cached_sub_issues(p):
            child_num = sub.get("number")
            if child_num is None:
                continue
            if child_num not in gate:
                gate[child_num] = inherited
            # Recurse into nested parents regardless of their own gate
            # status — their children may still be ungated and need the
            # ancestor's inherited blocker.
            if child_num in parent_nums:
                _propagate(child_num, inherited)

    for p_num, blocker in list(gate.items()):
        if p_num in parent_nums:
            _propagate(p_num, blocker)

    return gate


# ---------------------------------------------------------------------------
# Handler registries
# ---------------------------------------------------------------------------
#
# Some states share a handler: e.g. both RAISED and TRIAGING route to
# ``handle_triage`` — the handler itself decides whether it's a fresh
# entry (apply the raise_to_triaging transition first) or a resume
# (skip the entry transition).
#
# States with no handler (SOLVED, MERGED on the PR
# side) are terminal or parked and the dispatcher returns without doing
# anything. HUMAN_NEEDED has a handler that auto-resumes the FSM when
# the admin has applied ``human:solved`` and no-ops otherwise.

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
    from cai_lib.actions.maintain  import handle_maintain, handle_applied
    from cai_lib.actions.confirm   import handle_confirm
    from cai_lib.actions.pr_bounce import handle_pr_bounce
    from cai_lib.cmd_unblock       import handle_human_needed

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
        IssueState.APPLYING:          handle_maintain,
        IssueState.APPLIED:           handle_applied,
        IssueState.PR:                handle_pr_bounce,
        IssueState.MERGED:            handle_confirm,
        # HUMAN_NEEDED is only picked up when the admin has applied
        # ``human:solved`` (see filter in _pick_oldest_actionable_target).
        IssueState.HUMAN_NEEDED:      handle_human_needed,
        # SOLVED → no handler (terminal)
    }


def _build_pr_registry() -> dict[PRState, PRHandler]:
    from cai_lib.actions.open_pr     import handle_open_to_review
    from cai_lib.actions.review_pr   import handle_review_pr
    from cai_lib.actions.revise      import handle_revise
    from cai_lib.actions.review_docs import handle_review_docs
    from cai_lib.actions.fix_ci      import handle_fix_ci
    from cai_lib.actions.merge       import handle_merge
    from cai_lib.actions.rebase      import handle_rebase
    from cai_lib.cmd_unblock          import handle_pr_human_needed

    return {
        PRState.OPEN:             handle_open_to_review,
        PRState.REVIEWING_CODE:   handle_review_pr,
        PRState.REVISION_PENDING: handle_revise,
        PRState.REVIEWING_DOCS:   handle_review_docs,
        PRState.APPROVED:         handle_merge,
        PRState.REBASING:         handle_rebase,
        PRState.CI_FAILING:       handle_fix_ci,
        # PR_HUMAN_NEEDED is actionable via handle_pr_human_needed —
        # the picker filters to only those PRs carrying ``human:solved``
        # so parked-waiting PRs stay out of the queue.
        PRState.PR_HUMAN_NEEDED:  handle_pr_human_needed,
        # MERGED → no handler (terminal)
    }


# Pre-merge PR states from which the dispatcher can divert into REBASING
# when it sees ``mergeable == "CONFLICTING"`` / ``mergeStateStatus == "DIRTY"``.
# Maps each from-state to the canonical ``*_to_rebasing`` transition name.
# REBASING itself, MERGED, and OPEN are intentionally
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
    if not _acquire_remote_lock("issue", issue_number):
        print(f"[cai dispatch] issue #{issue_number}: lock busy, yielding",
              flush=True)
        return 0
    try:
        return handler(issue)
    finally:
        _release_remote_lock("issue", issue_number)


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
        if not _acquire_remote_lock("pr", pr_number):
            print(f"[cai dispatch] PR #{pr_number}: lock busy, yielding",
                  flush=True)
            return 0
        try:
            apply_pr_transition(pr_number, entry, current_pr=pr,
                                log_prefix="cai dispatch")
            return handle_rebase(pr)
        finally:
            _release_remote_lock("pr", pr_number)

    handler = _pr_registry().get(state)
    if handler is None:
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
        return handler(pr)
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

    # Build the ordering gate from GitHub's native sub-issues API: for
    # each parent issue (label ``auto-improve:parent``), fetch its
    # ordered sub-issues and record, for every child, the immediately
    # prior sibling that is still open. A child present in ``gate`` is
    # blocked until that prior sibling closes. See the "ordering gate"
    # referenced in cai_lib/actions/refine.py::_create_sub_issues.
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
            # HUMAN_NEEDED is actionable only when the admin has signalled
            # ready-to-resume via ``human:solved``. Other parked issues
            # stay out of the queue so we don't spin on them each tick.
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
            # PR_HUMAN_NEEDED is actionable only when the admin has
            # signalled ready-to-resume via ``human:solved``. Mirrors
            # the issue-side gate above.
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


def _linked_open_pr_number(issue_number: int) -> Optional[int]:
    """Return the open ``auto-improve/<N>-...`` PR number for this issue, or None."""
    # Local import — pr_bounce imports from dispatcher for dispatch_pr.
    from cai_lib.actions.pr_bounce import _find_open_linked_pr
    pr = _find_open_linked_pr(issue_number)
    return pr["number"] if pr else None


def _issue_number_from_pr_branch(pr: dict) -> Optional[int]:
    """Parse the issue number from an ``auto-improve/<N>-...`` branch."""
    import re
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
                    linked = _linked_open_pr_number(number)
                    if linked is not None and ("pr", linked) not in touched:
                        print(f"[cai dispatch] issue #{number} advanced to PR — "
                              f"following PR #{linked}", flush=True)
                        # Manual release/acquire — finally can't sequence
                        # release-old-then-acquire-new across a continue.
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
                    # No linked open PR found (orphan) or already driven.
                    return worst_rc
                if post_state == pre_state:
                    # Handler ran but did not advance state → blocked.
                    print(f"[cai dispatch] issue #{number} at "
                          f"{post_state.name}: no state change — blocked, "
                          f"moving on", flush=True)
                    return worst_rc
                # State advanced on the same issue — keep driving.
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
