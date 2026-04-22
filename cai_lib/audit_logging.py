"""Per-workflow structured audit logging for cai audit runs.

Writes one JSONL file per ``(kind, module)`` pair under
``/var/log/cai/audit/<kind>/<module>.jsonl``.  Each line is a JSON
object conforming to the schema below:

  {
    "ts":             "ISO 8601 UTC timestamp",
    "level":          "INFO" | "WARN" | "ERROR",
    "kind":           "code-reduction" | ...,
    "module":         "actions" | ...,
    "agent":          "cai-audit-code-reduction",
    "session_id":     "<sdk session id>" | null,
    "event":          "start" | "finish" | "error",
    "message":        "<human-readable one-liner>",
    "cost_usd":       0.1234 | null,
    "duration_ms":    45123 | null,
    "num_turns":      7 | null,
    "tokens": {
      "input_tokens": N, "output_tokens": N,
      "cache_creation_input_tokens": N,
      "cache_read_input_tokens": N
    } | null,
    "findings_count": 3 | null,
    "exit_code":      0 | 1 | null,
    "error_class":    "agent_nonzero" | "findings_missing_list"
                      | "findings_parse_error" | "publish_failed"
                      | "unexpected_exception" | null
  }

The two public helpers, ``audit_log_start`` and ``audit_log_finish``,
are additive sinks alongside the existing ``log_run`` / ``log_cost``
contracts — they do not replace them.
"""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from typing import Optional

from cai_lib.config import audit_log_path


def _write_log_entry(kind: str, module: str, row: dict) -> None:
    """Atomically append one JSON line to the audit log file. Never raises."""
    try:
        path = audit_log_path(kind, module)
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(row, separators=(",", ":")) + "\n"
        with path.open("a") as fh:
            fh.write(line)
            fh.flush()
    except Exception:  # noqa: BLE001
        pass


def audit_log_start(kind: str, module: str, agent: str) -> None:
    """Write a ``start`` event to the audit log before ``_run_claude_p``.

    Never raises — a logging failure must never abort the audit run.
    """
    _write_log_entry(kind, module, {
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "level": "INFO",
        "kind": kind,
        "module": module,
        "agent": agent,
        "session_id": None,
        "event": "start",
        "message": f"Starting audit for module {module}",
        "cost_usd": None,
        "duration_ms": None,
        "num_turns": None,
        "tokens": None,
        "findings_count": None,
        "exit_code": None,
        "error_class": None,
    })


def audit_log_finish(
    kind: str,
    module: str,
    agent: str,
    *,
    proc: Optional[subprocess.CompletedProcess],
    findings_count: Optional[int],
    exit_code: int,
    error_class: Optional[str] = None,
    message: str = "",
) -> None:
    """Write a ``finish`` or ``error`` event to the audit log.

    Lifts ``cost_usd``, ``duration_ms``, ``num_turns``, ``session_id``,
    and ``tokens`` from *proc* attributes when present (populated by
    ``_run_claude_p``'s internal cost row).  Falls back to ``null`` for
    a standard ``subprocess.CompletedProcess`` that does not carry them.

    Never raises — a logging failure must never abort the audit run.
    """
    # Extract metrics injected by _run_claude_p when available.
    cost_usd = getattr(proc, "cost_usd", None) if proc is not None else None
    duration_ms = getattr(proc, "duration_ms", None) if proc is not None else None
    num_turns = getattr(proc, "num_turns", None) if proc is not None else None
    session_id = getattr(proc, "session_id", None) if proc is not None else None
    tokens = getattr(proc, "tokens", None) if proc is not None else None

    level = "INFO" if exit_code == 0 else "ERROR"
    event = "finish" if exit_code == 0 else "error"

    _write_log_entry(kind, module, {
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "level": level,
        "kind": kind,
        "module": module,
        "agent": agent,
        "session_id": session_id,
        "event": event,
        "message": message or f"Audit {'completed' if exit_code == 0 else 'failed'} for module {module}",
        "cost_usd": cost_usd,
        "duration_ms": duration_ms,
        "num_turns": num_turns,
        "tokens": tokens,
        "findings_count": findings_count,
        "exit_code": exit_code,
        "error_class": error_class,
    })
