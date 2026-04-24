"""Logging utilities extracted from cai.py."""

import json
import re
from datetime import datetime, timezone

from cai_lib.config import LOG_PATH, COST_LOG_PATH, OUTCOME_LOG_PATH
from cai_lib.audit.cost import _load_outcome_counts


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


_CATEGORY_BODY_RE = re.compile(r"\*\*Category:\*\*\s*`?([^`\n]+?)`?\s*$", re.MULTILINE)


def _get_issue_category(issue: dict) -> str:
    """Return the category value parsed from *issue*'s body, or ``'(unknown)'`` if absent."""
    m = _CATEGORY_BODY_RE.search(issue.get("body") or "")
    if m:
        return m.group(1).strip()
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
