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

Built on ``structlog``: a minimal per-file ``_JSONLAppender`` logger at
the bottom of the stack with ``TimeStamper`` (injects ``ts``),
``EventRenamer`` (renames the built-in ``event`` slot to ``message`` so
our typed ``event`` field can live under its own name), and
``JSONRenderer`` in the processor chain.

The two public helpers, ``audit_log_start`` and ``audit_log_finish``,
are additive sinks alongside the existing ``log_run`` / ``log_cost``
contracts — they do not replace them.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Optional

import structlog

from cai_lib.config import audit_log_path


class _JSONLAppender:
    """Bottom-of-stack structlog logger that appends one rendered line to a file.

    structlog's processor chain renders an event dict into a JSONL
    string and hands it to the matching log-level method on this
    class.  All log-level names are aliased to the same ``msg``
    implementation — structlog only calls whichever method matches
    the level used at the call site.
    """

    __slots__ = ("_path",)

    def __init__(self, path: Path) -> None:
        self._path = path

    def msg(self, message: str) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a") as fh:
            fh.write(message + "\n")
            fh.flush()

    log = debug = info = warning = warn = error = critical = fatal = msg


_PROCESSORS = [
    structlog.processors.TimeStamper(fmt="%Y-%m-%dT%H:%M:%SZ", utc=True, key="ts"),
    structlog.processors.EventRenamer("message", replace_by="_event"),
    structlog.processors.JSONRenderer(separators=(",", ":")),
]


def _get_logger(kind: str, module: str):
    """Return a structlog bound logger writing to ``audit_log_path(kind, module)``."""
    return structlog.wrap_logger(
        _JSONLAppender(audit_log_path(kind, module)),
        processors=_PROCESSORS,
    )


def audit_log_start(kind: str, module: str, agent: str) -> None:
    """Write a ``start`` event to the audit log before ``_run_claude_p``.

    Never raises — a logging failure must never abort the audit run.
    """
    try:
        _get_logger(kind, module).info(
            f"Starting audit for module {module}",
            level="INFO",
            kind=kind,
            module=module,
            agent=agent,
            session_id=None,
            _event="start",
            cost_usd=None,
            duration_ms=None,
            num_turns=None,
            tokens=None,
            findings_count=None,
            exit_code=None,
            error_class=None,
        )
    except Exception:  # noqa: BLE001
        pass


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
    try:
        cost_usd = getattr(proc, "cost_usd", None) if proc is not None else None
        duration_ms = getattr(proc, "duration_ms", None) if proc is not None else None
        num_turns = getattr(proc, "num_turns", None) if proc is not None else None
        session_id = getattr(proc, "session_id", None) if proc is not None else None
        tokens = getattr(proc, "tokens", None) if proc is not None else None

        level = "INFO" if exit_code == 0 else "ERROR"
        event = "finish" if exit_code == 0 else "error"
        human_message = message or (
            f"Audit completed for module {module}"
            if exit_code == 0
            else f"Audit failed for module {module}"
        )

        _get_logger(kind, module).info(
            human_message,
            level=level,
            kind=kind,
            module=module,
            agent=agent,
            session_id=session_id,
            _event=event,
            cost_usd=cost_usd,
            duration_ms=duration_ms,
            num_turns=num_turns,
            tokens=tokens,
            findings_count=findings_count,
            exit_code=exit_code,
            error_class=error_class,
        )
    except Exception:  # noqa: BLE001
        pass
