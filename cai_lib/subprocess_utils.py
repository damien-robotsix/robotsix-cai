"""Subprocess helpers extracted from cai.py."""

import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from cai_lib.logging_utils import log_cost


def _run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    """Thin wrapper around subprocess.run with text mode; defaults check=False.

    ``check`` is overridable — callers that want the stdlib raise-on-nonzero
    semantics can pass ``check=True``. Previously we hard-coded ``check=False``
    and then also forwarded ``**kwargs`` into ``subprocess.run``, which raised
    ``TypeError: got multiple values for keyword argument 'check'`` whenever
    a caller tried to opt in (e.g. `actions/triage.py` on the body-edit path).
    """
    kwargs.setdefault("check", False)
    return subprocess.run(cmd, text=True, **kwargs)


def _run_claude_p(
    cmd: list[str],
    *,
    category: str,
    agent: str = "",
    **kwargs,
) -> subprocess.CompletedProcess:
    """Run a `claude -p` command and record its cost.

    `cmd` is the full argv. The wrapper injects `--output-format json
    --verbose` so claude-code returns the cost/usage bookkeeping for
    the run. With `--verbose`, claude-code emits a JSON **array** of
    stream events (`system` → `assistant` → `user` → `result`); the
    `result` element holds `total_cost_usd`, `usage`, `duration_ms`,
    `result` text, etc. We extract that element, log a cost row, and
    rewrite `CompletedProcess.stdout` to just the `result` text — so
    existing callers that pipe `proc.stdout` to `publish.py` or print
    it keep working unchanged.

    `category` labels the row by top-level cai command (e.g.
    "analyze", "implement", "audit"). `agent` records the subagent name
    (e.g. "cai-implement") if applicable.

    On JSON parse failure or a missing `result` event, no cost row is
    written, the original stdout is left in place, and a one-line
    warning is printed to stderr so this silent-drop failure mode is
    noisy. Never raises.
    """
    # Inject --output-format json --verbose right after `claude -p`
    # (positions 0 and 1). --verbose is required for claude-code to
    # populate the `usage` field; with it, the output becomes a JSON
    # array of stream events instead of a single envelope dict.
    if len(cmd) < 2 or cmd[0] != "claude" or cmd[1] != "-p":
        raise ValueError("_run_claude_p requires cmd[:2] == ['claude', '-p']")
    plugin_dir = Path(".claude/plugins/cai-skills")
    plugin_flags: list[str] = (
        ["--plugin-dir", str(plugin_dir)] if plugin_dir.is_dir() else []
    )
    full_cmd = (
        cmd[:2]
        + ["--output-format", "json", "--verbose"]
        + plugin_flags
        + cmd[2:]
    )

    # Force capture so we can parse the JSON envelope. Callers that
    # previously did not capture (only cmd_init) get back the result
    # text in `.stdout` — they can print it themselves if needed.
    kwargs.setdefault("capture_output", True)
    proc = _run(full_cmd, **kwargs)

    # Parse the JSON envelope and write the cost row. Belt and braces
    # — never let log writes break the actual command flow.
    try:
        parsed = json.loads(proc.stdout) if proc.stdout else None
    except (json.JSONDecodeError, ValueError):
        parsed = None

    # Two shapes are tolerated:
    #   1. dict   — legacy `--output-format json` (no --verbose) returns
    #      a single envelope object. Kept for forward/backward compat.
    #   2. list   — current `--output-format json --verbose` returns a
    #      JSON array of stream events; the cost data lives on the
    #      element with `"type": "result"`.
    envelope: dict | None = None
    subagent_results: list[dict] = []
    if isinstance(parsed, dict):
        envelope = parsed
    elif isinstance(parsed, list):
        result_events = [
            e for e in parsed
            if isinstance(e, dict) and e.get("type") == "result"
        ]
        # The last result event is the parent (top-level) result;
        # earlier ones are subagent results.
        envelope = result_events[-1] if result_events else None
        subagent_results = result_events[:-1] if len(result_events) > 1 else []

    if envelope is None:
        # Don't fail the caller, but make the silent-drop loud so a
        # future shape change in claude-code surfaces immediately
        # instead of leaving cai-cost.jsonl mysteriously empty.
        preview = (proc.stdout or "")[:120].replace("\n", " ")
        print(
            f"[cai cost] could not extract cost envelope from claude -p "
            f"({category}/{agent}); stdout starts with: {preview!r}",
            file=sys.stderr,
            flush=True,
        )

    if isinstance(envelope, dict):
        usage = envelope.get("usage") or {}
        # claude-code's `usage` may be either a flat dict (input_tokens,
        # output_tokens, cache_*_input_tokens) or a nested per-model
        # dict. Record both shapes when available.
        flat_keys = (
            "input_tokens",
            "output_tokens",
            "cache_creation_input_tokens",
            "cache_read_input_tokens",
        )
        flat = {k: usage[k] for k in flat_keys if isinstance(usage.get(k), (int, float))}
        models = {
            k: v for k, v in usage.items()
            if isinstance(v, dict) and any(fk in v for fk in flat_keys)
        }

        # -- Subagent token aggregation --
        subagent_rows: list[dict] = []
        combined = dict(flat)  # start with parent tokens
        for sr in subagent_results:
            sr_usage = sr.get("usage") or {}
            sr_flat = {k: sr_usage[k] for k in flat_keys if isinstance(sr_usage.get(k), (int, float))}
            if sr_flat:
                for k in flat_keys:
                    if k in sr_flat:
                        combined[k] = combined.get(k, 0) + sr_flat[k]
                sr_entry: dict = dict(sr_flat)
                sr_entry["cost_usd"] = sr.get("total_cost_usd")
                subagent_rows.append(sr_entry)

        row = {
            "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "category": category,
            "agent": agent,
            "cost_usd": envelope.get("total_cost_usd"),
            "duration_ms": envelope.get("duration_ms"),
            "duration_api_ms": envelope.get("duration_api_ms"),
            "num_turns": envelope.get("num_turns"),
            "session_id": envelope.get("session_id"),
            "exit": proc.returncode,
            "is_error": bool(envelope.get("is_error", proc.returncode != 0)),
        }
        row.update(combined)
        if models:
            row["models"] = models
        if subagent_rows:
            sub_cost_sum = sum(float(s.get("cost_usd") or 0.0) for s in subagent_rows)
            total = float(envelope.get("total_cost_usd") or 0.0)
            row["parent_cost_usd"] = round(total - sub_cost_sum, 6)
            row["subagents"] = subagent_rows
        log_cost(row)

        # Rewrite stdout to the result text so existing callers stay
        # backwards compatible. If `result` is missing (e.g. the run
        # ended with subtype=error_max_budget_usd, which omits the
        # result field), fall back to the text of the last assistant
        # stream event so callers still see the agent's final output
        # instead of the raw JSON envelope.
        if "result" in envelope and isinstance(envelope["result"], str):
            proc.stdout = envelope["result"]
        elif isinstance(parsed, list):
            salvaged = _last_assistant_text(parsed)
            if salvaged:
                proc.stdout = salvaged

    return proc


def _last_assistant_text(events: list) -> str:
    """Return the concatenated text of the final assistant event, or ''."""
    for event in reversed(events):
        if not isinstance(event, dict) or event.get("type") != "assistant":
            continue
        message = event.get("message") or {}
        content = message.get("content") or []
        parts = [
            item.get("text", "")
            for item in content
            if isinstance(item, dict) and item.get("type") == "text"
        ]
        text = "".join(parts).strip()
        if text:
            return text
    return ""
