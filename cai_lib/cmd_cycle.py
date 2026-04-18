"""Cycle and dispatch entry-points for the cai pipeline."""
import fcntl
import os
import sys
import time

from cai_lib.config import *  # noqa: E402,F403
from cai_lib.github import _gh_json, _set_labels
from cai_lib.watchdog import _rollback_stale_in_progress
from cai_lib.dispatcher import dispatch_drain
from cai_lib.logging_utils import log_run


def _run_step(name: str, handler, args) -> int:
    """Run a single cycle step, catching exceptions."""
    print(f"\n[cai cycle] === {name} ===", flush=True)
    try:
        return handler(args)
    except Exception as exc:
        print(f"[cai cycle] {name} raised {exc!r}", file=sys.stderr, flush=True)
        return 1


_CYCLE_LOCK_PATH = f"/tmp/cai-cycle-{REPO.replace('/', '-')}.lock"


def cmd_cycle(args) -> int:
    """One cycle tick under a non-blocking flock.

    Delegates to :func:`_cmd_cycle_inner`, which reconciles labels,
    runs audit, and dispatches a single actionable issue/PR via the
    FSM dispatcher. The flock on ``_CYCLE_LOCK_PATH`` (per-repo) ensures
    overlapping supercronic fires don't step on each other.
    """
    lock_fd = os.open(_CYCLE_LOCK_PATH, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(lock_fd)
        print("[cai cycle] another cycle is already running; skipping this tick",
              flush=True)
        return 0

    try:
        return _cmd_cycle_inner(args)
    finally:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        finally:
            os.close(lock_fd)


def _cmd_cycle_inner(args) -> int:
    """One cycle tick: restart-recovery + dispatch one actionable issue/PR.

    Verify and audit run on their own cron cadences (CAI_VERIFY_SCHEDULE,
    CAI_AUDIT_SCHEDULE) — the cycle is purely restart-recovery + dispatch.
    """
    print("[cai cycle] starting cycle tick", flush=True)
    t0 = time.monotonic()
    all_results: dict[str, int] = {}
    had_failure = False

    # Phase 0: self-heal parent label. Dispatcher lists open issues via
    # `--label auto-improve`, so any issue carrying an FSM state label
    # (e.g. auto-improve:raised) but missing the parent `auto-improve`
    # label is invisible to the cycle. Add the parent where missing.
    _fsm_state_labels = (
        LABEL_RAISED, LABEL_REFINING, LABEL_REFINED,
        LABEL_PLANNING, LABEL_PLANNED, LABEL_PLAN_APPROVED,
        LABEL_APPLYING, LABEL_APPLIED, LABEL_IN_PROGRESS,
        LABEL_PR_OPEN, LABEL_REVISING, LABEL_MERGED,
        LABEL_HUMAN_NEEDED, LABEL_TRIAGING,
    )
    _healed: set[int] = set()
    for _lbl in _fsm_state_labels:
        try:
            _issues = _gh_json([
                "issue", "list",
                "--repo", REPO,
                "--label", _lbl,
                "--state", "open",
                "--json", "number,labels",
                "--limit", "100",
            ]) or []
        except Exception:
            continue
        for _iss in _issues:
            _num = _iss["number"]
            if _num in _healed:
                continue
            _names = [lb["name"] for lb in _iss.get("labels", [])]
            if "auto-improve" not in _names:
                if _set_labels(_num, add=["auto-improve"], log_prefix="cai cycle"):
                    print(
                        f"[cai cycle] self-heal: added parent "
                        f"`auto-improve` to #{_num}",
                        flush=True,
                    )
                _healed.add(_num)

    # Phase 1: restart recovery — force-rollback any stuck locks left
    # behind by a previous run that crashed mid-handler.
    rolled_back = _rollback_stale_in_progress(immediate=True)
    if rolled_back:
        nums = ", ".join(f"#{i['number']}" for i in rolled_back)
        print(f"[cai cycle] recovered {len(rolled_back)} stale lock(s): {nums}",
              flush=True)

    # Phase 2: dispatch a single actionable issue/PR via the FSM dispatcher.
    # Note: :applied → :solved bookkeeping is handled by handle_applied in
    # the dispatcher (IssueState.APPLIED), so no separate Phase 1.5 is needed.
    rc = _run_step("dispatch", lambda _a: dispatch_drain(), args)
    all_results["dispatch"] = rc
    if rc != 0:
        had_failure = True

    dur = f"{time.monotonic() - t0:.1f}s"
    summary = " ".join(f"{k}={v}" for k, v in all_results.items())
    print(f"\n[cai cycle] done in {dur} — {summary}", flush=True)
    log_run("cycle", repo=REPO, results=summary,
            duration=dur, exit=1 if had_failure else 0)
    return 1 if had_failure else 0


def cmd_dispatch(args) -> int:
    """Dispatch one or more FSM actions.

    With no args, drains the actionable queue: repeatedly picks the
    oldest actionable issue/PR and dispatches it until the queue is
    empty (or a loop-guard / max-iter cap fires). With --issue N,
    fetches issue N, derives its FSM state, and runs the matching
    handler exactly once. With --pr N, same for a PR.
    """
    from cai_lib.dispatcher import (
        dispatch_issue, dispatch_pr, dispatch_drain,
    )
    if getattr(args, "issue", None) is not None:
        return dispatch_issue(args.issue)
    if getattr(args, "pr", None) is not None:
        return dispatch_pr(args.pr)
    return dispatch_drain()
