"""cai_lib.watchdog — stale-lock recovery for the auto-improve pipeline.

Contains the single deterministic watchdog that rolls back issues whose
``:in-progress`` or ``:revising`` lock has outlived its TTL (or every
such issue on container restart). The FSM in :mod:`cai_lib.fsm` is the
source of truth for issue state; this module is the paranoia backstop
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
    LABEL_RAISED,
    LABEL_REFINED,
    LABEL_AUDIT_RAISED,
    _STALE_IN_PROGRESS_HOURS,
    _STALE_REVISING_HOURS,
    _STALE_APPLYING_HOURS,
)
from cai_lib.github import _gh_json, _set_labels
from cai_lib.logging_utils import log_run


def _rollback_stale_in_progress(*, immediate: bool = False) -> list[dict]:
    """Deterministic rollback: :in-progress or :revising issues with no recent activity.

    When ``immediate=True`` every locked issue is rolled back regardless of age
    (used by ``cmd_cycle`` on container restart where all in-flight locks are
    guaranteed to be orphaned).

    Returns the list of issues that were rolled back.
    """
    all_issues = []
    for lock_label in (LABEL_IN_PROGRESS, LABEL_REVISING, LABEL_APPLYING):
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

    # Read the log tail to find the most recent [fix] line per issue.
    fix_timestamps: dict[int, float] = {}
    if LOG_PATH.exists():
        try:
            lines = LOG_PATH.read_text().splitlines()[-200:]
        except Exception:
            lines = []
        for line in lines:
            if "[fix]" not in line and "[revise]" not in line:
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
            else:
                # In-progress lock: roll back to the appropriate label.
                # Check originating label: audit-raised go back to
                # :audit-raised; all others go back to :refined.
                issue_labels = {lbl["name"] for lbl in issue.get("labels", [])}
                if LABEL_AUDIT_RAISED in issue_labels:
                    raised_label = LABEL_AUDIT_RAISED
                else:
                    raised_label = LABEL_REFINED
                ok = _set_labels(
                    issue_num,
                    add=[raised_label],
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
