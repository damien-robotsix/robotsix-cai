"""Audit-side cost/outcome helpers (moved from cai_lib/logging_utils.py)."""

import json
from datetime import datetime, timezone

from cai_lib.config import COST_LOG_AGGREGATE_DIR, COST_LOG_PATH, OUTCOME_LOG_PATH


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


def _load_cost_log(days: int = 7) -> list[dict]:
    """Read cost log rows from the last `days` days.

    When ``COST_LOG_AGGREGATE_DIR`` is populated (cross-host cost sync has
    run), reads the union of all machines' ``cai-cost.jsonl`` files from
    that directory. Falls back to the local-only ``COST_LOG_PATH`` when the
    aggregate dir is absent or empty — preserving single-host behaviour for
    deployments without sync configured.

    Each row is a dict as written by ``log_cost``. Malformed lines are
    skipped silently. Returns an empty list if no readable log exists.
    Used by both ``_build_cost_summary`` (audit prompt) and
    ``cmd_cost_report`` (host-facing report).
    """
    # Prefer aggregate (multi-host) over local-only when available.
    agg_files: list = []
    if COST_LOG_AGGREGATE_DIR.exists():
        agg_files = list(COST_LOG_AGGREGATE_DIR.rglob("cai-cost.jsonl"))

    if agg_files:
        paths_to_read = agg_files
    elif COST_LOG_PATH.exists():
        paths_to_read = [COST_LOG_PATH]
    else:
        return []

    cutoff_ts = datetime.now(timezone.utc).timestamp() - days * 86400
    rows: list[dict] = []
    for path in paths_to_read:
        if not path.exists():
            continue
        try:
            with path.open("r") as f:
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
            continue
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


def _primary_model(row: dict) -> str:
    """Return the model name with the most output tokens, or ''."""
    models = row.get("models")
    if not models or not isinstance(models, dict):
        return ""
    best = max(models.items(), key=lambda kv: kv[1].get("outputTokens", 0))
    return best[0] if best else ""


def _build_cost_summary(days: int = 7, top_n: int = 10) -> str:
    """Build a markdown cost summary for the on-demand
    cost-reduction audit user message.

    Returns an empty string if no cost rows exist for the window.
    Otherwise emits a section with per-category aggregates, per-FSM-state
    aggregates (when rows carry the optional #1203 ``fsm_state`` field),
    and the top-N most expensive individual invocations, so the audit
    agent can spot cost outliers (a single invocation that dwarfs the
    median, or a funnel stage that dominates total spend).
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

    # Per-FSM-state aggregates (issue #1203). Rows written by non-FSM
    # call sites omit ``fsm_state``; those land in the ``(none)`` bucket
    # so the section stays faithful to the data.
    fsm_states: dict[str, dict] = {}
    for r in rows:
        fs = r.get("fsm_state") or "(none)"
        cost = r.get("cost_usd") or 0.0
        try:
            cost = float(cost)
        except (TypeError, ValueError):
            cost = 0.0
        bucket = fsm_states.setdefault(fs, {"calls": 0, "cost": 0.0})
        bucket["calls"] += 1
        bucket["cost"] += cost

    fsm_lines = []
    for fs, b in sorted(fsm_states.items(), key=lambda kv: -kv[1]["cost"]):
        share = (b["cost"] / grand_total * 100.0) if grand_total else 0.0
        mean = b["cost"] / b["calls"] if b["calls"] else 0.0
        fsm_lines.append(
            f"| {fs} | {b['calls']} | ${b['cost']:.4f} "
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
        # Issue #1205: cite the pre-computed ``cache_hit_rate`` field
        # written by ``_run_claude_p`` (aggregate rate over
        # cache_read + cache_creation + input tokens). Rows predating
        # the change legitimately omit the field and render as ``-``.
        hit = r.get("cache_hit_rate")
        hit_str = f"{hit * 100:.1f}%" if isinstance(hit, (int, float)) else "-"
        top_lines.append(
            f"| {r.get('ts', '')} | {r.get('category', '')} | "
            f"{r.get('agent', '')} | {_primary_model(r)} | ${cost:.4f} | "
            f"{r.get('num_turns', '')} | "
            f"{(r.get('input_tokens') or 0) + (r.get('output_tokens') or 0)} | "
            f"{hit_str} |"
        )

    return (
        f"## Cost summary (last {days}d, total ${grand_total:.4f} "
        f"across {len(rows)} invocations)\n\n"
        "### Per-category totals\n\n"
        "| category | calls | total cost (share) | mean cost |\n"
        "|---|---|---|---|\n"
        + "\n".join(cat_lines)
        + "\n\n"
        "### By FSM state\n\n"
        "| fsm_state | calls | total cost (share) | mean cost |\n"
        "|---|---|---|---|\n"
        + "\n".join(fsm_lines)
        + "\n\n"
        f"### Top {len(top_lines)} most expensive individual invocations\n\n"
        "| ts | category | agent | model | cost | turns | tokens | hit% |\n"
        "|---|---|---|---|---|---|---|---|\n"
        + "\n".join(top_lines)
        + "\n"
    )
