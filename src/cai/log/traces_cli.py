"""Langfuse traces client and agent tools for querying cai workflow traces."""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any

from pydantic_ai import Tool


class LangfuseTraces:
    """Lazy-initialised Langfuse client for querying cai workflow traces."""

    def __init__(self) -> None:
        self._client: Any = None

    @property
    def client(self):
        if self._client is None:
            if not (os.environ.get("LANGFUSE_PUBLIC_KEY") and os.environ.get("LANGFUSE_SECRET_KEY")):
                raise EnvironmentError("LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY must be set")
            from langfuse import Langfuse
            kwargs: dict = {
                "public_key": os.environ["LANGFUSE_PUBLIC_KEY"],
                "secret_key": os.environ["LANGFUSE_SECRET_KEY"],
            }
            if base_url := os.environ.get("LANGFUSE_BASE_URL"):
                kwargs["host"] = base_url
            self._client = Langfuse(**kwargs)
        return self._client

    def list_traces(
        self,
        limit: int = 20,
        workflow: str | None = None,
        since: str | None = None,
    ) -> list[dict]:
        """Return recent traces as dicts with id, name, timestamp, cost, latency."""
        kwargs: dict = {"limit": limit, "page": 1}
        if workflow:
            kwargs["name"] = workflow
        if since:
            kwargs["from_timestamp"] = datetime.fromisoformat(since).replace(tzinfo=timezone.utc)
        result = self.client.api.trace.list(**kwargs)
        traces = result.data if hasattr(result, "data") else list(result)
        return [
            {
                "id": t.id,
                "name": t.name or "",
                "timestamp": t.timestamp.isoformat() if getattr(t, "timestamp", None) else None,
                "cost": getattr(t, "total_cost", None),
                "latency": getattr(t, "latency", None),
            }
            for t in traces
        ]

    def show_trace(self, trace_id: str, full: bool = False, analyze: bool = False) -> dict:
        """Return details of a single trace with its observations."""
        trace = self.client.api.trace.get(trace_id)
        data: dict = {
            "id": trace.id,
            "name": trace.name,
            "timestamp": str(getattr(trace, "timestamp", None)),
            "cost": getattr(trace, "total_cost", None),
            "latency": getattr(trace, "latency", None),
            "metadata": getattr(trace, "metadata", None),
        }
        observations = sorted(trace.observations or [], key=_sort_key)

        if analyze:
            from collections import Counter
            tool_counts: Counter = Counter()
            errors = []
            for obs in observations:
                if getattr(obs, "parent_observation_id", None):
                    tool_counts[obs.name or "?"] += 1
                if _has_error_level(obs):
                    errors.append(obs)
            data["tool_counts"] = dict(tool_counts.most_common(20))
            data["errors"] = [_obs_error_dict(obs) for obs in errors]
        else:
            data["observations"] = [_obs_dict(obs, full=full) for obs in observations]

        return data

    def list_failures(self, limit: int = 50, since: str | None = None) -> list[dict]:
        """Return traces that contain error-level observations."""
        kwargs: dict = {"limit": limit, "page": 1}
        if since:
            kwargs["from_timestamp"] = datetime.fromisoformat(since).replace(tzinfo=timezone.utc)
        result = self.client.api.trace.list(**kwargs)
        traces = result.data if hasattr(result, "data") else list(result)

        failures = []
        for t in traces:
            trace = self.client.api.trace.get(t.id)
            errors = [
                o for o in (trace.observations or [])
                if str(getattr(o, "level", "")).upper() in ("ERROR", "OBSERVATIONLEVEL.ERROR")
            ]
            if errors:
                failures.append({
                    "id": t.id,
                    "name": t.name or "",
                    "timestamp": t.timestamp.isoformat() if getattr(t, "timestamp", None) else None,
                    "errors": [_obs_error_dict(o) for o in errors],
                })
        return failures


# --- helpers -----------------------------------------------------------------

def _sort_key(obs):
    epoch = datetime.min.replace(tzinfo=timezone.utc)
    t = getattr(obs, "start_time", None)
    return t if t and t.tzinfo else epoch


def _has_error_level(obs) -> bool:
    level = str(getattr(obs, "level", ""))
    return bool(level) and level not in ("DEFAULT", "ObservationLevel.DEFAULT")


def _obs_dict(obs, full: bool = False) -> dict:
    entry: dict = {
        "name": obs.name or getattr(obs, "type", "?"),
        "level": str(getattr(obs, "level", "")),
        "cost": getattr(obs, "calculated_total_cost", None),
        "latency": getattr(obs, "latency", None),
        "parent_id": getattr(obs, "parent_observation_id", None),
        "status_message": getattr(obs, "status_message", None) if _has_error_level(obs) else None,
    }
    if full:
        entry["input"] = str(obs.input)[:300] if getattr(obs, "input", None) else None
        entry["output"] = str(obs.output)[:300] if getattr(obs, "output", None) else None
    return entry


def _obs_error_dict(obs) -> dict:
    return {
        "name": obs.name or getattr(obs, "type", "?"),
        "level": str(getattr(obs, "level", "")),
        "status_message": getattr(obs, "status_message", None),
        "output": str(getattr(obs, "output", None))[:400] if getattr(obs, "output", None) else None,
    }


# --- module-level singleton --------------------------------------------------

_TRACES = LangfuseTraces()


# --- agent tools -------------------------------------------------------------

async def traces_list(
    limit: int = 20,
    workflow: str | None = None,
    since: str | None = None,
) -> str:
    """List recent Langfuse traces.

    Args:
        limit: Maximum number of traces to return (default 20).
        workflow: Filter by workflow name, e.g. 'cai-solve' or 'cai-address'.
        since: ISO date string — only return traces after this date, e.g. '2026-01-01'.
    """
    traces = _TRACES.list_traces(limit=limit, workflow=workflow, since=since)
    if not traces:
        return "No traces found."
    lines = [f"{'ID':<36} {'NAME':<16} {'TIMESTAMP':<22} {'COST':>9} {'LATENCY':>9}", "-" * 96]
    for t in traces:
        ts = (t["timestamp"] or "?")[:19]
        cost = f"${t['cost']:.4f}" if t["cost"] else "N/A"
        latency = f"{t['latency']:.1f}s" if t["latency"] else "N/A"
        lines.append(f"{t['id']:<36} {t['name']:<16} {ts:<22} {cost:>9} {latency:>9}")
    return "\n".join(lines)


async def traces_show(
    trace_id: str,
    full: bool = False,
    analyze: bool = False,
) -> str:
    """Show details for a specific Langfuse trace.

    Args:
        trace_id: The trace ID to inspect.
        full: Include raw input/output for each observation.
        analyze: Show tool-call counts and error summary instead of full timeline.
    """
    data = _TRACES.show_trace(trace_id, full=full, analyze=analyze)
    lines = [
        f"Trace:     {data['id']}",
        f"Name:      {data['name']}",
        f"Timestamp: {data['timestamp']}",
        f"Cost:      ${data['cost']:.4f}" if data["cost"] else "Cost:      N/A",
        f"Latency:   {data['latency']:.1f}s" if data["latency"] else "Latency:   N/A",
    ]
    if data.get("metadata"):
        lines.append(f"Metadata:  {data['metadata']}")

    if analyze:
        tool_counts = data.get("tool_counts", {})
        errors = data.get("errors", [])
        lines.append(f"\nTool call counts (top 20) — total: {sum(tool_counts.values())}")
        for name, count in tool_counts.items():
            lines.append(f"  {count:>4}  {name}")
        if errors:
            lines.append(f"\nErrors ({len(errors)}):")
            for e in errors:
                lines.append(f"  {e['name']} [{e['level']}]")
                if e.get("status_message"):
                    lines.append(f"    Message: {e['status_message'][:400]}")
                if e.get("output"):
                    lines.append(f"    Output:  {e['output']}")
    else:
        obs_list = data.get("observations", [])
        lines.append(f"\nObservations ({len(obs_list)}):")
        for obs in obs_list:
            indent = "    " if obs.get("parent_id") else "  "
            level_tag = f" [{obs['level']}]" if _has_error_level_str(obs["level"]) else ""
            cost_str = f"  ${obs['cost']:.4f}" if obs.get("cost") else ""
            lat_str = f"  {obs['latency']:.1f}s" if obs.get("latency") else ""
            lines.append(f"{indent}{obs['name']}{level_tag}{cost_str}{lat_str}")
            if level_tag and obs.get("status_message"):
                lines.append(f"{indent}  Error: {obs['status_message']}")
            if full:
                if obs.get("input"):
                    lines.append(f"{indent}  Input:  {obs['input']}")
                if obs.get("output"):
                    lines.append(f"{indent}  Output: {obs['output']}")
    return "\n".join(lines)


async def traces_failures(
    limit: int = 50,
    since: str | None = None,
) -> str:
    """Find Langfuse traces that contain error-level observations.

    Args:
        limit: Maximum number of traces to scan (default 50).
        since: ISO date string — only scan traces after this date, e.g. '2026-01-01'.
    """
    failures = _TRACES.list_failures(limit=limit, since=since)
    if not failures:
        return "No failed traces found in the scanned set."
    lines = []
    for f in failures:
        ts = (f["timestamp"] or "?")[:19]
        lines.append(f"\n[{ts}] {f['name']}  trace_id={f['id']}")
        for e in f["errors"]:
            lines.append(f"  Failed step: {e['name']}")
            if e.get("status_message"):
                lines.append(f"    Message: {e['status_message']}")
            if e.get("output"):
                lines.append(f"    Output:  {e['output']}")
    return "\n".join(lines)


def _has_error_level_str(level: str) -> bool:
    return bool(level) and level not in ("DEFAULT", "ObservationLevel.DEFAULT")


TRACES_LIST_TOOL = Tool(traces_list)
TRACES_SHOW_TOOL = Tool(traces_show)
TRACES_FAILURES_TOOL = Tool(traces_failures)
