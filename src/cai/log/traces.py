"""Langfuse traces client and agent tools for querying cai workflow traces."""
from __future__ import annotations

import asyncio
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
            kwargs["timeout"] = 300
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

    def cost_per_session(
        self,
        limit: int = 100,
        since: str | None = None,
    ) -> list[dict]:
        """Return total cost grouped by Langfuse ``session_id``.

        Sessions group an issue's full lifecycle — the cai-solve run, its PR's
        review-thread runs, and any later conflict-resolves — under a single
        id (see ``cai.log.observability.session_id_for_pr``).
        """
        from collections import defaultdict

        kwargs: dict = {"limit": limit, "page": 1}
        if since:
            kwargs["from_timestamp"] = datetime.fromisoformat(since).replace(tzinfo=timezone.utc)
        result = self.client.api.trace.list(**kwargs)
        traces = result.data if hasattr(result, "data") else list(result)

        session_costs: dict = defaultdict(lambda: {"cost": 0.0, "trace_ids": [], "workflows": []})
        for t in traces:
            session_id = getattr(t, "session_id", None)
            if not session_id:
                continue
            session_costs[session_id]["cost"] += getattr(t, "total_cost", None) or 0.0
            session_costs[session_id]["trace_ids"].append(t.id)
            session_costs[session_id]["workflows"].append(t.name or "")

        groups = [
            {
                "session_id": sid,
                "total_cost": data["cost"],
                "trace_count": len(data["trace_ids"]),
                "trace_ids": data["trace_ids"],
                "workflows": data["workflows"],
            }
            for sid, data in session_costs.items()
        ]
        groups.sort(key=lambda x: x["total_cost"], reverse=True)
        return groups

    def list_session_traces(self, session_id: str, limit: int = 100) -> list[dict]:
        """Return every trace in one session, oldest first."""
        result = self.client.api.trace.list(limit=limit, page=1, session_id=session_id)
        traces = result.data if hasattr(result, "data") else list(result)
        epoch = datetime.min.replace(tzinfo=timezone.utc)
        traces = sorted(
            traces,
            key=lambda t: getattr(t, "timestamp", None) or epoch,
        )
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

    def list_solve_sessions(self, limit: int = 10) -> list[dict]:
        """Return the last N distinct issue-solving sessions, newest first.

        Fetches recent cai-solve traces, groups them by session_id (keeping only
        ``issue-*`` sessions), and returns up to ``limit`` sessions with their
        trace IDs so the caller can drill in with ``list_session_traces``.
        """
        from collections import OrderedDict

        # Over-fetch to have enough unique sessions after deduplication.
        result = self.client.api.trace.list(limit=limit * 10, page=1, name="cai-solve")
        traces = result.data if hasattr(result, "data") else list(result)

        seen: OrderedDict = OrderedDict()
        for t in traces:
            sid = getattr(t, "session_id", None)
            if not sid or not sid.startswith("issue-"):
                continue
            if sid not in seen:
                seen[sid] = {
                    "session_id": sid,
                    "timestamp": t.timestamp.isoformat() if getattr(t, "timestamp", None) else None,
                    "trace_ids": [],
                    "cost": 0.0,
                }
            seen[sid]["trace_ids"].append(t.id)
            seen[sid]["cost"] += getattr(t, "total_cost", None) or 0.0
            if limit and len(seen) >= limit:
                break

        return list(seen.values())

    def most_costly_solve_session(self, n: int = 10) -> dict | None:
        """Return the costliest session among the last N issue-solving sessions.

        Uses ``cost_per_session`` to find all ``issue-*`` sessions, sorts them
        by issue number (descending) to get the N most recent, then returns the
        one with the highest total cost.
        """
        all_groups = [
            g for g in self.cost_per_session(limit=100)
            if g["session_id"].startswith("issue-")
        ]
        if not all_groups:
            return None

        def _issue_number(g: dict) -> int:
            try:
                return int(g["session_id"].split("-", 1)[1])
            except (IndexError, ValueError):
                return 0

        recent = sorted(all_groups, key=_issue_number, reverse=True)[:n]
        return max(recent, key=lambda g: g["total_cost"])

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
        workflow: Filter by workflow name, e.g. 'cai-solve' or 'cai-audit'.
        since: ISO date string — only return traces after this date, e.g. '2026-01-01'.
    """
    traces = await asyncio.to_thread(_TRACES.list_traces, limit, workflow, since)
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
    try:
        data = await asyncio.to_thread(_TRACES.show_trace, trace_id, full, analyze)
    except Exception as exc:
        return f"Could not fetch trace {trace_id}: {exc}"
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
    limit: int = 20,
    since: str | None = None,
) -> str:
    """Find Langfuse traces that contain error-level observations.

    Args:
        limit: Maximum number of traces to scan (default 20). Each trace requires
            a separate API call, so keep this low to avoid timeouts.
        since: ISO date string — only scan traces after this date, e.g. '2026-01-01'.
    """
    try:
        failures = await asyncio.to_thread(_TRACES.list_failures, limit, since)
    except Exception as exc:
        return f"Could not fetch failures: {exc}"
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


async def traces_session_cost(
    limit: int = 100,
    since: str | None = None,
) -> str:
    """Show total LLM cost grouped by Langfuse session id.

    Sessions group an issue's full lifecycle — the cai-solve run, its PR's
    review-thread runs, and any later conflict-resolves — under one id
    (e.g. 'issue-1426', 'pr-1427').

    Args:
        limit: Maximum number of traces to scan (default 100).
        since: ISO date string — only include traces after this date, e.g. '2026-01-01'.
    """
    groups = await asyncio.to_thread(_TRACES.cost_per_session, limit, since)
    if not groups:
        return "No sessioned traces found."
    lines = [f"{'SESSION':<24} {'COST':>10} {'TRACES':>7}  WORKFLOWS", "-" * 80]
    total = 0.0
    for g in groups:
        cost_str = f"${g['total_cost']:.4f}"
        workflows = ", ".join(sorted(set(g["workflows"])))
        lines.append(f"{g['session_id']:<24} {cost_str:>10} {g['trace_count']:>7}  {workflows}")
        total += g["total_cost"]
    lines.append("-" * 80)
    lines.append(f"{'TOTAL':<24} ${total:>9.4f}")
    return "\n".join(lines)


async def traces_session(
    session_id: str,
    limit: int = 100,
) -> str:
    """List every trace in one Langfuse session, oldest first.

    Args:
        session_id: The session id to inspect, e.g. 'issue-1426' or 'pr-1427'.
        limit: Maximum number of traces to return (default 100).
    """
    traces = await asyncio.to_thread(_TRACES.list_session_traces, session_id, limit)
    if not traces:
        return f"No traces found for session {session_id!r}."
    lines = [
        f"Session: {session_id}  ({len(traces)} traces)",
        f"{'ID':<36} {'NAME':<20} {'TIMESTAMP':<22} {'COST':>9} {'LATENCY':>9}",
        "-" * 100,
    ]
    total_cost = 0.0
    for t in traces:
        ts = (t["timestamp"] or "?")[:19]
        cost = f"${t['cost']:.4f}" if t["cost"] else "N/A"
        latency = f"{t['latency']:.1f}s" if t["latency"] else "N/A"
        lines.append(f"{t['id']:<36} {t['name']:<20} {ts:<22} {cost:>9} {latency:>9}")
        total_cost += t["cost"] or 0.0
    lines.append("-" * 100)
    lines.append(f"{'TOTAL':<80} ${total_cost:>9.4f}")
    return "\n".join(lines)


async def traces_solve_sessions(limit: int = 10) -> str:
    """List the last N distinct issue-solving sessions (cai-solve runs).

    Returns one row per session with its session_id, timestamp of the most
    recent cai-solve trace, and all trace IDs belonging to it.  Use
    ``traces_session`` to expand a session into its full trace list.

    Args:
        limit: Number of distinct issue sessions to return (default 10).
    """
    sessions = await asyncio.to_thread(_TRACES.list_solve_sessions, limit)
    if not sessions:
        return "No issue-solving sessions found."
    lines = [f"{'SESSION':<24} {'TIMESTAMP':<22} TRACE IDS", "-" * 100]
    for s in sessions:
        ts = (s["timestamp"] or "?")[:19]
        ids = ", ".join(s["trace_ids"])
        lines.append(f"{s['session_id']:<24} {ts:<22} {ids}")
    return "\n".join(lines)


TRACES_LIST_TOOL = Tool(traces_list)
TRACES_SHOW_TOOL = Tool(traces_show)
TRACES_FAILURES_TOOL = Tool(traces_failures)
TRACES_SESSION_COST_TOOL = Tool(traces_session_cost)
TRACES_SESSION_TOOL = Tool(traces_session)
TRACES_SOLVE_SESSIONS_TOOL = Tool(traces_solve_sessions)
