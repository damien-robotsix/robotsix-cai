"""cost_audit.py — agent-accessible cost exploration tools.

Provides two functions:
  cost_query(...)   — filter / group cost-log rows
  cost_issue(n)     — join cost rows + outcome + PR-linked rows for issue N

Both functions return JSON-serialisable Python objects. When executed
as __main__ they parse sys.argv and print JSON to stdout so a skill
prompt can invoke them via Bash (if available) or Claude can read the
file and reproduce the logic inline using Read + Glob.

Usage (standalone):
  python cost_audit.py cost_query '{"agent":"cai-implement","last_n":10}'
  python cost_audit.py cost_issue '{"issue_number":1208}'
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ── path helpers ──────────────────────────────────────────────────────────────

_COST_LOG = Path("/var/log/cai/cai-cost.jsonl")
_OUTCOME_LOG = Path("/var/log/cai/cai-outcomes.jsonl")
_AGGREGATE_DIR = Path("/var/log/cai/cost-aggregate")


def _load_rows(days: int = 90) -> list[dict]:
    """Load all cost-log rows from the last `days` days.

    Prefers the aggregate dir (multi-host) over the local log.
    Malformed lines are silently skipped.
    """
    cutoff = datetime.now(timezone.utc).timestamp() - days * 86400

    # Discover source files.
    paths: list[Path] = []
    if _AGGREGATE_DIR.exists():
        paths = list(_AGGREGATE_DIR.rglob("cai-cost.jsonl"))
    if not paths and _COST_LOG.exists():
        paths = [_COST_LOG]
    if not paths:
        return []

    rows: list[dict] = []
    for path in paths:
        try:
            with path.open("r") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                    except (json.JSONDecodeError, ValueError):
                        continue
                    if _row_ts(row) >= cutoff:
                        rows.append(row)
        except OSError:
            continue
    return rows


def _row_ts(row: dict) -> float:
    ts = row.get("ts") or ""
    try:
        return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc,
        ).timestamp()
    except ValueError:
        return 0.0


# ── cost_query ────────────────────────────────────────────────────────────────


def cost_query(
    *,
    agent: str | None = None,
    target: int | None = None,
    phase: str | None = None,
    module: str | None = None,
    session: str | None = None,
    since: str | None = None,
    until: str | None = None,
    fingerprint: str | None = None,
    min_cost: float | None = None,
    group_by: str | None = None,
    last_n: int | None = None,
) -> Any:
    """Filter cost-log rows with optional predicates.

    Parameters
    ----------
    agent:        exact match on the ``agent`` field
    target:       exact match on ``target_number``
    phase:        exact match on ``fsm_state``
    module:       exact match on ``module``
    session:      exact match on ``session_id``
    since:        ISO timestamp lower bound (inclusive)
    until:        ISO timestamp upper bound (exclusive)
    fingerprint:  exact match on ``prompt_fingerprint``
    min_cost:     minimum ``cost_usd`` (inclusive)
    group_by:     group rows by this field; returns ``{value: [rows]}``
    last_n:       keep only the last N rows (takes precedence over since/until)

    Returns a JSON-serialisable list of dicts (or a dict when group_by is set).
    """
    rows = _load_rows()

    # Chronological sort (stable for subsequent last_n slice).
    rows.sort(key=_row_ts)

    # Apply filters.
    def _keep(r: dict) -> bool:
        if agent is not None and r.get("agent") != agent:
            return False
        if target is not None and r.get("target_number") != target:
            return False
        if phase is not None and r.get("fsm_state") != phase:
            return False
        if module is not None and r.get("module") != module:
            return False
        if session is not None and r.get("session_id") != session:
            return False
        if fingerprint is not None and r.get("prompt_fingerprint") != fingerprint:
            return False
        if min_cost is not None:
            try:
                if float(r.get("cost_usd") or 0) < min_cost:
                    return False
            except (TypeError, ValueError):
                return False
        if since is not None:
            try:
                since_ts = datetime.strptime(since, "%Y-%m-%dT%H:%M:%SZ").replace(
                    tzinfo=timezone.utc,
                ).timestamp()
                if _row_ts(r) < since_ts:
                    return False
            except ValueError:
                pass
        if until is not None:
            try:
                until_ts = datetime.strptime(until, "%Y-%m-%dT%H:%M:%SZ").replace(
                    tzinfo=timezone.utc,
                ).timestamp()
                if _row_ts(r) >= until_ts:
                    return False
            except ValueError:
                pass
        return True

    filtered = [r for r in rows if _keep(r)]

    # group_by takes priority over last_n.
    if group_by is not None:
        groups: dict[str, list[dict]] = {}
        for r in filtered:
            val = str(r.get(group_by) or "(none)")
            groups.setdefault(val, []).append(r)
        return groups

    if last_n is not None:
        filtered = filtered[-last_n:]

    return filtered


# ── cost_issue ────────────────────────────────────────────────────────────────


def cost_issue(issue_number: int) -> dict:
    """Return cost, outcome, and PR-linked cost data for an issue.

    Returns
    -------
    {
        "cost_rows":      [...],   # cost rows where target_number == issue_number
        "outcome":        {...}|null,  # outcome-log row for the issue
        "linked_pr_rows": [...],   # cost rows for PRs linked to the issue
    }
    """
    rows = _load_rows()

    # Direct cost rows for the issue.
    cost_rows = [r for r in rows if r.get("target_number") == issue_number]

    # Outcome log.
    outcome: dict | None = None
    if _OUTCOME_LOG.exists():
        try:
            with _OUTCOME_LOG.open("r") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                    except (json.JSONDecodeError, ValueError):
                        continue
                    if row.get("issue_number") == issue_number:
                        outcome = row  # last matching row wins
        except OSError:
            pass

    # PR-linked rows: cost rows where the PR's target_number points at
    # our issue (i.e. rows tagged with a PR number that was opened for
    # this issue).  We detect these by looking for rows whose
    # ``target_number`` is NOT the issue itself but whose ``pr_number``
    # field equals the issue number, or whose ``target_number`` appears
    # in the set of PR numbers mentioned alongside the issue.
    #
    # Simple heuristic: collect all PR numbers seen in cost_rows (rows
    # whose ``target_number`` != issue_number but that share a session
    # with issue cost rows, or have a ``pr_number == issue_number``).
    linked_pr_numbers: set[int] = set()
    issue_sessions = {r.get("session_id") for r in cost_rows if r.get("session_id")}
    for r in rows:
        tn = r.get("target_number")
        if isinstance(tn, int) and tn != issue_number:
            # Same session as an issue cost row → likely the PR created for it.
            if r.get("session_id") in issue_sessions:
                linked_pr_numbers.add(tn)
        # Explicit back-reference from the PR's row.
        if r.get("pr_number") == issue_number and isinstance(tn, int):
            linked_pr_numbers.add(tn)

    linked_pr_rows = [
        r for r in rows
        if isinstance(r.get("target_number"), int)
        and r["target_number"] in linked_pr_numbers
    ]

    return {
        "cost_rows": cost_rows,
        "outcome": outcome,
        "linked_pr_rows": linked_pr_rows,
    }


# ── CLI entry point ───────────────────────────────────────────────────────────


def _parse_args(argv: list[str]) -> tuple[str, dict]:
    """Parse ``mode args_json`` from argv[1:].

    Returns (mode, kwargs_dict).
    """
    if len(argv) < 2:
        raise SystemExit("Usage: cost_audit.py <cost_query|cost_issue> ['{...}']")
    mode = argv[1]
    raw = argv[2] if len(argv) > 2 else "{}"
    try:
        kwargs = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Invalid JSON arguments: {exc}") from exc
    if not isinstance(kwargs, dict):
        raise SystemExit("Arguments must be a JSON object ({...})")
    return mode, kwargs


def main(argv: list[str] | None = None) -> None:
    argv = argv or sys.argv
    mode, kwargs = _parse_args(argv)
    if mode == "cost_query":
        result = cost_query(**kwargs)
    elif mode == "cost_issue":
        n = kwargs.get("issue_number")
        if not isinstance(n, int):
            raise SystemExit("cost_issue requires {\"issue_number\": <int>}")
        result = cost_issue(n)
    else:
        raise SystemExit(f"Unknown mode: {mode!r}. Use cost_query or cost_issue.")
    print(json.dumps(result, default=str, indent=2))


if __name__ == "__main__":
    main()
