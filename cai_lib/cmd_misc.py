"""cmd_misc — small/isolated cmd_* functions extracted from cai.py."""

import json
import re
import shutil
import subprocess
import sys
import time
import uuid

from datetime import datetime, timedelta, timezone
from pathlib import Path

from cai_lib.config import *  # noqa: F403,F401
from cai_lib.logging_utils import log_run
from cai_lib.audit.cost import _load_cost_log, _load_outcome_counts, _row_ts
from cai_lib.subprocess_utils import _run, _run_claude_p
from cai_lib.github import (
    _gh_json, _set_labels, _transcript_dir_is_empty,
    _find_linked_pr, _recover_stale_pr_open,
)
from cai_lib.issues import all_sub_issues_closed


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
    try:
        parent_issues = _gh_json([
            "issue", "list",
            "--repo", REPO,
            "--label", LABEL_PARENT,
            "--state", "open",
            "--json", "number",
            "--limit", "50",
        ]) or []
    except subprocess.CalledProcessError:
        parent_issues = []

    for _pass in range(2):
        for parent in parent_issues:
            if all_sub_issues_closed(parent["number"]) is True:
                _run(
                    ["gh", "issue", "close", str(parent["number"]),
                     "--repo", REPO,
                     "--comment",
                     "All sub-issues completed. Closing parent."],
                    capture_output=True,
                )
                print(
                    f"[cai verify] parent #{parent['number']}: "
                    f"all sub-issues done — closed",
                    flush=True,
                )
        # Re-fetch before second pass: parents closed in pass 1
        # should not be re-attempted.
        if _pass == 0:
            try:
                parent_issues = _gh_json([
                    "issue", "list",
                    "--repo", REPO,
                    "--label", LABEL_PARENT,
                    "--state", "open",
                    "--json", "number",
                    "--limit", "50",
                ]) or []
            except subprocess.CalledProcessError:
                parent_issues = []

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
# health-report
# ---------------------------------------------------------------------------


def cmd_health_report(args) -> int:
    """Automated pipeline health report with anomaly detection.

    Gathers cost trends, issue throughput, pipeline stalls, and fix
    quality metrics from existing data sources (_load_cost_log,
    _gh_json), formats them as GitHub-flavored markdown with
    traffic-light anomaly indicators, and optionally posts the report
    as a GitHub issue.

    With --dry-run the report is printed to stdout without posting.
    """
    t0 = time.monotonic()
    now_ts = datetime.now(timezone.utc).timestamp()
    sections: list[str] = []
    anomalies: list[str] = []

    # ------------------------------------------------------------------ #
    # 1. Cost Trends                                                       #
    # ------------------------------------------------------------------ #
    cost_status = "🟢"
    try:
        rows_14d = _load_cost_log(days=14)
        boundary = now_ts - 7 * 86400
        last_7d = [r for r in rows_14d if _row_ts(r) >= boundary]
        prior_7d = [r for r in rows_14d if _row_ts(r) < boundary]

        def _cost(r: dict) -> float:
            try:
                return float(r.get("cost_usd") or 0.0)
            except (TypeError, ValueError):
                return 0.0

        last_7d_total = sum(_cost(r) for r in last_7d)
        prior_7d_total = sum(_cost(r) for r in prior_7d)

        if not rows_14d:
            cost_section_body = "_No cost data available._"
        elif not prior_7d:
            cost_section_body = (
                f"- Last 7d total: **${last_7d_total:.4f}**\n"
                "- Prior 7d: _Insufficient history (need ≥2 weeks of cost data)_"
            )
        else:
            wow_pct = (
                ((last_7d_total - prior_7d_total) / prior_7d_total * 100)
                if prior_7d_total > 0 else 0.0
            )
            if prior_7d_total > 0 and last_7d_total > 1.5 * prior_7d_total:
                cost_status = "🔴"
                anomalies.append(
                    f"🔴 **Cost spike**: last-7d ${last_7d_total:.4f} is "
                    f"{wow_pct:+.1f}% vs prior-7d ${prior_7d_total:.4f}"
                )

            # Per-agent breakdown
            def _by_agent(rows: list[dict]) -> dict:
                agg: dict[str, float] = {}
                for r in rows:
                    agent = r.get("agent") or "(none)"
                    agg[agent] = agg.get(agent, 0.0) + _cost(r)
                return agg

            last_by_agent = _by_agent(last_7d)
            prior_by_agent = _by_agent(prior_7d)
            all_agents = sorted(
                set(last_by_agent) | set(prior_by_agent),
                key=lambda a: -last_by_agent.get(a, 0.0),
            )

            rows_md = []
            for agent in all_agents:
                l = last_by_agent.get(agent, 0.0)  # noqa: E741
                p = prior_by_agent.get(agent, 0.0)
                delta = ((l - p) / p * 100) if p > 0 else float("nan")
                delta_str = f"{delta:+.1f}%" if p > 0 else "n/a"
                rows_md.append(
                    f"| `{agent}` | ${l:.4f} | ${p:.4f} | {delta_str} |"
                )

            agent_table = (
                "| Agent | Last 7d | Prior 7d | WoW Δ |\n"
                "|-------|---------|----------|-------|\n"
                + "\n".join(rows_md)
            )
            wow_line = f"{wow_pct:+.1f}%"
            cost_section_body = (
                f"- **Last 7d total**: ${last_7d_total:.4f}\n"
                f"- **Prior 7d total**: ${prior_7d_total:.4f}\n"
                f"- **WoW Δ**: {wow_line}\n\n"
                f"{agent_table}"
            )
    except Exception as exc:
        cost_section_body = f"⚠️ Data unavailable ({exc})"
        cost_status = "🟡"

    sections.append(
        f"## {cost_status} Cost Trends\n\n{cost_section_body}"
    )

    # ------------------------------------------------------------------ #
    # 2. Issue Throughput                                                  #
    # ------------------------------------------------------------------ #
    throughput_status = "🟢"
    label_states = [
        ("raised", LABEL_RAISED),
        ("refined", LABEL_REFINED),
        ("planned", LABEL_PLANNED),
        ("plan-approved", LABEL_PLAN_APPROVED),
        ("in-progress", LABEL_IN_PROGRESS),
        ("pr-open", LABEL_PR_OPEN),
        ("merged", LABEL_MERGED),
        ("revising", LABEL_REVISING),
    ]
    counts: dict[str, int] = {}
    try:
        for name, label in label_states:
            items = _gh_json(
                ["issue", "list", "--repo", REPO,
                 "--label", label, "--state", "open",
                 "--json", "number", "--limit", "200"]
            )
            counts[name] = len(items) if items else 0
    except Exception:
        throughput_status = "🟡"
        counts = {name: -1 for name, _ in label_states}

    header_row = "| " + " | ".join(n for n, _ in label_states) + " |"
    sep_row = "|" + "|".join("---" for _ in label_states) + "|"
    val_row = "| " + " | ".join(
        str(counts.get(n, "?")) for n, _ in label_states
    ) + " |"
    throughput_table = "\n".join([header_row, sep_row, val_row])

    sections.append(
        f"## {throughput_status} Issue Queue\n\n{throughput_table}"
    )

    # ------------------------------------------------------------------ #
    # 3. Pipeline Stalls                                                   #
    # ------------------------------------------------------------------ #
    stall_status = "🟢"
    stall_lines: list[str] = []

    def _parse_gh_ts(ts_str: str) -> float:
        """Parse a GitHub updatedAt/createdAt timestamp to Unix time."""
        if not ts_str:
            return 0.0
        # Strip fractional seconds if present
        ts_str = ts_str.split(".")[0].rstrip("Z") + "Z"
        try:
            return datetime.strptime(
                ts_str, "%Y-%m-%dT%H:%M:%SZ",
            ).replace(tzinfo=timezone.utc).timestamp()
        except ValueError:
            return 0.0

    # In-progress stalls (>2h since update)
    try:
        ip_items = _gh_json(
            ["issue", "list", "--repo", REPO,
             "--label", LABEL_IN_PROGRESS, "--state", "open",
             "--json", "number,title,updatedAt", "--limit", "200"]
        ) or []
        stalled_ip = [
            i for i in ip_items
            if now_ts - _parse_gh_ts(i.get("updatedAt", "")) > 7200
        ]
        if stalled_ip:
            stall_status = "🟡"
            shown = stalled_ip[:10]
            for i in shown:
                stall_lines.append(
                    f"- `:in-progress` stall >2h: #{i['number']} {i.get('title', '')[:60]}"
                )
            remainder = len(stalled_ip) - len(shown)
            if remainder > 0:
                stall_lines.append(f"  _…and {remainder} more_")
    except Exception as exc:
        stall_lines.append(f"⚠️ `:in-progress` data unavailable ({exc})")

    # Refined stalls (>5 days since update)
    try:
        ref_items = _gh_json(
            ["issue", "list", "--repo", REPO,
             "--label", LABEL_REFINED, "--state", "open",
             "--json", "number,title,updatedAt", "--limit", "200"]
        ) or []
        stalled_ref = [
            i for i in ref_items
            if now_ts - _parse_gh_ts(i.get("updatedAt", "")) > 5 * 86400
        ]
        if stalled_ref:
            if stall_status == "🟢":
                stall_status = "🟡"
            shown = stalled_ref[:10]
            for i in shown:
                stall_lines.append(
                    f"- `:refined` stall >5d: #{i['number']} {i.get('title', '')[:60]}"
                )
            remainder = len(stalled_ref) - len(shown)
            if remainder > 0:
                stall_lines.append(f"  _…and {remainder} more_")
    except Exception as exc:
        stall_lines.append(f"⚠️ `:refined` data unavailable ({exc})")

    # Merged stalls (>14 days since update — confirm not run)
    try:
        merged_items = _gh_json(
            ["issue", "list", "--repo", REPO,
             "--label", LABEL_MERGED, "--state", "open",
             "--json", "number,title,updatedAt", "--limit", "200"]
        ) or []
        stalled_merged = [
            i for i in merged_items
            if now_ts - _parse_gh_ts(i.get("updatedAt", "")) > 14 * 86400
        ]
        if len(stalled_merged) > 5:
            stall_status = "🟡"
            anomalies.append(
                f"🟡 **Confirm backlog**: {len(stalled_merged)} issues in "
                "`:merged` state >14d without confirmation"
            )
        if stalled_merged:
            shown = stalled_merged[:10]
            for i in shown:
                stall_lines.append(
                    f"- `:merged` stall >14d: #{i['number']} {i.get('title', '')[:60]}"
                )
            remainder = len(stalled_merged) - len(shown)
            if remainder > 0:
                stall_lines.append(f"  _…and {remainder} more_")
    except Exception as exc:
        stall_lines.append(f"⚠️ `:merged` data unavailable ({exc})")

    # Complete pipeline stall detection — no PRs opened in >72h?
    try:
        all_prs = _gh_json(
            ["pr", "list", "--repo", REPO, "--state", "all",
             "--json", "number,createdAt", "--limit", "200"]
        ) or []
        recent_prs = [
            pr for pr in all_prs
            if now_ts - _parse_gh_ts(pr.get("createdAt", "")) < 72 * 3600
        ]
        raised_count = counts.get("raised", 0)
        refined_count = counts.get("refined", 0)
        if not recent_prs and (raised_count + refined_count) > 0:
            stall_status = "🔴"
            anomalies.append(
                "🔴 **Pipeline stall**: no PRs opened in the last 72h, "
                f"but {raised_count + refined_count} issues are queued "
                "(`:raised` + `:refined`)"
            )
            stall_lines.append(
                "⚠️ No PRs opened in the last 72h — pipeline may be stalled"
            )
    except Exception as exc:
        stall_lines.append(f"⚠️ PR stall check unavailable ({exc})")

    stall_body = "\n".join(stall_lines) if stall_lines else "_No stalls detected._"
    sections.append(
        f"## {stall_status} Pipeline Stalls\n\n{stall_body}"
    )

    # ------------------------------------------------------------------ #
    # 4. Fix Quality (last 7 days)                                        #
    # ------------------------------------------------------------------ #
    quality_status = "🟢"
    try:
        prs = _gh_json(
            ["pr", "list", "--repo", REPO, "--state", "all",
             "--json", "number,state,mergedAt,closedAt,createdAt",
             "--limit", "200"]
        ) or []
        # Filter to PRs created in the last 7 days
        recent = [
            pr for pr in prs
            if now_ts - _parse_gh_ts(pr.get("createdAt", "")) <= 7 * 86400
        ]
        merged_prs = [p for p in recent if p.get("mergedAt")]
        # gh CLI returns "CLOSED" or "OPEN" (uppercase) for state
        closed_no_merge = [
            p for p in recent
            if p.get("state", "").upper() == "CLOSED" and not p.get("mergedAt")
        ]
        open_prs = [
            p for p in recent
            if p.get("state", "").upper() == "OPEN"
        ]

        denom = len(merged_prs) + len(closed_no_merge)
        if denom > 0:
            rate = len(merged_prs) / denom
            rate_str = f"{rate:.0%}"
            if rate < 0.5 and denom >= 5:
                quality_status = "🟡"
                anomalies.append(
                    f"🟡 **Fix success rate drop**: {rate_str} merge rate "
                    f"({len(merged_prs)} merged / {denom} resolved)"
                )
        else:
            rate_str = "n/a (no resolved PRs)"

        quality_body = (
            f"| Merged | Closed w/o merge | Still open | Merge rate |\n"
            f"|--------|-----------------|------------|------------|\n"
            f"| {len(merged_prs)} | {len(closed_no_merge)} | {len(open_prs)} | {rate_str} |"
        )
    except Exception as exc:
        quality_body = f"⚠️ Data unavailable ({exc})"
        quality_status = "🟡"

    sections.append(
        f"## {quality_status} Fix Quality (last 7d)\n\n{quality_body}"
    )

    # ------------------------------------------------------------------ #
    # 5. Assemble the report                                               #
    # ------------------------------------------------------------------ #
    overall = "🟢 healthy"
    if any("🔴" in a for a in anomalies):
        overall = "🔴 critical"
    elif any("🟡" in a for a in anomalies):
        overall = "🟡 warning"

    run_date = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    if anomalies:
        anomaly_block = (
            "### Anomalies\n\n"
            + "\n".join(f"- {a}" for a in anomalies)
        )
    else:
        anomaly_block = "### Anomalies\n\n_None detected._"

    narrative = (
        f"Pipeline status as of **{run_date}**: **{overall}**. "
        f"{len(anomalies)} anomaly(ies) detected across cost, throughput, "
        f"stalls, and fix quality."
    )

    report = "\n\n".join(
        [
            f"# 🤖 Pipeline Health Report — {run_date}",
            narrative,
            anomaly_block,
        ]
        + sections
    )

    # ------------------------------------------------------------------ #
    # 6. Post or print                                                     #
    # ------------------------------------------------------------------ #
    if args.dry_run:
        print(report)
        dur = f"{int(time.monotonic() - t0)}s"
        log_run("health-report", repo=REPO, result="dry-run", duration=dur, exit=0)
        return 0

    result = _run(
        ["gh", "issue", "create",
         "--repo", REPO,
         "--title", f"Health Report — {datetime.now(timezone.utc).strftime('%Y-%m-%d')}",
         "--body", report,
         "--label", "health-report"],
        capture_output=True,
    )
    if result.returncode == 0:
        url = result.stdout.strip()
        print(f"[cai health-report] created report issue: {url}", flush=True)
    else:
        print(
            f"[cai health-report] failed to create issue: {result.stderr}",
            file=sys.stderr, flush=True,
        )

    dur = f"{int(time.monotonic() - t0)}s"
    log_run("health-report", repo=REPO, duration=dur, exit=result.returncode)
    return result.returncode


# ---------------------------------------------------------------------------
# check-workflows
# ---------------------------------------------------------------------------


def cmd_check_workflows(args) -> int:
    """Check GitHub Actions for recent workflow failures and raise findings."""
    print("[cai check-workflows] running workflow check", flush=True)
    t0 = time.monotonic()

    # 1. Fetch recent failed runs from GitHub Actions.
    try:
        runs = _gh_json([
            "run", "list",
            "--repo", REPO,
            "--status", "failure",
            "--json", "databaseId,name,headBranch,conclusion,createdAt,url,event,headSha",
            "--limit", "20",
        ]) or []
    except subprocess.CalledProcessError as exc:
        print(
            f"[cai check-workflows] gh run list failed: {exc}",
            file=sys.stderr, flush=True,
        )
        dur = f"{int(time.monotonic() - t0)}s"
        log_run("check-workflows", repo=REPO, result="gh_failed", duration=dur, exit=1)
        return 1

    # 2. Filter: last 24 hours only, skip bot branches.
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    recent_runs = []
    for r in runs:
        try:
            created = datetime.fromisoformat(r["createdAt"].replace("Z", "+00:00"))
        except (KeyError, ValueError):
            continue
        if created < cutoff:
            continue
        if r.get("headBranch", "").startswith("auto-improve/"):
            continue
        recent_runs.append(r)

    if not recent_runs:
        dur = f"{int(time.monotonic() - t0)}s"
        print("[cai check-workflows] no recent failures found", flush=True)
        log_run("check-workflows", repo=REPO, failures=0, duration=dur, exit=0)
        return 0

    # 3. Fetch existing open check-workflows issues for dedup context.
    try:
        existing = _gh_json([
            "issue", "list",
            "--repo", REPO,
            "--label", "check-workflows",
            "--state", "open",
            "--json", "number,title,body",
            "--limit", "50",
        ]) or []
    except subprocess.CalledProcessError:
        existing = []

    # 4. Build user message.
    _uid = uuid.uuid4().hex[:8]
    work_dir = Path(f"/tmp/cai-check-workflows-{_uid}")
    work_dir.mkdir(parents=True, exist_ok=True)
    findings_file = work_dir / "findings.json"

    runs_section = "## Recent failed workflow runs\n\n"
    runs_section += json.dumps(recent_runs, indent=2) + "\n\n"

    existing_section = "## Existing open check-workflows issues\n\n"
    if existing:
        for iss in existing:
            existing_section += f"- #{iss['number']}: {iss['title']}\n"
            body_snippet = (iss.get("body") or "")[:300]
            existing_section += f"  Body: {body_snippet}\n"
    else:
        existing_section += "(none)\n"

    user_message = (
        runs_section
        + existing_section
        + f"\n## Findings file\n\nWrite your findings to: `{findings_file}`\n"
    )

    # 5. Invoke the declared cai-check-workflows agent.
    print(
        f"[cai check-workflows] running agent on {len(recent_runs)} failure(s)",
        flush=True,
    )
    agent = _run_claude_p(
        ["claude", "-p", "--agent", "cai-check-workflows",
         "--max-turns", "3",
         "--permission-mode", "acceptEdits",
         "--allowedTools", "Read,Grep,Glob,Write",
         "--add-dir", str(work_dir)],
        category="check-workflows",
        agent="cai-check-workflows",
        input=user_message,
        cwd="/app",
    )
    if agent.stdout:
        print(agent.stdout, flush=True)
    if agent.returncode != 0:
        print(
            f"[cai check-workflows] agent failed (exit {agent.returncode}):\n"
            f"{agent.stderr}",
            file=sys.stderr, flush=True,
        )
        shutil.rmtree(work_dir, ignore_errors=True)
        dur = f"{int(time.monotonic() - t0)}s"
        log_run("check-workflows", repo=REPO, result="agent_failed",
                duration=dur, exit=agent.returncode)
        return agent.returncode

    if not findings_file.exists():
        print(
            f"[cai check-workflows] agent did not write {findings_file} — "
            f"expected findings.json output",
            file=sys.stderr, flush=True,
        )
        shutil.rmtree(work_dir, ignore_errors=True)
        dur = f"{int(time.monotonic() - t0)}s"
        log_run("check-workflows", repo=REPO, result="no_findings_file",
                duration=dur, exit=1)
        return 1

    # 6. Publish findings via publish.py with check-workflows namespace.
    print("[cai check-workflows] publishing findings", flush=True)
    published = _run(
        ["python", str(PUBLISH_SCRIPT), "--namespace", "check-workflows",
         "--findings-file", str(findings_file)],
    )
    shutil.rmtree(work_dir, ignore_errors=True)

    dur = f"{int(time.monotonic() - t0)}s"
    log_run("check-workflows", repo=REPO, failures=len(recent_runs),
            duration=dur, exit=published.returncode)
    return published.returncode


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
