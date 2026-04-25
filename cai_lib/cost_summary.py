"""Close-time per-issue cost-summary comment.

Sits on top of the per-invocation ``<!-- cai-cost ... -->`` markers
posted by :func:`cai_lib.cost_comment._post_cost_comment`. When an
auto-improve issue reaches the merged path,
:func:`post_final_cost_summary` aggregates every cost row tagged
against that issue or its linked PR and posts a single
``<!-- cai-cost-final ... -->`` roll-up comment on the issue.

Best-effort by contract: any exception is caught at the top level and
logged to stderr. A failed post must never change the merge handler's
return value.

Depends on #1199 having landed so cost rows carry ``target_number`` /
``target_kind``. If those keys are absent on every row at close time,
the aggregator degrades to an empty summary and skips the post.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone

from cai_lib.config import OUTCOME_LOG_PATH


def _load_issue_cost_rows(
    issue_number: int, pr_number: int | None,
) -> list[dict]:
    """Return cost rows attributed to *issue_number* or *pr_number*.

    Calls ``cai_lib.transcript_sync.pull_cost`` so the aggregate
    mirror is fresh on multi-host deployments, then reads rows via
    :func:`cai_lib.audit.cost._load_cost_log` with a 90-day window.
    Filters to rows whose ``target_kind`` is ``"issue"``/``"pr"`` AND
    whose ``target_number`` matches one of the two numbers.
    """
    from cai_lib import transcript_sync
    from cai_lib.audit.cost import _load_cost_log
    try:
        transcript_sync.pull_cost()
    except Exception as exc:  # noqa: BLE001 — best-effort sync
        print(
            f"[cai cost-final] pull_cost failed: {exc}",
            file=sys.stderr, flush=True,
        )
    rows = _load_cost_log(days=90)
    targets: set[tuple[str, int]] = {("issue", issue_number)}
    if pr_number is not None:
        targets.add(("pr", pr_number))
    out: list[dict] = []
    for row in rows:
        kind = row.get("target_kind")
        num = row.get("target_number")
        if not isinstance(kind, str) or not isinstance(num, int):
            continue
        if (kind, num) in targets:
            out.append(row)
    return out


def _load_fix_attempt_count(issue_number: int) -> int:
    """Return the max ``fix_attempt_count`` recorded for *issue_number*.

    Reads ``OUTCOME_LOG_PATH`` (``/var/log/cai/cai-outcomes.jsonl``).
    Returns 0 when the file is missing, unreadable, or contains no
    matching row.
    """
    if not OUTCOME_LOG_PATH.exists():
        return 0
    best = 0
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
                if row.get("issue_number") != issue_number:
                    continue
                try:
                    count = int(row.get("fix_attempt_count") or 0)
                except (TypeError, ValueError):
                    continue
                if count > best:
                    best = count
    except OSError:
        return 0
    return best


def _stage_key(row: dict) -> str:
    """Pick the per-stage grouping key for a cost row.

    Prefers ``phase`` (issue #1202 log-layer enrichment) when present,
    falls back to ``fsm_state``, then to ``category`` (always present
    on well-formed rows).
    """
    for key in ("phase", "fsm_state", "category"):
        val = row.get(key)
        if isinstance(val, str) and val:
            return val
    return "(unknown)"


def _sum_field(rows: list[dict], key: str, cast=int):
    total = cast(0)
    for r in rows:
        try:
            total += cast(r.get(key) or 0)
        except (TypeError, ValueError):
            continue
    return total


def _build_final_cost_summary(
    issue_number: int,
    pr_number: int,
    rows: list[dict],
    fix_attempt_count: int,
) -> tuple[str, str]:
    """Render the ``<!-- cai-cost-final ... -->`` marker and body.

    Returns ``("", "")`` when *rows* is empty so the caller skips the
    post entirely.
    """
    if not rows:
        return ("", "")

    total_usd = _sum_field(rows, "cost_usd", float)
    total_turns = _sum_field(rows, "num_turns")
    total_duration_ms = _sum_field(rows, "duration_ms")
    total_input = _sum_field(rows, "input_tokens")
    total_output = _sum_field(rows, "output_tokens")
    total_cache_read = _sum_field(rows, "cache_read_input_tokens")
    total_cache_create = _sum_field(rows, "cache_creation_input_tokens")
    n_rows = len(rows)
    seconds = total_duration_ms / 1000.0

    marker = (
        f"<!-- cai-cost-final issue={issue_number} pr={pr_number} "
        f"total_usd={total_usd:.4f} total_turns={total_turns} "
        f"total_duration_ms={total_duration_ms} rows={n_rows} "
        f"fix_attempt_count={fix_attempt_count} -->"
    )

    # Per-agent breakdown.
    per_agent: dict[str, dict] = {}
    for r in rows:
        agent = r.get("agent") or "(unknown)"
        bucket = per_agent.setdefault(
            agent,
            {"calls": 0, "cost": 0.0, "input": 0, "output": 0,
             "cache_read": 0, "cache_create": 0},
        )
        bucket["calls"] += 1
        try:
            bucket["cost"] += float(r.get("cost_usd") or 0.0)
        except (TypeError, ValueError):
            pass
        bucket["input"] += int(r.get("input_tokens") or 0)
        bucket["output"] += int(r.get("output_tokens") or 0)
        bucket["cache_read"] += int(r.get("cache_read_input_tokens") or 0)
        bucket["cache_create"] += int(
            r.get("cache_creation_input_tokens") or 0,
        )

    agent_rows = sorted(
        per_agent.items(), key=lambda kv: -kv[1]["cost"],
    )
    agent_lines = [
        f"| `{name}` | {b['calls']} | ${b['cost']:.4f} | "
        f"{b['input']} | {b['output']} | "
        f"{b['cache_read']} | {b['cache_create']} |"
        for name, b in agent_rows
    ]

    # Per-stage breakdown (phase → fsm_state → category fallback).
    per_stage: dict[str, dict] = {}
    for r in rows:
        key = _stage_key(r)
        bucket = per_stage.setdefault(key, {"calls": 0, "cost": 0.0})
        bucket["calls"] += 1
        try:
            bucket["cost"] += float(r.get("cost_usd") or 0.0)
        except (TypeError, ValueError):
            pass
    stage_rows = sorted(
        per_stage.items(), key=lambda kv: -kv[1]["cost"],
    )
    stage_lines = [
        f"| `{name}` | {b['calls']} | ${b['cost']:.4f} |"
        for name, b in stage_rows
    ]

    # Cache health. Prefer per-row ``cache_hit_rate`` (#1205) when
    # present on every row; otherwise derive from flat token totals.
    explicit_rates: list[float] = []
    for r in rows:
        val = r.get("cache_hit_rate")
        if isinstance(val, (int, float)):
            explicit_rates.append(float(val))
    if explicit_rates and len(explicit_rates) == n_rows:
        cache_hit_rate = sum(explicit_rates) / len(explicit_rates)
    else:
        denom = total_cache_read + total_input
        cache_hit_rate = (
            total_cache_read / denom if denom > 0 else 0.0
        )

    # Parent model mix (share by $).
    parent_mix: dict[str, float] = {}
    for r in rows:
        model = r.get("parent_model") or "(unknown)"
        try:
            cost = float(r.get("cost_usd") or 0.0)
        except (TypeError, ValueError):
            cost = 0.0
        parent_mix[model] = parent_mix.get(model, 0.0) + cost
    parent_lines = []
    for model, cost in sorted(parent_mix.items(), key=lambda kv: -kv[1]):
        share = (cost / total_usd * 100.0) if total_usd else 0.0
        parent_lines.append(
            f"- `{model}`: ${cost:.4f} ({share:.1f}%)"
        )

    body = (
        f"## cai final cost summary\n\n"
        f"**Issue:** #{issue_number}  |  **PR:** #{pr_number}  |  "
        f"**Invocations:** {n_rows}  |  "
        f"**fix_attempt_count:** {fix_attempt_count}\n\n"
        f"**Total:** ${total_usd:.4f}  |  **Turns:** {total_turns}  |  "
        f"**Wall time:** {seconds:.1f}s  |  "
        f"**Cache hit rate:** {cache_hit_rate*100.0:.1f}%\n\n"
        f"### Per-agent breakdown\n\n"
        f"| agent | calls | cost | input tokens | output tokens | "
        f"cache_read | cache_create |\n"
        f"|---|---|---|---|---|---|---|\n"
        + "\n".join(agent_lines)
        + "\n\n### Per-stage breakdown\n\n"
        f"| stage | calls | cost |\n"
        f"|---|---|---|\n"
        + "\n".join(stage_lines)
        + "\n\n### Parent model mix\n\n"
        + ("\n".join(parent_lines) if parent_lines else "- (none)")
        + "\n"
    )
    return (marker, body)


def post_final_cost_summary(
    issue_number: int, pr_number: int,
) -> None:
    """Best-effort: aggregate + post the close-time cost summary.

    Contract mirrors :func:`cai_lib.cost_comment._post_cost_comment`:
    every exception is caught and logged to stderr; the caller (merge
    handler) must remain unaffected by failures here.
    """
    try:
        rows = _load_issue_cost_rows(issue_number, pr_number)
        if not rows:
            print(
                f"[cai cost-final] no attributed rows for "
                f"#{issue_number} / PR #{pr_number}; skipping",
                file=sys.stderr, flush=True,
            )
            return
        fix_attempt_count = _load_fix_attempt_count(issue_number)
        marker, body = _build_final_cost_summary(
            issue_number, pr_number, rows, fix_attempt_count,
        )
        if not marker or not body:
            return
        from cai_lib.github import _post_issue_comment
        _post_issue_comment(
            issue_number,
            marker + "\n" + body,
            log_prefix="cai cost final",
        )
    except Exception as exc:  # noqa: BLE001 — best-effort
        print(
            f"[cai cost-final] post_final_cost_summary failed for "
            f"#{issue_number}: {exc}",
            file=sys.stderr, flush=True,
        )
