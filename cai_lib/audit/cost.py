"""Audit-side cost/outcome helpers (moved from cai_lib/utils/log.py)."""

import fnmatch
import json
from datetime import datetime, timezone
from pathlib import Path

from cai_lib.config import COST_LOG_AGGREGATE_DIR, COST_LOG_PATH, OUTCOME_LOG_PATH


def _iter_outcome_rows(days: int):
    """Yield parsed outcome-log rows within the trailing ``days`` day window.

    Handles file-existence guard, cutoff calculation, OS errors, and
    per-line JSON/timestamp parsing. Rows with unparsable timestamps are
    silently skipped (``_row_ts`` returns 0.0, which is always < cutoff).
    """
    if not OUTCOME_LOG_PATH.exists():
        return
    cutoff_ts = datetime.now(timezone.utc).timestamp() - days * 86400
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
                if _row_ts(row) < cutoff_ts:
                    continue
                yield row
    except OSError:
        return


def _load_outcome_counts(days: int = 90) -> dict:
    """Read OUTCOME_LOG_PATH and return per-category {total, solved} counts.

    Filters to trailing `days` days. Malformed lines are skipped silently.
    Returns an empty dict if the file is missing or unreadable.
    """
    counts: dict = {}  # category -> {"total": N, "solved": N}
    for row in _iter_outcome_rows(days):
        cat = row.get("category") or "(unknown)"
        outcome = row.get("outcome", "")
        bucket = counts.setdefault(cat, {"total": 0, "solved": 0})
        bucket["total"] += 1
        if outcome == "solved":
            bucket["solved"] += 1
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
                    if _row_ts(row) >= cutoff_ts:
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


def _load_outcome_index(days: int = 90) -> dict[int, dict]:
    """Return a mapping of issue_number -> {outcome, fix_attempt_count}
    from the outcome log. Used by _build_cost_summary for §3 and §4 joins.

    Returns {} when the file is absent, the required fields are missing,
    or any read failure occurs — all section joins degrade gracefully.
    """
    result: dict[int, dict] = {}
    for row in _iter_outcome_rows(days):
        issue_num = row.get("issue_number")
        if not isinstance(issue_num, int):
            continue
        result[issue_num] = {
            "outcome": row.get("outcome"),
            "fix_attempt_count": row.get("fix_attempt_count"),
        }
    return result


def _build_module_index() -> list[tuple[str, str]]:
    """Return a list of (glob_pattern, module_name) from docs/modules.yaml.

    Used by _build_cost_summary §5 to infer module name from scope_files.
    Returns [] when the manifest is absent, unreadable, or raises.
    """
    try:
        from cai_lib.audit.modules import load_modules  # noqa: PLC0415
        manifest = Path(__file__).resolve().parents[2] / "docs" / "modules.yaml"
        modules = load_modules(manifest)
        return [(g, m.name) for m in modules for g in m.globs]
    except Exception:  # noqa: BLE001
        return []


def _infer_module_from_files(
    scope_files: list[str],
    module_index: list[tuple[str, str]],
) -> str | None:
    """Return the most-matched module name for a list of scope_files.

    Counts how many files in scope_files match each module's globs; returns
    the module with the most matches. Returns None when no match is found.
    """
    module_counts: dict[str, int] = {}
    for f in scope_files:
        for g, name in module_index:
            if fnmatch.fnmatch(f, g):
                module_counts[name] = module_counts.get(name, 0) + 1
    if not module_counts:
        return None
    return max(module_counts.items(), key=lambda kv: kv[1])[0]


def _build_cost_summary(days: int = 7, top_n: int = 10, cluster_n: int = 10) -> str:
    """Build a rich 7-section cost-analysis markdown summary for the
    on-demand cost-reduction audit user message and operator banner.

    Returns an empty string when no cost rows exist for the window.
    Each section degrades gracefully when its required fields are absent
    from the data — sections with no content are omitted.

    Sections:
      §1 Window headline
      §2 Recent vs prior Δ by agent (skip agents < 2*cluster_n invocations)
      §3 Top-N expensive targets (skip when no rows have target_number)
      §4 Phase breakdown by fsm_state (always shown)
      §5 Per-module cost (skip when no module/scope_files data available)
      §6 Cache-health regressions (skip when no regressions detected)
      §7 Host anomalies (always shown when rows have host field)
    """
    rows = _load_cost_log(days=days)
    if not rows:
        return ""

    def _cost(r: dict) -> float:
        try:
            return float(r.get("cost_usd") or 0.0)
        except (TypeError, ValueError):
            return 0.0

    rows_sorted = sorted(rows, key=_row_ts)
    grand_total = sum(_cost(r) for r in rows)
    target_numbers = {r["target_number"] for r in rows if isinstance(r.get("target_number"), int)}
    hosts = {r["host"] for r in rows if r.get("host")}

    # Precompute shared indices (outcome join for §3+§4, module globs for §5).
    outcome_index = _load_outcome_index()
    module_index = _build_module_index()

    sections: list[str] = []

    # ── §1 Window headline ─────────────────────────────────────────────
    sections.append(
        f"## Cost summary (last {days}d, ${grand_total:.4f} across "
        f"{len(rows)} invocations, {len(target_numbers)} unique target(s), "
        f"{len(hosts)} host(s), cluster_n={cluster_n})\n"
    )

    # ── §2 Recent vs prior Δ by agent ──────────────────────────────────
    # Group rows by agent in chronological order; compare most-recent
    # cluster_n vs the prior cluster_n; skip agents with < 2*cluster_n calls.
    by_agent: dict[str, list[dict]] = {}
    for r in rows_sorted:
        a = r.get("agent") or "(no-agent)"
        by_agent.setdefault(a, []).append(r)

    delta_lines: list[str] = []
    for agent_name in sorted(by_agent.keys()):
        agent_rows = by_agent[agent_name]
        if len(agent_rows) < cluster_n * 2:
            continue
        recent = agent_rows[-cluster_n:]
        prior = agent_rows[-(cluster_n * 2):-cluster_n]
        recent_mean = sum(_cost(r) for r in recent) / cluster_n
        prior_mean = sum(_cost(r) for r in prior) / cluster_n
        delta_pct = ((recent_mean - prior_mean) / prior_mean * 100.0) if prior_mean else 0.0
        flag = " ⚠️" if delta_pct > 10.0 else ""
        delta_lines.append(
            f"| {agent_name} | {len(agent_rows)} | ${prior_mean:.4f} | "
            f"${recent_mean:.4f} | {delta_pct:+.1f}%{flag} |"
        )

    if delta_lines:
        sections.append(
            f"### §2 Recent vs prior Δ by agent "
            f"(recent/prior = last {cluster_n}/{cluster_n} calls, "
            f"skip < {cluster_n * 2} invocations)\n\n"
            "| agent | total calls | prior mean | recent mean | Δ% |\n"
            "|---|---|---|---|---|\n"
            + "\n".join(delta_lines) + "\n"
        )

    # ── §3 Top-N expensive targets ─────────────────────────────────────
    # Group by target_number, join with outcome log for outcome +
    # fix_attempt_count. Rows without target_number are excluded here.
    by_target: dict[int, list[dict]] = {}
    for r in rows:
        tn = r.get("target_number")
        if isinstance(tn, int):
            by_target.setdefault(tn, []).append(r)

    if by_target:
        top_targets = sorted(
            by_target.items(),
            key=lambda kv: sum(_cost(r) for r in kv[1]),
            reverse=True,
        )[:top_n]
        target_lines = []
        for tn, tn_rows in top_targets:
            total = sum(_cost(r) for r in tn_rows)
            oi = outcome_index.get(tn, {})
            outcome = oi.get("outcome") or "-"
            fa = oi.get("fix_attempt_count")
            attempts_str = str(int(fa)) if isinstance(fa, (int, float)) else "-"
            target_lines.append(
                f"| #{tn} | {len(tn_rows)} | ${total:.4f} | {outcome} | {attempts_str} |"
            )
        sections.append(
            f"### §3 Top-{len(target_lines)} expensive target(s)\n\n"
            "| target | calls | total cost | outcome | fix_attempts |\n"
            "|---|---|---|---|---|\n"
            + "\n".join(target_lines) + "\n"
        )

    # ── §4 Phase breakdown ─────────────────────────────────────────────
    # Group all rows by fsm_state. For rows with target_number, join the
    # outcome log to split first-attempt (fix_attempt_count ≤ 1) vs retry.
    fsm_buckets: dict[str, dict] = {}
    for r in rows:
        fs = r.get("fsm_state") or "(none)"
        tn = r.get("target_number")
        oi = outcome_index.get(tn, {}) if isinstance(tn, int) else {}
        fa = oi.get("fix_attempt_count")
        is_first = not isinstance(fa, (int, float)) or fa <= 1
        bucket = fsm_buckets.setdefault(fs, {"first": [], "retry": []})
        if is_first:
            bucket["first"].append(r)
        else:
            bucket["retry"].append(r)

    phase_lines = []
    for fs in sorted(
        fsm_buckets.keys(),
        key=lambda k: -(
            sum(_cost(r) for r in fsm_buckets[k]["first"])
            + sum(_cost(r) for r in fsm_buckets[k]["retry"])
        ),
    ):
        b = fsm_buckets[fs]
        first_cost = sum(_cost(r) for r in b["first"])
        retry_cost = sum(_cost(r) for r in b["retry"])
        phase_lines.append(
            f"| {fs} | {len(b['first'])} | ${first_cost:.4f} | "
            f"{len(b['retry'])} | ${retry_cost:.4f} | "
            f"${first_cost + retry_cost:.4f} |"
        )

    sections.append(
        "### §4 Phase breakdown (first-attempt vs retry, by fsm_state)\n\n"
        "| fsm_state | first calls | first cost | retry calls | "
        "retry cost | total cost |\n"
        "|---|---|---|---|---|---|\n"
        + "\n".join(phase_lines) + "\n"
    )

    # ── §5 Per-module cost ─────────────────────────────────────────────
    # Group by the ``module`` field. For rows missing ``module`` but having
    # ``scope_files``, infer module via fnmatch against docs/modules.yaml.
    module_buckets: dict[str, list[dict]] = {}
    for r in rows:
        mod = r.get("module") or ""
        if not mod:
            sf = r.get("scope_files")
            if isinstance(sf, list) and sf and module_index:
                mod = _infer_module_from_files(sf, module_index) or ""
        if mod:
            module_buckets.setdefault(mod, []).append(r)

    if module_buckets:
        module_lines = []
        for mod in sorted(
            module_buckets.keys(),
            key=lambda k: -sum(_cost(r) for r in module_buckets[k]),
        ):
            mod_rows = module_buckets[mod]
            total = sum(_cost(r) for r in mod_rows)
            module_lines.append(f"| {mod} | {len(mod_rows)} | ${total:.4f} |")
        sections.append(
            "### §5 Per-module cost\n\n"
            "| module | calls | total cost |\n"
            "|---|---|---|\n"
            + "\n".join(module_lines) + "\n"
        )

    # ── §6 Cache-health regressions ────────────────────────────────────
    # Group by (agent, prompt_fingerprint). Compare most-recent cluster_n
    # vs prior cluster_n cache_hit_rate. Flag ≥10pp drops.
    # Skip pairs with < 2*cluster_n invocations.
    fp_buckets: dict[tuple, list[dict]] = {}
    for r in rows_sorted:
        a = r.get("agent") or "(no-agent)"
        fp = r.get("prompt_fingerprint")
        if fp is not None:
            fp_buckets.setdefault((a, fp), []).append(r)

    regression_lines = []
    for (a, fp), fp_rows in sorted(fp_buckets.items()):
        if len(fp_rows) < cluster_n * 2:
            continue
        recent = fp_rows[-cluster_n:]
        prior = fp_rows[-(cluster_n * 2):-cluster_n]
        recent_vals = [
            r["cache_hit_rate"] for r in recent
            if isinstance(r.get("cache_hit_rate"), (int, float))
        ]
        prior_vals = [
            r["cache_hit_rate"] for r in prior
            if isinstance(r.get("cache_hit_rate"), (int, float))
        ]
        if not recent_vals or not prior_vals:
            continue
        recent_mean = sum(recent_vals) / len(recent_vals)
        prior_mean = sum(prior_vals) / len(prior_vals)
        drop_pp = (prior_mean - recent_mean) * 100.0
        if drop_pp >= 10.0:
            regression_lines.append(
                f"| {a} | `{fp}` | {prior_mean * 100:.1f}% | "
                f"{recent_mean * 100:.1f}% | -{drop_pp:.1f}pp ⚠️ |"
            )

    if regression_lines:
        sections.append(
            f"### §6 Cache-health regressions "
            f"(≥10pp drop, ≥{cluster_n * 2} invocations)\n\n"
            "| agent | fingerprint | prior hit% | recent hit% | drop |\n"
            "|---|---|---|---|---|\n"
            + "\n".join(regression_lines) + "\n"
        )

    # ── §7 Host anomalies ──────────────────────────────────────────────
    # Per-host totals; flag hosts whose mean $/call is ≥ 2× the median.
    host_buckets: dict[str, list[dict]] = {}
    for r in rows:
        h = r.get("host")
        if h:
            host_buckets.setdefault(h, []).append(r)

    if host_buckets:
        host_means = {
            h: sum(_cost(r) for r in hrs) / len(hrs)
            for h, hrs in host_buckets.items()
        }
        sorted_means = sorted(host_means.values())
        n = len(sorted_means)
        if n % 2 == 1:
            median_mean = sorted_means[n // 2]
        else:
            median_mean = (sorted_means[n // 2 - 1] + sorted_means[n // 2]) / 2.0

        host_lines = []
        for h in sorted(host_buckets.keys(), key=lambda k: -host_means[k]):
            total = sum(_cost(r) for r in host_buckets[h])
            mean = host_means[h]
            flag = " ⚠️" if median_mean > 0 and mean >= 2.0 * median_mean else ""
            host_lines.append(
                f"| {h} | {len(host_buckets[h])} | ${total:.4f} | ${mean:.4f}{flag} |"
            )
        sections.append(
            "### §7 Host anomalies (flag mean $/call ≥ 2× median)\n\n"
            "| host | calls | total cost | mean cost |\n"
            "|---|---|---|---|\n"
            + "\n".join(host_lines) + "\n"
        )

    return "\n".join(sections)
