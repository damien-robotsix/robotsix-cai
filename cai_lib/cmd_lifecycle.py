"""cai_lib.cmd_lifecycle — pipeline state-transition helpers.

This module contains deterministic (no-LLM) lifecycle helpers that
manage label transitions for issues in the auto-improve pipeline.
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
    LABEL_REFINED,
    LABEL_RAISED,
    LABEL_AUDIT_RAISED,
    LABEL_NEEDS_SPIKE,
    LABEL_PR_REVIEWING_CODE,
    LABEL_PR_REVISION_PENDING,
    LABEL_PR_REVIEWING_DOCS,
    _STALE_IN_PROGRESS_HOURS,
    _STALE_REVISING_HOURS,
)
from cai_lib.github import _gh_json, _set_labels, _set_pr_labels, _issue_has_label
from cai_lib.logging_utils import log_run


# Label retired in favour of folding human submissions directly into
# :raised. Helper below migrates any open issue still carrying it.
_LEGACY_HUMAN_SUBMITTED_LABEL = "human:submitted"


def _migrate_legacy_human_submitted() -> int:
    """Relabel open issues carrying the retired ``human:submitted`` to ``:raised``.

    Idempotent — safe to invoke at the top of every cmd_refine run; once
    the queue is empty the function becomes a single gh call that does
    nothing. Returns the number of issues relabelled this invocation.
    """
    try:
        issues = _gh_json([
            "issue", "list",
            "--repo", REPO,
            "--label", _LEGACY_HUMAN_SUBMITTED_LABEL,
            "--state", "open",
            "--json", "number,labels",
            "--limit", "100",
        ]) or []
    except subprocess.CalledProcessError as e:
        print(
            f"[cai refine] migration gh issue list failed:\n{e.stderr}",
            file=sys.stderr,
        )
        return 0

    migrated = 0
    for issue in issues:
        num = issue["number"]
        labels = {lbl["name"] for lbl in issue.get("labels", [])}
        add = [] if LABEL_RAISED in labels else [LABEL_RAISED]
        if _set_labels(
            num,
            add=add,
            remove=[_LEGACY_HUMAN_SUBMITTED_LABEL],
            log_prefix="cai refine",
        ):
            migrated += 1
            print(
                f"[cai refine] migrated #{num}: human:submitted -> :raised",
                flush=True,
            )
    return migrated


# Legacy PR pipeline labels retired when PRState absorbed CI health
# and docs-review as first-class states. Mapping reflects the FSM
# meaning of each old label:
#   - pr:edited           → REVIEWING_CODE  (new SHA pending review)
#   - pr:reviewed-reject  → REVISION_PENDING (review found issues)
#   - pr:reviewed-accept  → REVIEWING_DOCS  (code clean, docs next)
#   - pr:documented       → REVIEWING_DOCS  (docs reviewed; the HEAD-
#                           scoped docs-clean comment is what actually
#                           gates merge now)
_LEGACY_PR_LABEL_MAP = {
    "pr:edited":          LABEL_PR_REVIEWING_CODE,
    "pr:reviewed-reject": LABEL_PR_REVISION_PENDING,
    "pr:reviewed-accept": LABEL_PR_REVIEWING_DOCS,
    "pr:documented":      LABEL_PR_REVIEWING_DOCS,
}


def _migrate_legacy_pr_pipeline_labels() -> int:
    """Relabel open PRs carrying retired ``pr:*`` labels to new PRState labels.

    Idempotent — safe to call at the top of every ``cmd_cycle`` tick.
    Returns the number of PRs relabelled this invocation.
    """
    migrated = 0
    for legacy, replacement in _LEGACY_PR_LABEL_MAP.items():
        try:
            prs = _gh_json([
                "pr", "list",
                "--repo", REPO,
                "--state", "open",
                "--label", legacy,
                "--json", "number,labels",
                "--limit", "100",
            ]) or []
        except subprocess.CalledProcessError as e:
            print(
                f"[cai cycle] legacy-PR migration gh pr list failed for "
                f"{legacy!r}:\n{e.stderr}",
                file=sys.stderr,
            )
            continue
        for pr in prs:
            num = pr["number"]
            labels = {lbl["name"] for lbl in pr.get("labels", [])}
            add = [] if replacement in labels else [replacement]
            if _set_pr_labels(
                num,
                add=add,
                remove=[legacy],
                log_prefix="cai cycle",
            ):
                migrated += 1
                print(
                    f"[cai cycle] migrated PR #{num}: {legacy} -> {replacement}",
                    flush=True,
                )
    return migrated


def _rollback_stale_in_progress(*, immediate: bool = False) -> list[dict]:
    """Deterministic rollback: :in-progress or :revising issues with no recent activity.

    When ``immediate=True`` every locked issue is rolled back regardless of age
    (used by ``cmd_cycle`` on container restart where all in-flight locks are
    guaranteed to be orphaned).

    Returns the list of issues that were rolled back.
    """
    all_issues = []
    for lock_label in (LABEL_IN_PROGRESS, LABEL_REVISING):
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
            if "[fix]" not in line and "[revise]" not in line and "[spike]" not in line:
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
        ttl_hours = _STALE_REVISING_HOURS if lock_label == LABEL_REVISING else _STALE_IN_PROGRESS_HOURS
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
            else:
                # In-progress lock: roll back to the appropriate label.
                # Check originating label: spike-provenance issues go back to
                # :needs-spike; audit-raised go back to :audit-raised; all
                # others go back to :refined.
                issue_labels = {lbl["name"] for lbl in issue.get("labels", [])}
                if LABEL_AUDIT_RAISED in issue_labels:
                    raised_label = LABEL_AUDIT_RAISED
                elif LABEL_NEEDS_SPIKE in issue_labels:
                    raised_label = LABEL_NEEDS_SPIKE
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


def _reconcile_fix(issue_number: int | None) -> str:
    """Reconcile an interrupted ``fix`` action."""
    if issue_number is None:
        return "not_started"

    # 1. Check for open PRs whose head matches auto-improve/<N>-*
    #    (single gh call — covers the "completed" case)
    prefix = f"auto-improve/{issue_number}-"
    try:
        open_prs = _gh_json([
            "pr", "list",
            "--repo", REPO,
            "--state", "open",
            "--json", "headRefName",
            "--limit", "50",
        ]) or []
    except subprocess.CalledProcessError:
        return "not_started"

    if any(pr.get("headRefName", "").startswith(prefix) for pr in open_prs):
        return "completed_externally"

    # 2. Check if a branch exists (no open PR).
    #    Use matching-refs to fetch only branches with our prefix — avoids
    #    paginating the entire branch list.
    try:
        refs = _gh_json([
            "api",
            f"repos/{REPO}/git/matching-refs/heads/{prefix}",
        ]) or []
    except (subprocess.CalledProcessError, Exception):
        refs = []

    if refs:
        return "partially_done"

    return "not_started"


def _reconcile_revise(issue_number: int | None) -> str:
    """Reconcile an interrupted ``revise`` action."""
    if issue_number is None:
        return "not_started"

    # 1. Check label state (1 gh call via _issue_has_label)
    has_revising = _issue_has_label(issue_number, LABEL_REVISING)

    # 2. Check for open PRs (1 gh call)
    prefix = f"auto-improve/{issue_number}-"
    try:
        open_prs = _gh_json([
            "pr", "list",
            "--repo", REPO,
            "--state", "open",
            "--json", "headRefName",
            "--limit", "50",
        ]) or []
    except subprocess.CalledProcessError:
        return "not_started"

    has_pr = any(pr.get("headRefName", "").startswith(prefix) for pr in open_prs)

    if not has_pr and not has_revising:
        return "not_started"
    if has_pr and not has_revising:
        # PR exists, :revising already removed → revision landed
        return "completed_externally"
    if has_pr and has_revising:
        # PR exists but still marked :revising → mid-flight
        return "partially_done"
    # has_revising but no PR — label orphan, treat as not started
    return "not_started"


def _reconcile_refine(issue_number: int | None) -> str:
    """Reconcile an interrupted ``refine`` action."""
    if issue_number is None:
        return "not_started"

    # Single gh call: fetch issue body
    try:
        issue = _gh_json([
            "issue", "view", str(issue_number),
            "--repo", REPO,
            "--json", "body",
        ])
    except subprocess.CalledProcessError:
        return "not_started"

    body = (issue or {}).get("body", "") or ""
    if "### Plan" in body:
        return "completed_externally"

    return "not_started"


def _reconcile_interrupted(cmd: str, target_type: str, target_id: int | None) -> str:
    """Classify how far an interrupted action got by inspecting remote state.

    Returns one of:
      - ``"not_started"``          — no remote side-effects detected
      - ``"partially_done"``       — branch pushed or label set, but action incomplete
      - ``"completed_externally"`` — PR opened or output already written

    Takes explicit args from the caller (the active-job file); does NOT read
    ``cai-active.json`` itself.  At most 2–3 ``gh`` subprocess calls.
    """
    _HANDLERS = {
        "implement": _reconcile_fix,
        "revise": _reconcile_revise,
        "refine": _reconcile_refine,
    }
    handler = _HANDLERS.get(cmd)
    if handler is None:
        return "not_started"
    return handler(target_id)
