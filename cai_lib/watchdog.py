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
)
from cai_lib.logging_utils import log_run


def _rollback_stale_in_progress(*, immediate: bool = False) -> list[dict]:
    """Deterministic rollback: :in-progress, :revising, or :applying issues with no recent activity.

    When ``immediate=True`` every locked issue is rolled back regardless of age
    (used by ``cmd_cycle`` on container restart where all in-flight locks are
    guaranteed to be orphaned).

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
                    try:
                        comments = _gh_json([
                            "api", f"/repos/{REPO}/issues/{issue_num}/comments",
                            "--paginate",
                        ]) or []
                    except subprocess.CalledProcessError:
                        comments = []
                    for c in comments:
                        body = c.get("body", "") or ""
                        if CAI_LOCK_COMMENT_RE.search(body):
                            cid = c.get("id")
                            if cid is not None:
                                _delete_issue_comment(int(cid),
                                                      log_prefix="cai audit")
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
        # No PR-side log-marker parsing (unlike the issue rollback which
        # scans [fix]/[revise]/[maintain] lines). The PR dispatcher spans
        # many action markers ([rebase], [fix-ci], [merge], [review_docs],
        # …) keyed by pr=<N>, and :locked is a brief ownership window —
        # falling back to updatedAt mirrors how the issue path falls back
        # when no log line is found, and the short _STALE_LOCKED_HOURS
        # TTL bounds any over-extension from a stray comment.
        try:
            updated = datetime.strptime(
                pr["updatedAt"], "%Y-%m-%dT%H:%M:%SZ"
            ).replace(tzinfo=timezone.utc).timestamp()
        except (ValueError, KeyError):
            updated = 0
        age = now - updated

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
        try:
            comments = _gh_json([
                "api", f"/repos/{REPO}/issues/{pr_num}/comments",
                "--paginate",
            ]) or []
        except subprocess.CalledProcessError:
            comments = []
        for c in comments:
            body = c.get("body", "") or ""
            if CAI_LOCK_COMMENT_RE.search(body):
                cid = c.get("id")
                if cid is not None:
                    _delete_issue_comment(int(cid), log_prefix="cai audit")

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
