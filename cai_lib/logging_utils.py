"""Logging utilities extracted from cai.py."""

import json
import os
from datetime import datetime, timezone

from cai_lib.config import ACTIVE_JOB_PATH, LOG_PATH, COST_LOG_PATH, OUTCOME_LOG_PATH


def _write_active_job(cmd: str, issue: int) -> None:
    """Write active-job state for observability. Never raises."""
    try:
        ACTIVE_JOB_PATH.parent.mkdir(parents=True, exist_ok=True)
        ACTIVE_JOB_PATH.write_text(json.dumps({
            "pid": os.getpid(),
            "cmd": cmd,
            "issue": issue,
            "start_ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }))
    except OSError:
        pass


def _clear_active_job() -> None:
    """Clear active-job state file. Never raises."""
    try:
        ACTIVE_JOB_PATH.write_text("{}")
    except OSError:
        pass


def log_run(category: str, **fields) -> None:
    """Append one key=value line to the persistent run log. Never raises."""
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        parts = [f"{ts} [{category}]"]
        for k, v in fields.items():
            parts.append(f"{k}={v}")
        line = " ".join(parts) + "\n"
        with LOG_PATH.open("a") as f:
            f.write(line)
            f.flush()
    except Exception:
        pass


def log_cost(row: dict) -> None:
    """Append one JSON object to the per-invocation cost log. Never raises.

    Each row records the cost and token usage of a single `claude -p`
    invocation, plus the cai-side context (category, agent) so the
    audit agent and the `cost-report` subcommand can attribute spend
    to specific cai commands and subagents.
    """
    try:
        COST_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with COST_LOG_PATH.open("a") as f:
            f.write(json.dumps(row, separators=(",", ":")) + "\n")
            f.flush()
    except Exception:
        pass


def _get_issue_category(issue: dict) -> str:
    """Return the category label value for *issue*, or ``'(unknown)'`` if absent."""
    for ln in (lbl["name"] for lbl in issue.get("labels", [])):
        if ln.startswith("category:"):
            return ln.split(":", 1)[1]
    return "(unknown)"


def _log_outcome(issue_number: int, category: str, outcome: str, fix_attempt_count: int) -> None:
    """Append one JSON record to the outcome log. Never raises."""
    try:
        OUTCOME_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        row = {
            "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "issue_number": issue_number,
            "category": category,
            "outcome": outcome,
            "fix_attempt_count": fix_attempt_count,
        }
        with OUTCOME_LOG_PATH.open("a") as f:
            f.write(json.dumps(row, separators=(",", ":")) + "\n")
            f.flush()
    except Exception:
        pass


def _load_outcome_counts(days: int = 90) -> dict:
    """Read OUTCOME_LOG_PATH and return per-category {total, solved} counts.

    Filters to trailing `days` days. Malformed lines are skipped silently.
    Returns an empty dict if the file is missing or unreadable.
    """
    if not OUTCOME_LOG_PATH.exists():
        return {}
    cutoff_ts = datetime.now(timezone.utc).timestamp() - days * 86400
    counts: dict = {}  # category -> {"total": N, "solved": N}
    try:
        with OUTCOME_LOG_PATH.open("r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                ts = row.get("ts", "")
                try:
                    row_ts = datetime.strptime(
                        ts, "%Y-%m-%dT%H:%M:%SZ"
                    ).replace(tzinfo=timezone.utc).timestamp()
                except ValueError:
                    continue
                if row_ts < cutoff_ts:
                    continue
                cat = row.get("category") or "(unknown)"
                outcome = row.get("outcome", "")
                bucket = counts.setdefault(cat, {"total": 0, "solved": 0})
                bucket["total"] += 1
                if outcome == "solved":
                    bucket["solved"] += 1
    except OSError:
        return {}
    return counts


def _load_outcome_stats(days: int = 90) -> dict:
    """Load per-category success rates from the trailing `days` days of outcome data.

    Returns a dict mapping category name to success rate (0.0–1.0).
    Categories with fewer than 3 observations get a neutral prior of 0.60.
    """
    counts = _load_outcome_counts(days)
    rates: dict = {}
    for cat, c in counts.items():
        if c["total"] < 3:
            rates[cat] = 0.60
        else:
            rates[cat] = c["solved"] / c["total"]
    return rates


def _load_cost_log(days: int = 7) -> list[dict]:
    """Read COST_LOG_PATH and return rows from the last `days` days.

    Each row is a dict as written by `log_cost`. Malformed lines are
    skipped silently. Returns an empty list if the file is missing or
    unreadable. Used by both `_build_cost_summary` (audit prompt) and
    `cmd_cost_report` (host-facing report).
    """
    if not COST_LOG_PATH.exists():
        return []
    cutoff_ts = datetime.now(timezone.utc).timestamp() - days * 86400
    rows: list[dict] = []
    try:
        with COST_LOG_PATH.open("r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                ts = row.get("ts") or ""
                try:
                    row_ts = datetime.strptime(
                        ts, "%Y-%m-%dT%H:%M:%SZ",
                    ).replace(tzinfo=timezone.utc).timestamp()
                except ValueError:
                    continue
                if row_ts >= cutoff_ts:
                    rows.append(row)
    except Exception:
        return []
    return rows


def _row_ts(row: dict) -> float:
    """Parse a cost-log row's 'ts' field to a Unix timestamp.

    Returns 0.0 on any parse failure so callers can safely compare
    against numeric boundaries without extra error handling.
    """
    ts = row.get("ts") or ""
    try:
        return datetime.strptime(
            ts, "%Y-%m-%dT%H:%M:%SZ",
        ).replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        return 0.0


def _build_cost_summary(days: int = 7, top_n: int = 10) -> str:
    """Build a markdown cost summary for the cai-audit user message.

    Returns an empty string if no cost rows exist for the window.
    Otherwise emits a section with per-category aggregates and the
    top-N most expensive individual invocations, so the audit agent
    can spot cost outliers (a single invocation that dwarfs the
    median, or a category that dominates total spend).
    """
    rows = _load_cost_log(days=days)
    if not rows:
        return ""

    # Per-category aggregates: total cost, call count, mean cost.
    cats: dict[str, dict] = {}
    grand_total = 0.0
    for r in rows:
        cat = r.get("category") or "(unknown)"
        cost = r.get("cost_usd") or 0.0
        try:
            cost = float(cost)
        except (TypeError, ValueError):
            cost = 0.0
        bucket = cats.setdefault(cat, {"calls": 0, "cost": 0.0})
        bucket["calls"] += 1
        bucket["cost"] += cost
        grand_total += cost

    cat_lines = []
    for cat, b in sorted(cats.items(), key=lambda kv: -kv[1]["cost"]):
        share = (b["cost"] / grand_total * 100.0) if grand_total else 0.0
        mean = b["cost"] / b["calls"] if b["calls"] else 0.0
        cat_lines.append(
            f"| {cat} | {b['calls']} | ${b['cost']:.4f} "
            f"({share:.1f}%) | ${mean:.4f} |"
        )

    # Top-N most expensive individual invocations.
    top = sorted(
        rows,
        key=lambda r: float(r.get("cost_usd") or 0.0),
        reverse=True,
    )[:top_n]
    top_lines = []
    for r in top:
        cost = float(r.get("cost_usd") or 0.0)
        top_lines.append(
            f"| {r.get('ts', '')} | {r.get('category', '')} | "
            f"{r.get('agent', '')} | ${cost:.4f} | "
            f"{r.get('num_turns', '')} | "
            f"{(r.get('input_tokens') or 0) + (r.get('output_tokens') or 0)} |"
        )

    return (
        f"## Cost summary (last {days}d, total ${grand_total:.4f} "
        f"across {len(rows)} invocations)\n\n"
        "### Per-category totals\n\n"
        "| category | calls | total cost (share) | mean cost |\n"
        "|---|---|---|---|\n"
        + "\n".join(cat_lines)
        + "\n\n"
        f"### Top {len(top_lines)} most expensive individual invocations\n\n"
        "| ts | category | agent | cost | turns | tokens |\n"
        "|---|---|---|---|---|---|\n"
        + "\n".join(top_lines)
        + "\n"
    )
