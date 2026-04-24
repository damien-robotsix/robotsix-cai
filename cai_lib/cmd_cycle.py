"""Cycle and dispatch entry-points for the cai pipeline."""
import sys
import time

from cai_lib.config import *  # noqa: E402,F403
from cai_lib.github import _gh_json, _set_labels
from cai_lib.watchdog import (
    _rollback_stale_in_progress,
    _rollback_stale_pr_locks,
)
from cai_lib.dispatcher import dispatch_drain
from cai_lib.issues import close_completed_parents
from cai_lib.utils.log import log_run


def _run_step(name: str, handler, args) -> int:
    """Run a single cycle step, catching exceptions."""
    print(f"\n[cai cycle] === {name} ===", flush=True)
    try:
        return handler(args)
    except Exception as exc:
        print(f"[cai cycle] {name} raised {exc!r}", file=sys.stderr, flush=True)
        return 1


def cmd_cycle(args) -> int:
    """One cycle tick: restart-recovery + dispatch one actionable issue/PR.

    Cross-instance serialization is enforced GitHub-side via the
    ``auto-improve:locked`` ownership lock acquired at every dispatch
    entry (see :func:`cai_lib.github._acquire_remote_lock`); two cai
    containers — on the same host or across hosts — cannot advance the
    same issue/PR concurrently. A stale-lock watchdog expires the lock
    after ``_STALE_LOCKED_HOURS`` so a crashed handler cannot strand a
    target. Verify runs on its own cron cadence (CAI_VERIFY_SCHEDULE) —
    the cycle itself is purely restart-recovery + dispatch.
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

    # Phase 0.5: close parent issues whose sub-issues are all closed.
    # The dispatcher's ordering gate in `_build_ordering_gate` treats an
    # open parent as a still-open prior sibling, blocking later siblings
    # under the grandparent. If all of a parent's sub-issues closed
    # between verify ticks, we must close it here or the queue stalls
    # until the next verify run.
    closed_parents = close_completed_parents(log_prefix="cai cycle")
    if closed_parents:
        print(
            f"[cai cycle] closed {closed_parents} completed parent(s)",
            flush=True,
        )

    # Phase 0.6: self-heal silent HUMAN_NEEDED diverts (issue #1009).
    # Any issue/PR parked at :human-needed / :pr-human-needed without a
    # MARKER-bearing divert-reason comment is invisible to the audit
    # agent's human_needed_reason_missing parser and cannot be resumed
    # by `cai unblock` (the classifier needs the divert context).
    # Post a retroactive backfill comment so the pipeline recovers
    # without an admin hand-crafting a comment.
    try:
        from cai_lib.fsm import backfill_silent_human_needed_comments
        backfilled = backfill_silent_human_needed_comments()
        if backfilled:
            nums = ", ".join(f"{k} #{n}" for k, n in backfilled)
            print(
                f"[cai cycle] backfilled silent HUMAN_NEEDED divert "
                f"comments on {len(backfilled)} target(s): {nums}",
                flush=True,
            )
    except Exception as exc:
        print(
            f"[cai cycle] Phase 0.6 backfill raised {exc!r} — continuing",
            file=sys.stderr, flush=True,
        )

    # Phase 0.7: process <!-- cai-resplit --> admin sigils (issue #1142).
    # An admin drops the sigil in a comment on a :plan-approved issue
    # to signal that the refined-and-planned scope is too large and
    # should be re-evaluated by cai-split. Detected deterministically
    # (literal-string check, no classifier) at the start of each tick
    # so the FSM is rolled back to :refined before the dispatcher
    # picks the issue up for handle_implement.
    try:
        from cai_lib.admin_sigils import (
            scan_resplit_sigil, process_resplit_sigil,
        )
        for _num in scan_resplit_sigil():
            if process_resplit_sigil(_num):
                print(
                    f"[cai cycle] resplit sigil: #{_num} rolled back "
                    f"to :refined",
                    flush=True,
                )
            else:
                print(
                    f"[cai cycle] resplit sigil: #{_num} rollback "
                    f"failed — leaving as-is",
                    file=sys.stderr, flush=True,
                )
    except Exception as exc:
        print(
            f"[cai cycle] Phase 0.7 resplit sigil raised {exc!r} — continuing",
            file=sys.stderr, flush=True,
        )

    # Phase 1: TTL-based stale-lock sweep — rolls back only locks that
    # have exceeded their configured TTL (_STALE_LOCKED_HOURS etc.).
    # NOTE: immediate=True is NOT used here; that path bypasses TTLs and
    # is reserved for explicit container-restart recovery where every
    # in-flight lock is guaranteed orphaned.  Normal cron ticks must use
    # TTL-based detection so live handlers are not killed.
    rolled_back = _rollback_stale_in_progress()
    if rolled_back:
        nums = ", ".join(f"#{i['number']}" for i in rolled_back)
        print(f"[cai cycle] recovered {len(rolled_back)} stale lock(s): {nums}",
              flush=True)
    rolled_back_prs = _rollback_stale_pr_locks()
    if rolled_back_prs:
        nums = ", ".join(f"#{p['number']}" for p in rolled_back_prs)
        print(
            f"[cai cycle] recovered {len(rolled_back_prs)} stale PR lock(s): "
            f"{nums}",
            flush=True,
        )

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
