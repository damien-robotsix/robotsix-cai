"""cai_lib.watchdog — stale-lock recovery for the auto-improve pipeline.

Contains the single deterministic watchdog that rolls back issues whose
``:in-progress``, ``:revising``, or ``:applying`` lock has outlived its TTL
(or every such issue on container restart). The FSM in :mod:`cai_lib.fsm`
is the source of truth for issue state; this module is the paranoia backstop
that un-sticks locks a crashed driver left behind.
"""

import re
import subprocess
import sys

from datetime import datetime, timezone

from cai_lib.config import (
    REPO,
    LOG_PATH,
    LABEL_IN_PROGRESS,
    LABEL_REVISING,
    LABEL_APPLYING,
    LABEL_LOCKED,
    LABEL_RAISED,
    LABEL_REFINED,
    CAI_LOCK_COMMENT_RE,
    _STALE_IN_PROGRESS_HOURS,
    _STALE_REVISING_HOURS,
    _STALE_APPLYING_HOURS,
    _STALE_LOCKED_HOURS,
)
from cai_lib.github import (
    _gh_json,
    _set_labels,
    _set_pr_labels,
    _delete_issue_comment,
    _list_lock_comments,
)
from cai_lib.utils.log import log_run


def _delete_lock_claim_comments(number: int) -> None:
    """Fetch and delete all ``<!-- cai-lock ... -->`` claim comments on *number*."""
    try:
        comments = _gh_json([
            "api", f"/repos/{REPO}/issues/{number}/comments",
            "--paginate",
        ]) or []
    except subprocess.CalledProcessError:
        comments = []
    for c in comments:
        if CAI_LOCK_COMMENT_RE.search(c.get("body", "") or ""):
            cid = c.get("id")
            if cid is not None:
                _delete_issue_comment(int(cid), log_prefix="cai audit")


def _lock_claim_age_seconds(number: int, now: float) -> float | None:
    """Age (seconds since ``now``) of the oldest ``cai-lock`` claim comment.

    The oldest ``<!-- cai-lock owner=... acquired=... -->`` comment on an
    issue/PR is the authoritative lock-acquisition marker: ``_acquire_remote_lock``
    posts it atomically with the ``:locked`` label and the comment survives
    every crash scenario the label does. Unlike the issue/PR's ``updatedAt``
    (which GitHub bumps for CI check-runs, label churn from losing
    lock-acquire races, and many unrelated events), this timestamp is
    immune to self-perpetuating freshness loops: later cycles' failed
    acquire attempts post+delete their *own* claim comments without
    disturbing the winning-oldest one.

    Returns ``None`` when no claim comment exists (the label alone is an
    anomaly — caller should fall back to ``updatedAt``) or the comment
    endpoint errored.
    """
    locks = _list_lock_comments(number)
    if not locks:
        # :locked label with no cai-lock claim comment is an anomaly —
        # either _acquire_remote_lock partially failed (crashed between
        # the post and the label-add) or the claim comment was manually
        # deleted. Emit a warn so the orphan is visible every watchdog
        # tick, not only after _STALE_LOCKED_HOURS strips it.
        print(
            f"[cai lock] WARN: #{number} has :locked label but no "
            "cai-lock claim comment — orphan lock detected",
            file=sys.stderr,
            flush=True,
        )
        return None
    oldest = locks[0].get("created_at") or ""
    try:
        ts = datetime.strptime(oldest, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        ).timestamp()
    except (ValueError, TypeError):
        return None
    return now - ts


def _rollback_stale_in_progress(*, immediate: bool = False) -> list[dict]:
    """Deterministic rollback: :in-progress, :revising, or :applying issues with no recent activity.

    TTL-based (normal operation): each state's configured threshold applies
    (``_STALE_IN_PROGRESS_HOURS``, ``_STALE_REVISING_HOURS``,
    ``_STALE_APPLYING_HOURS``, ``_STALE_LOCKED_HOURS``).

    When ``immediate=True`` every locked issue is rolled back regardless of age.
    This path is reserved for **explicit container-restart recovery** where all
    in-flight locks are guaranteed orphaned.  It must NOT be used on normal
    hourly cron ticks — doing so kills live handlers whose lock age is below
    the TTL.

    Returns the list of issues that were rolled back.
    """
    all_issues = []
    for lock_label in (LABEL_IN_PROGRESS, LABEL_REVISING, LABEL_APPLYING, LABEL_LOCKED):
        try:
            issues = _gh_json([
                "issue", "list",
                "--repo", REPO,
                "--label", lock_label,
                "--state", "open",
                "--json", "number,title,updatedAt,createdAt,labels",
                "--limit", "100",
            ]) or []
        except subprocess.CalledProcessError as e:
            print(
                f"[cai audit] gh issue list ({lock_label}) failed:\n{e.stderr}",
                file=sys.stderr,
            )
            continue
        for issue in issues:
            issue["_lock_label"] = lock_label
            all_issues.append(issue)

    if not all_issues:
        return []

    issues = all_issues

    # Read the log tail to find the most recent [fix], [revise], or [maintain] line per issue.
    fix_timestamps: dict[int, float] = {}
    if LOG_PATH.exists():
        try:
            lines = LOG_PATH.read_text().splitlines()[-200:]
        except Exception:
            lines = []
        for line in lines:
            if "[fix]" not in line and "[revise]" not in line and "[maintain]" not in line:
                continue
            # Extract issue number from "issue=<N>"
            m = re.search(r"issue=(\d+)", line)
            if not m:
                continue
            issue_num = int(m.group(1))
            # Extract timestamp from start of line (ISO format)
            ts_match = re.match(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)", line)
            if ts_match:
                try:
                    ts = datetime.strptime(ts_match.group(1), "%Y-%m-%dT%H:%M:%SZ").replace(
                        tzinfo=timezone.utc
                    ).timestamp()
                    fix_timestamps[issue_num] = max(fix_timestamps.get(issue_num, 0), ts)
                except ValueError:
                    pass

    now = datetime.now(timezone.utc).timestamp()
    rolled_back = []

    for issue in issues:
        issue_num = issue["number"]
        lock_label = issue.get("_lock_label", LABEL_IN_PROGRESS)
        if lock_label == LABEL_REVISING:
            ttl_hours = _STALE_REVISING_HOURS
        elif lock_label == LABEL_APPLYING:
            ttl_hours = _STALE_APPLYING_HOURS
        elif lock_label == LABEL_LOCKED:
            ttl_hours = _STALE_LOCKED_HOURS
        else:
            ttl_hours = _STALE_IN_PROGRESS_HOURS
        threshold = 0 if immediate else ttl_hours * 3600
        # :locked age comes from the oldest cai-lock claim comment — the
        # authoritative acquisition marker. The log-line and updatedAt
        # fallbacks below are unsafe for :locked: every cycle's failing
        # _acquire_remote_lock posts+deletes a claim comment, bumping
        # updatedAt indefinitely and hiding locks that are hours stale.
        if lock_label == LABEL_LOCKED:
            claim_age = _lock_claim_age_seconds(issue_num, now)
            # Label set but no claim comment is an anomaly (acquire
            # crashed between label and comment, or comment was deleted).
            # Treat as stale so the watchdog can strip the orphan label.
            age = claim_age if claim_age is not None else float("inf")
        else:
            last_fix = fix_timestamps.get(issue_num)
            if last_fix is not None:
                age = now - last_fix
            else:
                # No fix log line — use the issue's updatedAt as a fallback.
                try:
                    updated = datetime.strptime(
                        issue["updatedAt"], "%Y-%m-%dT%H:%M:%SZ"
                    ).replace(tzinfo=timezone.utc).timestamp()
                except (ValueError, KeyError):
                    updated = 0
                age = now - updated

        if age > threshold:
            if lock_label == LABEL_REVISING:
                # Revising lock: just remove the lock, leave :pr-open.
                ok = _set_labels(issue_num, remove=[LABEL_REVISING], log_prefix="cai audit")
            elif lock_label == LABEL_APPLYING:
                # Applying lock: roll back to :raised (no provenance check needed).
                ok = _set_labels(
                    issue_num,
                    add=[LABEL_RAISED],
                    remove=[LABEL_APPLYING],
                    log_prefix="cai audit",
                )
            elif lock_label == LABEL_LOCKED:
                # Ownership lock: orthogonal to FSM state — only strip the
                # :locked label and delete any cai-lock claim comments.
                # The FSM state label (:in-progress, :revising, …) stays
                # untouched so the regular per-state TTL still applies.
                ok = _set_labels(
                    issue_num,
                    remove=[LABEL_LOCKED],
                    log_prefix="cai audit",
                )
                if ok:
                    _delete_lock_claim_comments(issue_num)
            else:
                # In-progress lock: roll back to :refined.
                # Audit-originated issues carry an "audit" source tag so they
                # remain filterable; they no longer need a separate rollback path.
                ok = _set_labels(
                    issue_num,
                    add=[LABEL_REFINED],
                    remove=[LABEL_IN_PROGRESS],
                    log_prefix="cai audit",
                )
            if ok:
                rolled_back.append(issue)
                log_run(
                    "audit",
                    action="stale_lock_rollback",
                    issue=issue_num,
                    lock_label=lock_label,
                    stale_hours=f"{age / 3600:.1f}",
                )
                print(
                    f"[cai audit] rolled back #{issue_num} "
                    f"(removed {lock_label}, stale {age / 3600:.1f}h)",
                    flush=True,
                )

    return rolled_back


def _rollback_stale_pr_locks(*, immediate: bool = False) -> list[dict]:
    """PR-side counterpart for stale ``auto-improve:locked`` cleanup.

    The issue-side :func:`_rollback_stale_in_progress` only queries
    ``gh issue list``, so PRs whose dispatcher crashed mid-handler can
    strand the ownership lock indefinitely — there is no TTL sweep and
    no restart sweep for them. This helper closes that gap: it lists
    open PRs carrying ``LABEL_LOCKED``, and for each one older than
    ``_STALE_LOCKED_HOURS`` (or every one when ``immediate=True``), it
    strips the label and deletes any ``<!-- cai-lock ... -->`` claim
    comments. The FSM pipeline label (``pr:reviewing-code`` etc.) is
    orthogonal and left untouched — only the ownership lock is cleared.

    Returns the list of PRs that were rolled back.
    """
    try:
        prs = _gh_json([
            "pr", "list",
            "--repo", REPO,
            "--label", LABEL_LOCKED,
            "--state", "open",
            "--json", "number,title,updatedAt,createdAt,labels",
            "--limit", "100",
        ]) or []
    except subprocess.CalledProcessError as e:
        print(
            f"[cai audit] gh pr list ({LABEL_LOCKED}) failed:\n{e.stderr}",
            file=sys.stderr,
        )
        return []

    if not prs:
        return []

    now = datetime.now(timezone.utc).timestamp()
    threshold = 0 if immediate else _STALE_LOCKED_HOURS * 3600
    rolled_back: list[dict] = []

    for pr in prs:
        pr_num = pr["number"]
        # Age comes from the oldest cai-lock claim comment — the
        # authoritative acquisition marker. Using PR ``updatedAt`` here is
        # unsafe: GitHub bumps it for CI check-runs, head-branch pushes,
        # merge-conflict recomputation, and every losing _acquire_remote_lock
        # race (post+delete of a claim comment), so a lock set hours ago
        # can look "fresh" forever.
        claim_age = _lock_claim_age_seconds(pr_num, now)
        if claim_age is not None:
            age = claim_age
        else:
            # Anomaly: :locked label with no claim comment (crashed acquire
            # or manually deleted claim). Treat as stale so the watchdog
            # can strip the orphan label.
            age = float("inf")

        if age <= threshold:
            continue

        ok = _set_pr_labels(
            pr_num,
            remove=[LABEL_LOCKED],
            log_prefix="cai audit",
        )
        if not ok:
            continue

        # Delete this PR's cai-lock claim comments, if any. Same endpoint
        # as the issue path — GitHub posts PR-level issue comments at
        # /repos/.../issues/<N>/comments.
        _delete_lock_claim_comments(pr_num)

        rolled_back.append(pr)
        log_run(
            "audit",
            action="stale_lock_rollback",
            pr=pr_num,
            lock_label=LABEL_LOCKED,
            stale_hours=f"{age / 3600:.1f}",
        )
        print(
            f"[cai audit] rolled back PR #{pr_num} "
            f"(removed {LABEL_LOCKED}, stale {age / 3600:.1f}h)",
            flush=True,
        )

    return rolled_back
