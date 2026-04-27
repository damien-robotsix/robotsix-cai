"""CLI tool to query and analyze Langfuse traces for cai workflows.

Usage:
    cai-traces list [-n N] [-w cai-solve|cai-address] [--since YYYY-MM-DD]
    cai-traces show TRACE_ID [--full]
    cai-traces failures [-n N] [--since YYYY-MM-DD]
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone


def _get_client():
    if not (os.environ.get("LANGFUSE_PUBLIC_KEY") and os.environ.get("LANGFUSE_SECRET_KEY")):
        print("error: LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY must be set", file=sys.stderr)
        sys.exit(1)

    from langfuse import Langfuse

    kwargs: dict = {
        "public_key": os.environ["LANGFUSE_PUBLIC_KEY"],
        "secret_key": os.environ["LANGFUSE_SECRET_KEY"],
    }
    if base_url := os.environ.get("LANGFUSE_BASE_URL"):
        kwargs["host"] = base_url

    return Langfuse(**kwargs)


def _observations(client, trace_id: str):
    trace = client.api.trace.get(trace_id)
    return trace.observations or []


def cmd_list(args) -> None:
    client = _get_client()

    kwargs: dict = {"limit": args.limit, "page": 1}
    if args.workflow:
        kwargs["name"] = args.workflow
    if args.since:
        kwargs["from_timestamp"] = datetime.fromisoformat(args.since).replace(tzinfo=timezone.utc)

    result = client.api.trace.list(**kwargs)
    traces = result.data if hasattr(result, "data") else list(result)

    if not traces:
        print("No traces found.")
        return

    print(f"{'ID':<36} {'NAME':<16} {'TIMESTAMP':<22} {'COST':>9} {'LATENCY':>9}")
    print("-" * 96)
    for t in traces:
        ts = t.timestamp.strftime("%Y-%m-%d %H:%M:%S") if getattr(t, "timestamp", None) else "?"
        cost = f"${t.total_cost:.4f}" if getattr(t, "total_cost", None) else "    N/A"
        latency = f"{t.latency:.1f}s" if getattr(t, "latency", None) else "    N/A"
        print(f"{t.id:<36} {(t.name or ''):<16} {ts:<22} {cost:>9} {latency:>9}")


def _sort_key(o):
    epoch = datetime.min.replace(tzinfo=timezone.utc)
    t = getattr(o, "start_time", None)
    return t if t and t.tzinfo else epoch


def _level_tag(obs) -> str:
    level = getattr(obs, "level", None)
    s = str(level) if level else ""
    if s and s not in ("DEFAULT", "ObservationLevel.DEFAULT"):
        return f" [{level}]"
    return ""


def cmd_show(args) -> None:
    client = _get_client()
    trace = client.api.trace.get(args.trace_id)

    cost = getattr(trace, "total_cost", None)
    latency = getattr(trace, "latency", None)
    print(f"Trace:     {trace.id}")
    print(f"Name:      {trace.name}")
    print(f"Timestamp: {getattr(trace, 'timestamp', '?')}")
    print(f"Cost:      ${cost:.4f}" if cost else "Cost:      N/A")
    print(f"Latency:   {latency:.1f}s" if latency else "Latency:   N/A")
    if getattr(trace, "metadata", None):
        print(f"Metadata:  {trace.metadata}")

    observations = trace.observations or []
    if not observations:
        print("\nNo observations found.")
        return

    sorted_obs = sorted(observations, key=_sort_key)

    if args.analyze:
        from collections import Counter
        tool_counts: Counter = Counter()
        errors = []
        for obs in sorted_obs:
            parent = getattr(obs, "parent_observation_id", None)
            if parent:
                tool_counts[obs.name or "?"] += 1
            tag = _level_tag(obs)
            if tag:
                errors.append(obs)

        print(f"\nTool call counts (top 20)  — total observations: {len(observations)}")
        for name, count in tool_counts.most_common(20):
            print(f"  {count:>4}  {name}")

        if errors:
            print(f"\nErrors ({len(errors)}):")
            for obs in errors:
                print(f"  {obs.name or '?'}{_level_tag(obs)}")
                msg = getattr(obs, "status_message", None)
                if msg:
                    print(f"    Message: {msg[:400]}")
                out = getattr(obs, "output", None)
                if out:
                    print(f"    Output:  {str(out)[:400]}")
        return

    print(f"\nObservations ({len(observations)}):")
    for obs in sorted_obs:
        tag = _level_tag(obs)
        obs_cost = getattr(obs, "calculated_total_cost", None)
        obs_lat = getattr(obs, "latency", None)
        cost_str = f"  ${obs_cost:.4f}" if obs_cost else ""
        lat_str = f"  {obs_lat:.1f}s" if obs_lat else ""
        parent = getattr(obs, "parent_observation_id", None)
        indent = "    " if parent else "  "
        print(f"{indent}{obs.name or getattr(obs, 'type', '?')}{tag}{cost_str}{lat_str}")
        if tag:
            msg = getattr(obs, "status_message", None)
            if msg:
                print(f"{indent}  Error: {msg}")
        if args.full:
            if getattr(obs, "input", None):
                print(f"{indent}  Input:  {str(obs.input)[:300]}")
            if getattr(obs, "output", None):
                print(f"{indent}  Output: {str(obs.output)[:300]}")


def cmd_failures(args) -> None:
    client = _get_client()

    kwargs: dict = {"limit": args.limit, "page": 1}
    if args.since:
        kwargs["from_timestamp"] = datetime.fromisoformat(args.since).replace(tzinfo=timezone.utc)

    result = client.api.trace.list(**kwargs)
    traces = result.data if hasattr(result, "data") else list(result)

    found_any = False
    for t in traces:
        observations = _observations(client, t.id)  # noqa: uses corrected helper
        errors = [
            o for o in observations
            if str(getattr(o, "level", "")).upper() in ("ERROR", "OBSERVATIONLEVEL.ERROR")
        ]
        if not errors:
            continue

        found_any = True
        ts = t.timestamp.strftime("%Y-%m-%d %H:%M:%S") if getattr(t, "timestamp", None) else "?"
        print(f"\n[{ts}] {t.name}  trace_id={t.id}")
        for obs in errors:
            print(f"  Failed step: {obs.name or getattr(obs, 'type', '?')}")
            msg = getattr(obs, "status_message", None)
            if msg:
                print(f"    Message: {msg}")
            out = getattr(obs, "output", None)
            if out:
                print(f"    Output:  {str(out)[:400]}")

    if not found_any:
        print("No failed traces found in the scanned set.")


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="cai-traces",
        description="Query and analyze Langfuse traces for cai workflows",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_list = sub.add_parser("list", help="List recent traces")
    p_list.add_argument("-n", "--limit", type=int, default=20, metavar="N")
    p_list.add_argument("-w", "--workflow", choices=["cai-solve", "cai-address"], metavar="NAME")
    p_list.add_argument("--since", metavar="YYYY-MM-DD")
    p_list.set_defaults(func=cmd_list)

    p_show = sub.add_parser("show", help="Show details for a specific trace")
    p_show.add_argument("trace_id")
    p_show.add_argument("--full", action="store_true", help="Include input/output for each observation")
    p_show.add_argument("--analyze", action="store_true", help="Show tool-call counts and errors instead of full timeline")
    p_show.set_defaults(func=cmd_show)

    p_fail = sub.add_parser("failures", help="Show traces that contain errors")
    p_fail.add_argument("-n", "--limit", type=int, default=50, metavar="N",
                        help="Max traces to scan (default 50)")
    p_fail.add_argument("--since", metavar="YYYY-MM-DD")
    p_fail.set_defaults(func=cmd_failures)

    args = parser.parse_args()
    try:
        args.func(args)
    except KeyboardInterrupt:
        pass
