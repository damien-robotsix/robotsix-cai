"""cmd_misc — small/isolated cmd_* functions extracted from cai.py."""

import re
import subprocess
import sys

from datetime import datetime, timezone
from pathlib import Path

from cai_lib.config import *  # noqa: F403,F401
from cai_lib.logging_utils import log_run
from cai_lib.audit.cost import _load_cost_log, _load_outcome_counts
from cai_lib import transcript_sync
from cai_lib.subprocess_utils import _run, _run_claude_p
from cai_lib.github import (
    _gh_json, _set_labels, _transcript_dir_is_empty,
    _find_linked_pr, _recover_stale_pr_open,
)
from cai_lib.issues import close_completed_parents


# ---------------------------------------------------------------------------
# Canonical set of valid cai-managed labels on issues.  Any cai-owned label
# found on an open issue that is NOT in this set is considered stale and will
# be removed by _issue_label_sweep().
# ---------------------------------------------------------------------------
_ALL_MANAGED_ISSUE_LABELS: frozenset[str] = frozenset({
    LABEL_RAISED, LABEL_IN_PROGRESS, LABEL_PR_OPEN,
    LABEL_MERGED, LABEL_SOLVED,
    LABEL_NEEDS_EXPLORATION, LABEL_REFINED, LABEL_REVISING,
    LABEL_PARENT, LABEL_PLANNED, LABEL_PLAN_APPROVED,
    LABEL_REFINING, LABEL_PLANNING, LABEL_APPLYING, LABEL_APPLIED,
    LABEL_HUMAN_NEEDED, LABEL_PR_HUMAN_NEEDED,
    LABEL_TRIAGING, LABEL_MERGE_BLOCKED,
    LABEL_HUMAN_SOLVED, LABEL_KIND_CODE, LABEL_KIND_MAINTENANCE,
    # Opus one-shot marker set by `cai rescue` when it escalates a
    # stuck issue to Opus-backed implement. Must survive the hourly
    # sweep so the next rescue pass can detect the one-shot has
    # already been burned and refuse a second escalation (#944).
    LABEL_OPUS_ATTEMPTED,
    # Supplementary plan-needs-review marker (#1128) — set alongside
    # :human-needed by `handle_plan_gate` when cai-select emitted
    # requires_human_review=true. Must survive the hourly sweep so
    # `cai rescue` keeps skipping the issue until the admin resumes
    # it; auto-cleared on any `human_to_*` resume transition via
    # the transition's `labels_remove`.
    LABEL_PLAN_NEEDS_REVIEW,
    # Rescue-already-attempted marker — set by `cai rescue` when a
    # tick finishes without resuming a parked target. Must survive
    # the hourly sweep so subsequent rescue passes keep skipping the
    # target; auto-cleared on any `human_to_*` resume transition via
    # the transition's `labels_remove`. Same lifecycle pattern as
    # LABEL_PLAN_NEEDS_REVIEW above.
    LABEL_RESCUE_ATTEMPTED,
    "auto-improve", "audit", "check-workflows", "check-workflows:raised",
})

# Prefixes that identify a label as cai-owned on an issue.
# NO trailing colons — matching uses `lbl == p or lbl.startswith(p + ":")`.
_MANAGED_ISSUE_PREFIXES: tuple[str, ...] = (
    "auto-improve",
    "audit",
    "check-workflows",
    "merge-blocked",
    "human",          # matches human:solved (and any future human:* labels)
    "kind",           # matches kind:code, kind:maintenance
    "pr",             # pr:* labels are PR-only; stale if found on an issue
    "needs-human-review",
    "needs-workflow-review",  # PR-only; stale if found on an issue
)


def _issue_label_sweep() -> tuple[int, int]:
    """Remove stale/deprecated cai-managed labels from open issues.

    Fetches open issues bearing 'auto-improve', 'audit', or 'check-workflows'
    labels, identifies any cai-owned label not in _ALL_MANAGED_ISSUE_LABELS,
    and removes those stale labels via _set_labels().

    Returns (issues_scanned, labels_removed).
    """
    seen: dict[int, list[str]] = {}
    for base_label in ("auto-improve", "audit", "check-workflows"):
        try:
            batch = _gh_json([
                "issue", "list",
                "--repo", REPO,
                "--label", base_label,
                "--state", "open",
                "--json", "number,labels",
                "--limit", "200",
            ]) or []
        except subprocess.CalledProcessError as e:
            print(f"[cai sweep] gh issue list failed for {base_label!r}:\n{e.stderr}",
                  file=sys.stderr)
            continue
        for issue in batch:
            num = issue["number"]
            if num not in seen:
                seen[num] = [lbl["name"] for lbl in issue.get("labels", [])]

    issues_scanned = len(seen)
    labels_removed = 0
    for num, label_names in seen.items():
        stale = [
            lbl for lbl in label_names
            if any(lbl == p or lbl.startswith(p + ":") for p in _MANAGED_ISSUE_PREFIXES)
            and lbl not in _ALL_MANAGED_ISSUE_LABELS
        ]
        if stale:
            _set_labels(num, remove=stale, log_prefix="cai sweep")
            labels_removed += len(stale)
            print(f"[cai sweep] #{num}: removed stale label(s) {stale}", flush=True)

    print(f"[cai sweep] scanned {issues_scanned} issue(s), removed {labels_removed} stale label(s)",
          flush=True)
    return issues_scanned, labels_removed


# ---------------------------------------------------------------------------
# init
# ---------------------------------------------------------------------------

def cmd_init(args) -> int:
    """Seed the loop with a smoke test, only if nothing exists yet."""
    if not _transcript_dir_is_empty():
        print("[cai init] transcripts already present; skipping smoke test", flush=True)
        log_run("init", ran_smoke_test=False, exit=0)
        return 0

    print("[cai init] no prior transcripts; running smoke test to seed loop", flush=True)
    result = _run_claude_p(
        ["claude", "-p", SMOKE_PROMPT],
        category="init",
    )
    # _run_claude_p forces capture_output=True so the smoke test no
    # longer streams to the terminal. Print the result text now so
    # the user still sees that the loop seeded successfully.
    if result.stdout:
        print(result.stdout, flush=True)
    rc = result.returncode
    if rc != 0:
        print(f"[cai init] smoke test failed (exit {rc})", flush=True)
    log_run("init", ran_smoke_test=True, exit=rc)
    return rc


# ---------------------------------------------------------------------------
# verify
# ---------------------------------------------------------------------------

def cmd_verify(args) -> int:
    """Walk :pr-open issues and transition labels based on PR state."""
    print("[cai verify] checking pr-open issues", flush=True)
    _issue_label_sweep()
    try:
        issues = _gh_json([
            "issue", "list",
            "--repo", REPO,
            "--label", LABEL_PR_OPEN,
            "--state", "open",
            "--json", "number,title,labels",
            "--limit", "100",
        ]) or []
    except subprocess.CalledProcessError as e:
        print(f"[cai verify] gh issue list failed:\n{e.stderr}", file=sys.stderr)
        log_run("verify", repo=REPO, checked=0, transitioned=0, exit=1)
        return 1

    transitioned = 0
    pr_open_issue_nums = {i["number"] for i in issues}

    # Handle MERGED transitions inline; CLOSED and no-linked-PR recovery uses the shared helper.
    remaining = []
    for issue in issues:
        num = issue["number"]
        pr = _find_linked_pr(num)
        if pr is None:
            remaining.append(issue)
            continue
        state = (pr.get("state") or "").upper()
        if state == "MERGED":
            _set_labels(num, add=[LABEL_MERGED], remove=[LABEL_PR_OPEN, LABEL_MERGE_BLOCKED, LABEL_REVISING], log_prefix="cai verify")
            print(f"[cai verify] #{num}: PR #{pr['number']} merged → :merged", flush=True)
            transitioned += 1
        elif state == "CLOSED":
            remaining.append(issue)
        else:
            print(f"[cai verify] #{num}: PR #{pr['number']} still {state}", flush=True)

    transitioned += len(_recover_stale_pr_open(remaining, log_prefix="cai verify"))

    # Recovery: find open auto-improve PRs whose linked issue is missing
    # the :pr-open label.  This heals issues where the label transition
    # in cmd_implement step 10 failed silently.
    try:
        open_prs = _gh_json([
            "pr", "list",
            "--repo", REPO,
            "--state", "open",
            "--base", "main",
            "--json", "number,headRefName",
            "--limit", "100",
        ]) or []
    except subprocess.CalledProcessError:
        open_prs = []

    for opr in open_prs:
        branch = opr.get("headRefName", "")
        m = re.match(r"^auto-improve/(\d+)-", branch)
        if not m:
            continue
        issue_num = int(m.group(1))
        if issue_num in pr_open_issue_nums:
            continue
        # Check the issue's current state.
        try:
            iss = _gh_json([
                "issue", "view", str(issue_num),
                "--repo", REPO,
                "--json", "state,labels",
            ])
        except subprocess.CalledProcessError:
            continue
        if (iss.get("state") or "").upper() != "OPEN":
            continue
        iss_labels = {l["name"] for l in iss.get("labels", [])}  # noqa: E741
        if LABEL_PR_OPEN in iss_labels:
            continue
        # Issue is open, has an open PR, but missing :pr-open — recover.
        remove = [l for l in (LABEL_IN_PROGRESS, LABEL_REFINED, LABEL_PLANNED, LABEL_PLAN_APPROVED, LABEL_APPLYING, LABEL_APPLIED, LABEL_RAISED) if l in iss_labels]  # noqa: E741
        if _set_labels(issue_num, add=[LABEL_PR_OPEN], remove=remove, log_prefix="cai verify"):
            print(
                f"[cai verify] recovered #{issue_num}: added :pr-open "
                f"(open PR #{opr['number']} on branch {branch})",
                flush=True,
            )
            transitioned += 1

    # Check parent issues for completion: close parents whose
    # sub-issues are all closed.
    close_completed_parents(log_prefix="cai verify")

    print(f"[cai verify] done ({transitioned} transitioned)", flush=True)
    log_run("verify", repo=REPO, checked=len(issues), transitioned=transitioned, exit=0)
    return 0


# ---------------------------------------------------------------------------
# cost-report — human-readable view of /var/log/cai/cai-cost.jsonl
# ---------------------------------------------------------------------------


def cmd_cost_report(args) -> int:
    """Print a human-readable cost report from the cost log.

    Reads `/var/log/cai/cai-cost.jsonl` (written by `_run_claude_p`),
    aggregates by `--by` (category | agent | day), and prints two
    fixed-width tables: the per-group totals and the top-N most
    expensive individual invocations.

    Invoked from the host via the existing alias documented in
    README.md (`docker compose ... exec cai python /app/cai.py`):

        cai cost-report
        cai cost-report --days 30 --top 20 --by agent
    """
    transcript_sync.pull_cost()
    rows = _load_cost_log(days=args.days)
    if not rows:
        print(
            f"[cai cost-report] no rows in {COST_LOG_PATH} for the "
            f"last {args.days} day(s)"
        )
        return 0

    # Group rows by the requested key.
    def group_key(r: dict) -> str:
        if args.by == "category":
            return r.get("category") or "(unknown)"
        if args.by == "agent":
            return r.get("agent") or "(none)"
        if args.by == "day":
            ts = r.get("ts") or ""
            return ts.split("T", 1)[0] or "(unknown)"
        return "(unknown)"

    groups: dict[str, dict] = {}
    grand_total = 0.0
    grand_in = 0
    grand_out = 0
    for r in rows:
        key = group_key(r)
        try:
            cost = float(r.get("cost_usd") or 0.0)
        except (TypeError, ValueError):
            cost = 0.0
        in_t = int(r.get("input_tokens") or 0)
        out_t = int(r.get("output_tokens") or 0)
        bucket = groups.setdefault(
            key, {"calls": 0, "cost": 0.0, "in": 0, "out": 0},
        )
        bucket["calls"] += 1
        bucket["cost"] += cost
        bucket["in"] += in_t
        bucket["out"] += out_t
        grand_total += cost
        grand_in += in_t
        grand_out += out_t

    # Header.
    print(
        f"\n=== Cost report — last {args.days} day(s), "
        f"{len(rows)} invocations, total ${grand_total:.4f} ===\n"
    )

    # Per-group totals (sorted by cost descending).
    sorted_groups = sorted(
        groups.items(), key=lambda kv: -kv[1]["cost"],
    )
    key_width = max(len(args.by), max(len(k) for k in groups) if groups else 0)
    key_width = max(key_width, 12)
    header = (
        f"{args.by:<{key_width}}  {'calls':>6}  {'cost':>10}  "
        f"{'share':>7}  {'mean':>10}  {'in_tok':>10}  {'out_tok':>10}"
    )
    print(header)
    print("-" * len(header))
    for key, b in sorted_groups:
        share = (b["cost"] / grand_total * 100.0) if grand_total else 0.0
        mean = b["cost"] / b["calls"] if b["calls"] else 0.0
        print(
            f"{key:<{key_width}}  {b['calls']:>6}  ${b['cost']:>9.4f}  "
            f"{share:>6.1f}%  ${mean:>9.4f}  {b['in']:>10}  {b['out']:>10}"
        )
    print(
        f"{'TOTAL':<{key_width}}  {len(rows):>6}  ${grand_total:>9.4f}  "
        f"{100.0:>6.1f}%  "
        f"${(grand_total / len(rows) if rows else 0):>9.4f}  "
        f"{grand_in:>10}  {grand_out:>10}"
    )

    # Top-N most expensive invocations.
    top = sorted(
        rows,
        key=lambda r: float(r.get("cost_usd") or 0.0),
        reverse=True,
    )[: args.top]
    print(f"\n--- Top {len(top)} most expensive invocations ---\n")
    top_header = (
        f"{'ts':<20}  {'category':<14}  {'agent':<20}  "
        f"{'cost':>10}  {'turns':>5}  {'in_tok':>10}  {'out_tok':>10}"
    )
    print(top_header)
    print("-" * len(top_header))
    for r in top:
        try:
            cost = float(r.get("cost_usd") or 0.0)
        except (TypeError, ValueError):
            cost = 0.0
        ts = (r.get("ts") or "")[:19]
        cat = (r.get("category") or "")[:14]
        ag = (r.get("agent") or "")[:20]
        turns = r.get("num_turns") or 0
        in_t = int(r.get("input_tokens") or 0)
        out_t = int(r.get("output_tokens") or 0)
        print(
            f"{ts:<20}  {cat:<14}  {ag:<20}  ${cost:>9.4f}  "
            f"{turns:>5}  {in_t:>10}  {out_t:>10}"
        )

    # Last-hour snapshot — cost per agent. Useful for spotting a
    # runaway subagent right now, independent of the `--days` window.
    hour_cutoff = datetime.now(timezone.utc).timestamp() - 3600
    hour_groups: dict[str, dict] = {}
    hour_total = 0.0
    hour_calls = 0
    for r in rows:
        ts = r.get("ts") or ""
        try:
            row_ts = datetime.strptime(
                ts, "%Y-%m-%dT%H:%M:%SZ",
            ).replace(tzinfo=timezone.utc).timestamp()
        except ValueError:
            continue
        if row_ts < hour_cutoff:
            continue
        try:
            cost = float(r.get("cost_usd") or 0.0)
        except (TypeError, ValueError):
            cost = 0.0
        in_t = int(r.get("input_tokens") or 0)
        out_t = int(r.get("output_tokens") or 0)
        ag = r.get("agent") or "(none)"
        bucket = hour_groups.setdefault(
            ag, {"calls": 0, "cost": 0.0, "in": 0, "out": 0},
        )
        bucket["calls"] += 1
        bucket["cost"] += cost
        bucket["in"] += in_t
        bucket["out"] += out_t
        hour_total += cost
        hour_calls += 1

    print(
        f"\n--- Last hour snapshot — {hour_calls} invocations, "
        f"total ${hour_total:.4f} ---\n"
    )
    if not hour_groups:
        print("(no invocations in the last hour)")
    else:
        hour_key_width = max(
            len("agent"), max(len(k) for k in hour_groups),
        )
        hour_key_width = max(hour_key_width, 12)
        hour_header = (
            f"{'agent':<{hour_key_width}}  {'calls':>6}  {'cost':>10}  "
            f"{'share':>7}  {'mean':>10}  {'in_tok':>10}  {'out_tok':>10}"
        )
        print(hour_header)
        print("-" * len(hour_header))
        for key, b in sorted(
            hour_groups.items(), key=lambda kv: -kv[1]["cost"],
        ):
            share = (b["cost"] / hour_total * 100.0) if hour_total else 0.0
            mean = b["cost"] / b["calls"] if b["calls"] else 0.0
            print(
                f"{key:<{hour_key_width}}  {b['calls']:>6}  "
                f"${b['cost']:>9.4f}  {share:>6.1f}%  "
                f"${mean:>9.4f}  {b['in']:>10}  {b['out']:>10}"
            )
    print()

    # Category success rates from outcome log.
    print("--- Category Success Rates (trailing 90 days) ---\n")
    cat_counts = _load_outcome_counts(days=90)
    if cat_counts:
        cat_width = max(12, max(len(k) for k in cat_counts))
        cat_header = (
            f"{'category':<{cat_width}}  {'attempts':>8}  "
            f"{'solved':>6}  {'rate':>7}  {'flag':>6}"
        )
        print(cat_header)
        print("-" * len(cat_header))
        for cat, c in sorted(cat_counts.items(), key=lambda kv: kv[1]["total"], reverse=True):
            rate = c["solved"] / c["total"] if c["total"] else 0.0
            flag = "⚠ LOW" if rate < 0.40 else ""
            print(
                f"{cat:<{cat_width}}  {c['total']:>8}  "
                f"{c['solved']:>6}  {rate:>6.0%}  {flag:>6}"
            )
    else:
        print("(no outcome data yet)")
    print()
    return 0


# ---------------------------------------------------------------------------
# test
# ---------------------------------------------------------------------------


def cmd_test(args) -> int:
    """Run the project test suite."""
    result = _run(
        [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"],
        cwd=str(Path(__file__).resolve().parent.parent),
    )
    return result.returncode
