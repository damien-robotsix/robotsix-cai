"""Phase E entry point — subcommand dispatcher.

Subcommands:

    python cai.py init      Smoke-test claude -p only if the transcript
                            volume has no prior sessions. Used to seed
                            the self-improvement loop on a fresh
                            install; a no-op once transcripts exist.

    python cai.py analyze   Parse prior transcripts with parse.py, pipe
                            the combined analyzer prompt through
                            claude -p, and publish findings via
                            publish.py.

    python cai.py fix       Score eligible issues by age, category
                            success rate, and prior fix attempts; pick
                            the highest scorer labelled `auto-improve:
                            refined` or `auto-improve:
                            requested` (audit issues reach fix via
                            triage relabelling), run a cheap Haiku
                            pre-screen to classify the issue; spike/
                            ambiguous issues are returned to their origin
                            label without cloning; if actionable, lock it
                            via the `:in-progress` label, clone the repo
                            into /tmp, run 2 serial plan agents (each
                            capped at $1.00; the second sees the first
                            plan and proposes an alternative) to generate
                            candidate fix plans, run a select
                            agent to pick the best plan, then run the fix
                            subagent (full tool permissions) with the
                            selected plan, and open a PR if the agent
                            produced a diff. Rolls back the label on
                            empty diff or any failure.

    python cai.py verify    Mechanical, no-LLM. Walk issues with
                            `:pr-open`, find their linked PR by `Refs`
                            search, and transition the label:
                            merged → `:merged`,
                            closed-unmerged → `:refined`,
                            no-linked-PR → `:raised`.

    python cai.py audit     Periodic queue/PR consistency audit.
                            Deterministically rolls back stale
                            `:in-progress` issues (>6h with no fix
                            activity), then runs a Sonnet-driven
                            semantic check for duplicates, stuck loops,
                            label corruption, etc. Findings are
                            published as `audit:raised` issues in a
                            separate label namespace.

    python cai.py revise    Watch `:pr-open` PRs for new comments and
                            let the fix subagent iterate on the same
                            branch. Force-pushes revisions with
                            `--force-with-lease`.

    python cai.py confirm   Re-analyze the recent transcript window and
                            verify whether `:merged` issues are actually
                            solved. Patterns that disappeared get closed
                            with `:solved`; patterns that persist are
                            re-queued to `:refined` (up to 3 attempts),
                            then escalated to `:needs-human-review`.

    python cai.py review-pr Walk open PRs against main, run a
                            consistency review for ripple effects, and
                            post findings as PR comments. Skips PRs
                            already reviewed at their current HEAD SHA.

    python cai.py merge     Confidence-gated auto-merge for bot PRs.
                            Evaluates each :pr-open PR against its
                            linked issue, posts a verdict comment, and
                            merges when confidence meets the threshold.

    python cai.py refine      Pick the oldest issue labelled
                            `auto-improve:raised`, invoke the
                            cai-refine subagent (read-only) to
                            produce a structured plan, update the
                            issue body, and transition the label to
                            `auto-improve:refined`.

    python cai.py plan      Run the plan-select pipeline on the
                            oldest issue labelled `auto-improve:
                            refined`. Clones the repo into /tmp,
                            runs 2 serial plan agents followed by
                            a select agent, stores the chosen plan
                            in the issue body inside
                            `<!-- cai-plan-start/end -->` markers,
                            and transitions the label from
                            `auto-improve:refined` to
                            `auto-improve:planned`. Invoked as part
                            of `cai.py cycle`; also runnable manually.

    python cai.py spike     Pick the oldest issue labelled
                            `auto-improve:needs-spike`, clone the
                            repo into /tmp, and run the cai-spike
                            subagent to investigate the open
                            research question. Transitions labels
                            based on the outcome: close (findings),
                            :refined (refined issue), or
                            needs-human-review (blocked).

    python cai.py code-audit  Weekly source-code consistency audit.
                            Clones the repo read-only, runs a Sonnet
                            agent that checks for cross-file
                            inconsistencies, dead code, missing
                            references, and similar concrete problems.
                            Findings are published as issues via
                            publish.py with the `code-audit` namespace.

    python cai.py propose     Weekly creative improvement proposal.
                            Clones the repo read-only, runs a creative
                            agent to propose an ambitious improvement,
                            then a review agent to evaluate feasibility.
                            Approved proposals are filed as issues with
                            `auto-improve:raised` so they flow through
                            the refine → fix pipeline.

    python cai.py update-check  Periodic Claude Code release check.
                            Clones the repo, fetches the latest Claude
                            Code releases from GitHub, and runs a Sonnet
                            agent that compares the current pinned
                            version against the latest releases. Findings
                            (new versions, deprecated flags, best
                            practices) are published via publish.py with
                            the `update-check` namespace.

    python cai.py health-report  Automated pipeline health report with
                            anomaly detection. Aggregates cost trends
                            (last 7d vs prior 7d), issue queue counts,
                            pipeline stalls, and fix quality metrics.
                            Flags anomalies with 🔴/🟡/🟢 traffic-light
                            indicators and posts the report as a GitHub
                            issue with the `health-report` label. Use
                            --dry-run to print to stdout without posting.

    python cai.py check-workflows  Periodic GitHub Actions workflow failure
                            monitor. Fetches recent workflow runs, filters
                            out bot branches (auto-improve/), and runs a
                            Haiku agent to identify persistent failures.
                            Findings are published via publish.py with the
                            `check-workflows` namespace. Runs every 6 hours
                            by default (CAI_CHECK_WORKFLOWS_SCHEDULE).

The container runs `entrypoint.sh`, which executes `cai.py cycle` once
synchronously at startup (driving the full issue-solving pipeline:
verify → confirm → drain PRs → refine → plan → fix loop), then hands
off to supercronic. Each cron tick is a fresh process. The pipeline is
driven by a single `CAI_CYCLE_SCHEDULE` cron line; a flock in
`cmd_cycle` serializes overlapping runs so issues are processed one
at a time. Orthogonal tasks (analyze, audit, propose, update-check,
health-report, cost-optimize, check-workflows, code-audit) keep their
own schedules and are not run at startup.

The gh auth check is done once per subcommand invocation. We want a
clear error message in docker logs if credentials ever disappear from
the cai_home volume.

No third-party Python dependencies — only stdlib.
"""

import argparse
import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
import time
import uuid

from datetime import datetime, timedelta, timezone
from pathlib import Path

from publish import ensure_all_labels


REPO = "damien-robotsix/robotsix-cai"
SMOKE_PROMPT = "Say hello in one short sentence."

# Root of claude-code's per-cwd transcript dirs. claude-code writes
# `~/.claude/projects/<sanitized-cwd>/<session-id>.jsonl` for every
# session, so this directory contains one subdir per cwd:
#   * `-app/`            — sessions started by cai.py inside /app
#   * `-tmp-cai-fix-<N>/` — sessions started by the fix subagent in
#                          its per-issue clone under /tmp
# The analyzer parses *all* of them so the fix subagent's tool-rich
# sessions feed back into the next analyzer cycle.
#
# Path is /home/cai/... because the container runs as the non-root
# `cai` user (uid 1000) — see Dockerfile.
TRANSCRIPT_DIR = Path("/home/cai/.claude/projects")

# Files baked into the image alongside cai.py.
PARSE_SCRIPT = Path("/app/parse.py")
PUBLISH_SCRIPT = Path("/app/publish.py")
# Persistent memory file for the code-audit agent. Stored in the
# named-volume log directory so it survives container restarts.
CODE_AUDIT_MEMORY = Path("/var/log/cai/code-audit-memory.md")
# Persistent memory file for the propose agent (same pattern).
PROPOSE_MEMORY = Path("/var/log/cai/propose-memory.md")
# Persistent memory file for the update-check agent.
UPDATE_CHECK_MEMORY = Path("/var/log/cai/update-check-memory.md")
# Persistent memory file for the cost-optimize agent.
COST_OPTIMIZE_MEMORY = Path("/var/log/cai/cost-optimize-memory.md")

# Persistent per-agent memory directory. Each declarative subagent
# has `memory: project` in its frontmatter, which Claude Code stores
# under `.claude/agent-memory/<agent-name>/MEMORY.md` relative to
# the project root. This directory is bind-mounted from the
# `cai_agent_memory` named volume so the memory survives container
# restarts. ALL subagents (both /app agents and the cloned-worktree
# agents) now read/write this path directly because they're all
# invoked with `cwd=/app`. The cloned-worktree agents
# (cai-fix, cai-revise, cai-rebase, cai-review-pr, cai-review-docs, cai-code-audit, cai-propose,
# cai-propose-review, cai-update-check, cai-plan, cai-select, cai-git) operate
# on a clone elsewhere via absolute paths —
# see `_work_directory_block` for the user-message section that
# tells them where the clone is.
AGENT_MEMORY_DIR = Path("/app/.claude/agent-memory")

# Issue lifecycle labels.
LABEL_RAISED = "auto-improve:raised"
LABEL_REQUESTED = "auto-improve:requested"
LABEL_IN_PROGRESS = "auto-improve:in-progress"
LABEL_PR_OPEN = "auto-improve:pr-open"
LABEL_MERGED = "auto-improve:merged"
LABEL_SOLVED = "auto-improve:solved"
LABEL_NO_ACTION = "auto-improve:no-action"
LABEL_NEEDS_SPIKE = "auto-improve:needs-spike"
LABEL_NEEDS_EXPLORATION = "auto-improve:needs-exploration"
LABEL_REFINED = "auto-improve:refined"
LABEL_REVISING = "auto-improve:revising"
LABEL_PARENT = "auto-improve:parent"
LABEL_MERGE_BLOCKED = "merge-blocked"
LABEL_AUDIT_RAISED = "audit:raised"
LABEL_AUDIT_NEEDS_HUMAN = "audit:needs-human"
LABEL_HUMAN_SUBMITTED = "human:submitted"
LABEL_PLANNED = "auto-improve:planned"
LABEL_PLAN_APPROVED = "auto-improve:plan-approved"

# PR-level label applied by `cai merge` when the verdict is below the
# auto-merge threshold. Lets a human filter open PRs that are waiting
# on their decision (`label:needs-human-review`). Issue #216.
LABEL_PR_NEEDS_HUMAN = "needs-human-review"


# ---------------------------------------------------------------------------
# Run log
# ---------------------------------------------------------------------------

LOG_PATH = Path("/var/log/cai/cai.log")
COST_LOG_PATH = Path("/var/log/cai/cai-cost.jsonl")
REVIEW_PR_PATTERN_LOG = Path("/var/log/cai/review-pr-patterns.jsonl")
OUTCOME_LOG_PATH = Path("/var/log/cai/cai-outcomes.jsonl")
ACTIVE_JOB_PATH = Path("/var/log/cai/cai-active.json")


def _write_active_job(cmd: str, target_type: str, target_id: int | None) -> None:
    """Write active-job state for observability. Never raises."""
    try:
        ACTIVE_JOB_PATH.parent.mkdir(parents=True, exist_ok=True)
        ACTIVE_JOB_PATH.write_text(json.dumps({
            "pid": os.getpid(),
            "cmd": cmd,
            "target_type": target_type,
            "target_id": target_id,
            "start_ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }))
    except OSError:
        pass


def _clear_active_job() -> None:
    """Clear active-job state file. Never raises."""
    try:
        ACTIVE_JOB_PATH.write_text("{}")
    except OSError:
        pass


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


def _get_issue_category(issue: dict) -> str:
    """Return the category label value for *issue*, or ``'(unknown)'`` if absent."""
    for ln in (lbl["name"] for lbl in issue.get("labels", [])):
        if ln.startswith("category:"):
            return ln.split(":", 1)[1]
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


def _load_outcome_counts(days: int = 90) -> dict:
    """Read OUTCOME_LOG_PATH and return per-category {total, solved} counts.

    Filters to trailing `days` days. Malformed lines are skipped silently.
    Returns an empty dict if the file is missing or unreadable.
    """
    if not OUTCOME_LOG_PATH.exists():
        return {}
    cutoff_ts = datetime.now(timezone.utc).timestamp() - days * 86400
    counts: dict = {}  # category -> {"total": N, "solved": N}
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
                ts = row.get("ts", "")
                try:
                    row_ts = datetime.strptime(
                        ts, "%Y-%m-%dT%H:%M:%SZ"
                    ).replace(tzinfo=timezone.utc).timestamp()
                except ValueError:
                    continue
                if row_ts < cutoff_ts:
                    continue
                cat = row.get("category") or "(unknown)"
                outcome = row.get("outcome", "")
                bucket = counts.setdefault(cat, {"total": 0, "solved": 0})
                bucket["total"] += 1
                if outcome == "solved":
                    bucket["solved"] += 1
    except OSError:
        return {}
    return counts


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


def _load_cost_log(days: int = 7) -> list[dict]:
    """Read COST_LOG_PATH and return rows from the last `days` days.

    Each row is a dict as written by `log_cost`. Malformed lines are
    skipped silently. Returns an empty list if the file is missing or
    unreadable. Used by both `_build_cost_summary` (audit prompt) and
    `cmd_cost_report` (host-facing report).
    """
    if not COST_LOG_PATH.exists():
        return []
    cutoff_ts = datetime.now(timezone.utc).timestamp() - days * 86400
    rows: list[dict] = []
    try:
        with COST_LOG_PATH.open("r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                ts = row.get("ts") or ""
                try:
                    row_ts = datetime.strptime(
                        ts, "%Y-%m-%dT%H:%M:%SZ",
                    ).replace(tzinfo=timezone.utc).timestamp()
                except ValueError:
                    continue
                if row_ts >= cutoff_ts:
                    rows.append(row)
    except Exception:
        return []
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


def _build_cost_summary(days: int = 7, top_n: int = 10) -> str:
    """Build a markdown cost summary for the cai-audit user message.

    Returns an empty string if no cost rows exist for the window.
    Otherwise emits a section with per-category aggregates and the
    top-N most expensive individual invocations, so the audit agent
    can spot cost outliers (a single invocation that dwarfs the
    median, or a category that dominates total spend).
    """
    rows = _load_cost_log(days=days)
    if not rows:
        return ""

    # Per-category aggregates: total cost, call count, mean cost.
    cats: dict[str, dict] = {}
    grand_total = 0.0
    for r in rows:
        cat = r.get("category") or "(unknown)"
        cost = r.get("cost_usd") or 0.0
        try:
            cost = float(cost)
        except (TypeError, ValueError):
            cost = 0.0
        bucket = cats.setdefault(cat, {"calls": 0, "cost": 0.0})
        bucket["calls"] += 1
        bucket["cost"] += cost
        grand_total += cost

    cat_lines = []
    for cat, b in sorted(cats.items(), key=lambda kv: -kv[1]["cost"]):
        share = (b["cost"] / grand_total * 100.0) if grand_total else 0.0
        mean = b["cost"] / b["calls"] if b["calls"] else 0.0
        cat_lines.append(
            f"| {cat} | {b['calls']} | ${b['cost']:.4f} "
            f"({share:.1f}%) | ${mean:.4f} |"
        )

    # Top-N most expensive individual invocations.
    top = sorted(
        rows,
        key=lambda r: float(r.get("cost_usd") or 0.0),
        reverse=True,
    )[:top_n]
    top_lines = []
    for r in top:
        cost = float(r.get("cost_usd") or 0.0)
        top_lines.append(
            f"| {r.get('ts', '')} | {r.get('category', '')} | "
            f"{r.get('agent', '')} | ${cost:.4f} | "
            f"{r.get('num_turns', '')} | "
            f"{(r.get('input_tokens') or 0) + (r.get('output_tokens') or 0)} |"
        )

    return (
        f"## Cost summary (last {days}d, total ${grand_total:.4f} "
        f"across {len(rows)} invocations)\n\n"
        "### Per-category totals\n\n"
        "| category | calls | total cost (share) | mean cost |\n"
        "|---|---|---|---|\n"
        + "\n".join(cat_lines)
        + "\n\n"
        f"### Top {len(top_lines)} most expensive individual invocations\n\n"
        "| ts | category | agent | cost | turns | tokens |\n"
        "|---|---|---|---|---|---|\n"
        + "\n".join(top_lines)
        + "\n"
    )


# ---------------------------------------------------------------------------
# Common helpers
# ---------------------------------------------------------------------------

def _run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    """Thin wrapper around subprocess.run with text mode and check=False."""
    return subprocess.run(cmd, text=True, check=False, **kwargs)


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
    "analyze", "fix", "audit"). `agent` records the subagent name
    (e.g. "cai-fix") if applicable.

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
        # backwards compatible. If `result` is missing, fall back to
        # the raw envelope so callers still see *something*.
        if "result" in envelope and isinstance(envelope["result"], str):
            proc.stdout = envelope["result"]

    return proc


def _gh_json(args: list[str]):
    """Run a gh command that prints JSON; return the parsed result.

    Raises on non-zero exit (gh failures should be loud).
    """
    result = subprocess.run(
        ["gh"] + args,
        text=True,
        check=True,
        capture_output=True,
    )
    return json.loads(result.stdout) if result.stdout.strip() else None


def check_gh_auth() -> int:
    """Fail fast if `gh` is not authenticated."""
    result = _run(["gh", "auth", "status"], capture_output=True)
    if result.returncode != 0:
        print("[cai] ERROR: gh is not authenticated in this container.", file=sys.stderr)
        print("       Credentials are expected in the cai_home volume.", file=sys.stderr)
        print("       Run the installer's login step, or do it manually:", file=sys.stderr)
        print("         docker compose run --rm cai gh auth login", file=sys.stderr)
        print(file=sys.stderr)
        print(result.stderr.strip() or result.stdout.strip(), file=sys.stderr)
        return 1
    return 0


def check_claude_auth() -> int:
    """Fail fast if `claude` is not authenticated.

    Two valid auth modes for the headless container:
      1. OAuth: credentials sit in the `cai_home` named volume
         (under `/home/cai/.claude/.credentials.json` plus the
         `/home/cai/.claude.json` runtime config sibling file).
         Verified by `claude auth status`.
      2. API key: `ANTHROPIC_API_KEY` is set in the env. claude-code
         uses it directly without needing the OAuth credentials file.

    If neither mode is configured, claude-code will 401 on the first
    API call with a confusing error. Catch the misconfiguration up
    front so the user gets a clear next-step instruction.
    """
    # API-key mode is checked first because it's a single env-var test
    # and doesn't require shelling out.
    if os.environ.get("ANTHROPIC_API_KEY"):
        return 0

    result = _run(["claude", "auth", "status", "--text"], capture_output=True)
    if result.returncode != 0:
        print("[cai] ERROR: claude is not authenticated in this container.", file=sys.stderr)
        print("       Credentials are expected in the cai_home volume,", file=sys.stderr)
        print("       OR set ANTHROPIC_API_KEY in your .env file.", file=sys.stderr)
        print("       Authenticate by opening the claude REPL — it auto-prompts", file=sys.stderr)
        print("       for OAuth login on first start:", file=sys.stderr)
        print("         docker compose run --rm -it cai claude", file=sys.stderr)
        print("       Then exit the REPL gracefully (/exit or Ctrl-D).", file=sys.stderr)
        print(file=sys.stderr)
        print(result.stderr.strip() or result.stdout.strip(), file=sys.stderr)
        return 1
    return 0


def _transcript_dir_is_empty() -> bool:
    if not TRANSCRIPT_DIR.exists():
        return True
    return not any(TRANSCRIPT_DIR.rglob("*.jsonl"))


# ---------------------------------------------------------------------------
# init
# ---------------------------------------------------------------------------

def cmd_init(args) -> int:
    """Seed the loop with a smoke test, only if nothing exists yet."""
    if not _transcript_dir_is_empty():
        print("[cai init] transcripts already present; skipping smoke test", flush=True)
        log_run("init", ran_smoke_test=False, exit=0)
        return 0

    print("[cai init] no prior transcripts; running smoke test to seed loop", flush=True)
    result = _run_claude_p(
        ["claude", "-p", SMOKE_PROMPT],
        category="init",
    )
    # _run_claude_p forces capture_output=True so the smoke test no
    # longer streams to the terminal. Print the result text now so
    # the user still sees that the loop seeded successfully.
    if result.stdout:
        print(result.stdout, flush=True)
    rc = result.returncode
    if rc != 0:
        print(f"[cai init] smoke test failed (exit {rc})", flush=True)
    log_run("init", ran_smoke_test=True, exit=rc)
    return rc


# ---------------------------------------------------------------------------
# analyze
# ---------------------------------------------------------------------------

def _fetch_closed_auto_improve_issues(limit: int = 50) -> list[dict]:
    """Return recently closed `auto-improve` issues with closing rationale.

    For each closed issue, the "rationale" is the last comment posted
    before the issue was closed by a non-bot author. Bot comments are
    skipped because they are status updates, not reasoning. If no
    human rationale comment exists, the rationale is left empty and
    the analyzer can fall back to the issue body.
    """
    try:
        issues = _gh_json([
            "issue", "list",
            "--repo", REPO,
            "--label", "auto-improve",
            "--state", "closed",
            "--json",
            "number,title,labels,closedAt,comments",
            "--limit", str(limit),
        ]) or []
    except subprocess.CalledProcessError:
        return []

    result = []
    for issue in issues:
        comments = issue.get("comments") or []
        rationale = ""
        rationale_author = ""
        # Walk comments newest-first; pick the first non-bot.
        for c in reversed(comments):
            author = (c.get("author") or {}).get("login", "")
            if not author or author.endswith("[bot]") or author == "github-actions":
                continue
            body = (c.get("body") or "").strip()
            if not body:
                continue
            rationale = body[:600]
            rationale_author = author
            break
        result.append({
            "number": issue["number"],
            "title": issue["title"],
            "labels": [lbl["name"] for lbl in issue.get("labels", [])],
            "closedAt": issue.get("closedAt", ""),
            "rationale": rationale,
            "rationale_author": rationale_author,
        })
    return result


def _review_pr_pattern_summary() -> str:
    """Read the review-pr pattern log and return a markdown summary block.

    Filters to the last 30 days.  Returns an empty string if no data exists.
    """
    if not REVIEW_PR_PATTERN_LOG.exists():
        return ""

    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=30)
    # category -> list of PR numbers that surfaced it
    category_prs: dict[str, list[int]] = {}
    total_with_findings = 0
    total_clean = 0

    try:
        with REVIEW_PR_PATTERN_LOG.open("r") as fh:
            for raw_line in fh:
                raw_line = raw_line.strip()
                if not raw_line:
                    continue
                try:
                    entry = json.loads(raw_line)
                except json.JSONDecodeError:
                    continue
                # Parse timestamp and apply 30-day filter
                try:
                    ts_str = entry.get("ts", "")
                    ts = datetime.fromisoformat(ts_str.rstrip("Z"))
                    if ts < cutoff:
                        continue
                except (ValueError, AttributeError):
                    continue
                categories = entry.get("categories", [])
                pr_num = entry.get("pr")
                if categories:
                    total_with_findings += 1
                    for cat in categories:
                        category_prs.setdefault(cat, [])
                        if pr_num is not None and pr_num not in category_prs[cat]:
                            category_prs[cat].append(pr_num)
                else:
                    total_clean += 1
    except OSError:
        return ""

    if not category_prs and total_clean == 0:
        return ""

    lines = [
        "",
        "## Review-PR finding patterns (last 30 days)",
        "",
        "Summary of recurring ripple-effect categories found during PR reviews:",
        "",
        "| Category | Count | Recent PRs |",
        "|---|---|---|",
    ]
    for cat, prs in sorted(category_prs.items(), key=lambda kv: -len(kv[1])):
        recent = ", ".join(f"#{p}" for p in prs[-5:])
        lines.append(f"| {cat} | {len(prs)} | {recent} |")

    lines += [
        "",
        f"Total reviews with findings: {total_with_findings}",
        f"Total clean reviews: {total_clean}",
        "",
    ]
    return "\n".join(lines)


def cmd_analyze(args) -> int:
    """Parse prior transcripts, ask claude to analyze, publish findings."""
    print("[cai analyze] running self-analyzer", flush=True)
    t0 = time.monotonic()

    if not TRANSCRIPT_DIR.exists():
        print(
            f"[cai analyze] no transcript dir at {TRANSCRIPT_DIR}; nothing to analyze",
            flush=True,
        )
        log_run("analyze", repo=REPO, sessions=0, tool_calls=0,
                in_tokens=0, out_tokens=0, duration="0s", exit=0)
        return 0

    parsed = _run(
        ["python", str(PARSE_SCRIPT), str(TRANSCRIPT_DIR)],
        capture_output=True,
    )
    if parsed.returncode != 0:
        print(
            f"[cai analyze] parse.py failed (exit {parsed.returncode}):\n{parsed.stderr}",
            flush=True,
        )
        dur = f"{int(time.monotonic() - t0)}s"
        log_run("analyze", repo=REPO, duration=dur, exit=parsed.returncode)
        return parsed.returncode

    parsed_signals = parsed.stdout.strip()

    # Extract stats from parse.py's JSON output.
    try:
        signals = json.loads(parsed_signals)
    except (json.JSONDecodeError, ValueError):
        signals = {}
    tool_calls = signals.get("tool_call_count", 0)
    token_usage = signals.get("token_usage", {})
    in_tokens = token_usage.get("input_tokens", 0)
    out_tokens = token_usage.get("output_tokens", 0)

    # Use the session count reported by parse.py (files actually read
    # after applying time and count windows) instead of counting all
    # .jsonl files on disk — the latter overstates what was analyzed.
    session_count = signals.get("session_count", 0)

    if in_tokens > 0 and in_tokens < 500:
        print(
            f"[cai analyze] WARNING: in_tokens={in_tokens} is below the "
            f"expected floor of 500 — the transcript window may be too "
            f"narrow or session files may be nearly empty",
            flush=True,
        )

    # Fetch currently open auto-improve issues so the analyzer can
    # avoid raising duplicates (semantic dedup, not just fingerprint).
    _STATE_PRIORITY = {
        LABEL_IN_PROGRESS: 0,
        LABEL_PR_OPEN: 1,
        LABEL_NEEDS_SPIKE: 2,
        LABEL_PLAN_APPROVED: 3,
        LABEL_REFINED: 3,
        LABEL_PLANNED: 3,
        LABEL_RAISED: 4,
        LABEL_HUMAN_SUBMITTED: 4,
        LABEL_MERGED: 5,
        LABEL_REQUESTED: 6,
    }

    def _issue_state_label(issue):
        label_names = [lbl["name"] for lbl in issue.get("labels", [])]
        best = None
        for name in label_names:
            if name in _STATE_PRIORITY:
                if best is None or _STATE_PRIORITY[name] < _STATE_PRIORITY[best]:
                    best = name
        if best is None:
            return "other"
        # Strip the 'auto-improve:' prefix for readability.
        return best.split(":", 1)[1] if ":" in best else best

    try:
        existing_issues = _gh_json([
            "issue", "list",
            "--repo", REPO,
            "--label", "auto-improve",
            "--state", "open",
            "--json", "number,title,labels",
            "--limit", "30",
        ]) or []
    except subprocess.CalledProcessError:
        existing_issues = []

    issues_block = ""
    if existing_issues:
        lines = []
        for ei in existing_issues:
            state = _issue_state_label(ei)
            lines.append(f"- #{ei['number']} [{state}] {ei['title']}")
        issues_block = (
            "\n\n## Currently open auto-improve issues\n\n"
            + "\n".join(lines)
            + "\n"
        )

    # The system prompt, tool allowlist, and model choice all live
    # in `.claude/agents/cai-analyze.md`. Durable per-agent learnings
    # live in its `memory: project` pool. The wrapper only passes
    # dynamic per-run context (parsed signals, open issues, and
    # review-pr pattern history) via stdin as the user message.
    # Closed-issue rationale lookup is now on-demand via the
    # skill:look-up-closed-finding plugin skill.
    review_pr_block = _review_pr_pattern_summary()
    user_message = (
        "## Parsed signals\n\n"
        "```json\n"
        f"{parsed_signals}\n"
        "```\n"
        f"{issues_block}"
        f"{review_pr_block}"
    )

    _write_active_job("analyze", "none", None)
    try:
        analyzer = _run_claude_p(
            ["claude", "-p", "--agent", "cai-analyze"],
            category="analyze",
            agent="cai-analyze",
            input=user_message,
        )
        print(analyzer.stdout, flush=True)
        if analyzer.returncode != 0:
            print(
                f"[cai analyze] claude -p failed (exit {analyzer.returncode}):\n"
                f"{analyzer.stderr}",
                flush=True,
            )
            dur = f"{int(time.monotonic() - t0)}s"
            log_run("analyze", repo=REPO, sessions=session_count,
                    tool_calls=tool_calls, in_tokens=in_tokens,
                    out_tokens=out_tokens, duration=dur, exit=analyzer.returncode)
            return analyzer.returncode

        print("[cai analyze] publishing findings", flush=True)
        published = _run(
            ["python", str(PUBLISH_SCRIPT)],
            input=analyzer.stdout,
        )
        dur = f"{int(time.monotonic() - t0)}s"
        log_run("analyze", repo=REPO, sessions=session_count,
                tool_calls=tool_calls, in_tokens=in_tokens,
                out_tokens=out_tokens, duration=dur, exit=published.returncode)
        return published.returncode
    finally:
        _clear_active_job()


# ---------------------------------------------------------------------------
# fix
# ---------------------------------------------------------------------------

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slugify(text: str, max_len: int = 50) -> str:
    """Branch-friendly slug — lowercase ascii, dashes, no leading/trailing."""
    slug = _SLUG_RE.sub("-", text.lower()).strip("-")
    if len(slug) > max_len:
        slug = slug[:max_len].rstrip("-")
    return slug or "fix"


def _gh_user_identity() -> tuple[str, str]:
    """Resolve the gh-token owner's git name and email."""
    user = _gh_json(["api", "user"])
    name = user.get("name") or user["login"]
    email = user.get("email") or f"{user['id']}+{user['login']}@users.noreply.github.com"
    return name, email


def _recover_stale_pr_open(issues: list[dict], *, log_prefix: str = "cai") -> list[dict]:
    """Transition :pr-open issues whose linked PR was closed (unmerged) back to :refined.

    Also recovers issues with no linked PR at all (dangling :pr-open).
    Returns the list of issues that were successfully recovered.
    """
    recovered: list[dict] = []
    subcmd = log_prefix.split()[-1]
    for issue in issues:
        if LABEL_IN_PROGRESS in {lbl["name"] for lbl in issue.get("labels", [])}:
            continue
        pr = _find_linked_pr(issue["number"])
        issue_labels = {lbl["name"] for lbl in issue.get("labels", [])}
        raised_label = LABEL_AUDIT_RAISED if LABEL_AUDIT_RAISED in issue_labels else (LABEL_HUMAN_SUBMITTED if LABEL_HUMAN_SUBMITTED in issue_labels else LABEL_RAISED)
        remove_labels = [LABEL_PR_OPEN, LABEL_MERGE_BLOCKED, LABEL_REVISING]
        if pr is None:
            if _set_labels(issue["number"], add=[raised_label], remove=remove_labels, log_prefix=log_prefix):
                comment = (
                    "## Auto-improve: rolling back to :raised\n\n"
                    "No linked PR found for this `:pr-open` issue. "
                    "Resetting to `:raised` so the refine subagent can re-structure it "
                    "and the fix subagent can then attempt a fresh fix.\n\n"
                    f"---\n_Rolled back automatically by `{log_prefix}`._"
                )
                _run(["gh", "issue", "comment", str(issue["number"]),
                      "--repo", REPO, "--body", comment], capture_output=True)
                log_run(subcmd, repo=REPO, issue=issue["number"],
                        pr=0, result="rollback_no_pr", exit=0)
                print(
                    f"[{log_prefix}] recovered stale :pr-open on #{issue['number']} "
                    f"(no linked PR found)",
                    flush=True,
                )
                recovered.append(issue)
            continue
        state = (pr.get("state") or "").upper()
        if state == "CLOSED":
            closed_raised_label = LABEL_AUDIT_RAISED if LABEL_AUDIT_RAISED in issue_labels else LABEL_REFINED
            if _set_labels(issue["number"], add=[closed_raised_label], remove=remove_labels, log_prefix=log_prefix):
                comment = (
                    "## Auto-improve: rolling back to :refined\n\n"
                    f"Linked PR #{pr['number']} was closed without merging. "
                    "Resetting this issue to `:refined` so the fix subagent can "
                    "re-attempt on the next tick (bypassing the refine step since "
                    "the issue was already structured before the previous fix attempt).\n\n"
                    f"---\n_Rolled back automatically by `{log_prefix}`._"
                )
                _run(["gh", "issue", "comment", str(issue["number"]),
                      "--repo", REPO, "--body", comment], capture_output=True)
                log_run(subcmd, repo=REPO, issue=issue["number"],
                        pr=pr["number"], result="rollback_closed_pr", exit=0)
                print(
                    f"[{log_prefix}] recovered stale :pr-open on #{issue['number']} "
                    f"(PR #{pr['number']} closed unmerged)",
                    flush=True,
                )
                recovered.append(issue)
    return recovered


# Labels that indicate an issue is already managed by the auto-improve
# pipeline (or the audit pipeline).  Issues carrying any of these are NOT
# eligible for automatic ingestion.
_MANAGED_LABEL_PREFIXES = ("auto-improve:", "audit:", "human:")


def _ingest_unlabeled_issues() -> list[dict]:
    """Find open issues with no pipeline label and tag them :raised.

    Returns the list of issues that were ingested.
    """
    try:
        all_open = _gh_json([
            "issue", "list",
            "--repo", REPO,
            "--state", "open",
            "--json", "number,title,labels",
            "--limit", "200",
        ]) or []
    except subprocess.CalledProcessError as e:
        print(
            f"[cai ingest] gh issue list failed:\n{e.stderr}",
            file=sys.stderr,
        )
        return []

    ingested: list[dict] = []
    for issue in all_open:
        label_names = {lbl["name"] for lbl in issue.get("labels", [])}
        if any(
            name.startswith(prefix)
            for name in label_names
            for prefix in _MANAGED_LABEL_PREFIXES
        ):
            continue  # already in the pipeline
        _set_labels(
            issue["number"],
            add=["auto-improve", LABEL_RAISED],
            log_prefix="cai ingest",
        )
        print(
            f"[cai ingest] #{issue['number']}: {issue['title']} → :raised",
            flush=True,
        )
        ingested.append(issue)
    return ingested


def _select_fix_target():
    """Return the highest-scored open issue eligible for the fix subagent.

    Scoring: age_days × category_success_rate × (1 / max(1, prior_attempts)).
    Categories with fewer than 3 observations get a neutral prior of 0.60.
    This replaces the previous FIFO (oldest-first) selection.

    Eligible = labelled `:refined` or `:requested`, NOT labelled
    `:in-progress` or `:pr-open`.  `audit:raised` issues are handled
    exclusively by the audit-triage agent — only issues that triage
    re-labels to `auto-improve:raised` (and subsequently refine to
    `auto-improve:refined`) enter the fix pipeline.
    If no candidates are found, attempts to recover stale `:pr-open`
    issues whose linked PR was closed unmerged or that have no linked PR.

    NOTE: `:planned` and `:plan-approved` issues are intentionally NOT
    picked up here.  `:planned` issues are waiting for human approval;
    `:plan-approved` issues will be wired into this function in Step 3 of
    the plan-gate sub-issue chain (#481).  Until then, they sit in the
    queue without being consumed by the fix agent.
    """
    candidates: dict[int, dict] = {}
    for label in (LABEL_REFINED, LABEL_REQUESTED):
        try:
            issues = _gh_json([
                "issue", "list",
                "--repo", REPO,
                "--label", label,
                "--state", "open",
                "--json", "number,title,body,labels,createdAt,comments",
                "--limit", "100",
            ]) or []
        except subprocess.CalledProcessError as e:
            print(
                f"[cai fix] gh issue list (label={label}) failed:\n{e.stderr}",
                file=sys.stderr,
            )
            return None
        for issue in issues:
            label_names = {lbl["name"] for lbl in issue.get("labels", [])}
            if LABEL_IN_PROGRESS in label_names or LABEL_PR_OPEN in label_names:
                continue
            candidates[issue["number"]] = issue

    if not candidates:
        # Recover stale :pr-open issues whose linked PR was closed (unmerged) or that have no linked PR.
        # This handles cases where the verify step failed to transition them
        # back to :refined (e.g. due to GitHub search indexing delays).
        try:
            pr_open_issues = _gh_json([
                "issue", "list",
                "--repo", REPO,
                "--label", LABEL_PR_OPEN,
                "--state", "open",
                "--json", "number,title,body,labels,createdAt,comments",
                "--limit", "100",
            ]) or []
        except subprocess.CalledProcessError:
            pr_open_issues = []
        for issue in _recover_stale_pr_open(pr_open_issues, log_prefix="cai fix"):
            candidates[issue["number"]] = issue

    if not candidates:
        return None

    # Enforce step ordering for multi-step sub-issues.
    # Group by parent, keep only the lowest-step candidate per parent,
    # and only if its prior step is done (closed).
    parent_groups: dict[int, list[tuple[int, int, dict]]] = {}
    regular: dict[int, dict] = {}
    for num, issue in candidates.items():
        body = issue.get("body") or ""
        pm = re.search(r"<!-- parent: #(\d+) -->", body)
        sm = re.search(r"<!-- step: (\d+) -->", body)
        if pm and sm:
            parent_groups.setdefault(int(pm.group(1)), []).append(
                (int(sm.group(1)), num, issue)
            )
        else:
            regular[num] = issue
    for parent_num, group in parent_groups.items():
        group.sort()  # sort by step number (lowest first)
        step_num, num, issue = group[0]
        if step_num > 1 and not _check_parent_step_done(parent_num, step_num - 1):
            continue  # prior step not done; skip entire group
        regular[num] = issue
    candidates = regular

    if not candidates:
        return None

    # Score candidates by age, category success rate, and prior fix attempts.
    outcome_stats = _load_outcome_stats()
    default_rate = 0.60

    def _score(issue: dict) -> float:
        # Age in days (older = higher base score).
        try:
            created = datetime.strptime(
                issue["createdAt"], "%Y-%m-%dT%H:%M:%SZ"
            ).replace(tzinfo=timezone.utc)
            age_days = max(1.0, (datetime.now(timezone.utc) - created).total_seconds() / 86400)
        except (ValueError, KeyError):
            age_days = 1.0
        # Category success rate.
        cat = _get_issue_category(issue)
        rate = outcome_stats.get(cat, default_rate)
        # Prior fix attempts (from closed unmerged PRs).
        prior = len(_fetch_previous_fix_attempts(issue["number"]))
        return age_days * rate * (1.0 / max(1, prior))

    return max(candidates.values(), key=_score)


def _select_plan_target(issue_number: int | None = None):
    """Return the oldest open :refined issue eligible for planning, or None.

    If *issue_number* is given, fetch that issue directly (validating it is
    open and not locked).  Otherwise query for the oldest :refined issue
    that is not :in-progress or :pr-open.
    """
    if issue_number is not None:
        try:
            issue = _gh_json([
                "issue", "view", str(issue_number),
                "--repo", REPO,
                "--json", "number,title,body,labels,state,createdAt,comments",
            ])
        except subprocess.CalledProcessError as e:
            print(f"[cai plan] gh issue view #{issue_number} failed:\n{e.stderr}",
                  file=sys.stderr)
            return None
        if issue.get("state", "").upper() != "OPEN":
            print(f"[cai plan] issue #{issue_number} is not open; nothing to do",
                  flush=True)
            return None
        label_names = {lbl["name"] for lbl in issue.get("labels", [])}
        if LABEL_IN_PROGRESS in label_names or LABEL_PR_OPEN in label_names:
            print(f"[cai plan] issue #{issue_number} is locked; skipping",
                  flush=True)
            return None
        return issue

    # Queue-based: oldest :refined issue not locked.
    try:
        candidates = _gh_json([
            "issue", "list",
            "--repo", REPO,
            "--label", LABEL_REFINED,
            "--state", "open",
            "--json", "number,title,body,labels,createdAt,comments",
            "--limit", "100",
        ]) or []
    except subprocess.CalledProcessError as e:
        print(f"[cai plan] gh issue list failed:\n{e.stderr}",
              file=sys.stderr)
        return None
    candidates = [
        c for c in candidates
        if not {lbl["name"] for lbl in c.get("labels", [])}
            & {LABEL_IN_PROGRESS, LABEL_PR_OPEN}
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda c: c.get("createdAt", ""))


def _set_labels(issue_number: int, *, add: list[str] = (), remove: list[str] = (), log_prefix: str = "cai fix") -> bool:
    """Add and/or remove labels on an issue. Returns True on success."""
    # Auto-add the base label for any state-prefixed label being added.
    # This is defensive: create_issue already applies base labels, but
    # auto-adding here self-heals issues that lost theirs.
    _BASE_NAMESPACES = {"auto-improve", "audit", "check-workflows"}
    auto_added_bases: set[str] = set()
    for label in add:
        if ":" in label:
            base = label.split(":", 1)[0]
            if base in _BASE_NAMESPACES and base not in add:
                auto_added_bases.add(base)
    effective_add = list(add) + sorted(auto_added_bases)

    args = ["issue", "edit", str(issue_number), "--repo", REPO]
    for label in effective_add:
        args.extend(["--add-label", label])
    for label in remove:
        args.extend(["--remove-label", label])
    result = _run(["gh"] + args, capture_output=True)
    if result.returncode != 0:
        print(
            f"[{log_prefix}] failed to update labels on #{issue_number}:\n{result.stderr}",
            file=sys.stderr,
        )
        return False
    return True


def _issue_has_label(issue_number: int, label: str) -> bool:
    """Re-fetch an issue's labels and check for *label*. Avoids stale-snapshot races."""
    try:
        issue = _gh_json([
            "issue", "view", str(issue_number),
            "--repo", REPO,
            "--json", "labels",
        ])
    except subprocess.CalledProcessError:
        return False
    return label in [l["name"] for l in (issue or {}).get("labels", [])]


def _build_issue_block(issue: dict) -> str:
    """Build the issue block shared by plan, select, and fix agents."""
    block = (
        f"## Issue\n\n"
        f"### #{issue['number']} — {issue['title']}\n\n"
        f"{issue.get('body') or '(no body)'}\n"
    )
    comments = issue.get("comments") or []
    if comments:
        block += "\n### Comments\n\n"
        for c in comments:
            author = c.get("author", {}).get("login", "unknown")
            body = c.get("body", "")
            block += f"**{author}:**\n{body}\n\n"
    return block


def _build_fix_user_message(issue: dict, attempt_history_block: str = "") -> str:
    """Build the dynamic per-run user message for the cai-fix agent.

    The system prompt, tool allowlist, and hard rules live in
    `.claude/agents/cai-fix.md`; durable per-agent learnings live
    in its `memory: project` pool. The wrapper passes the issue
    body, reviewer comments, and (when available) a summary of
    prior closed PRs for this issue.
    """
    return _build_issue_block(issue) + attempt_history_block


def _fetch_previous_fix_attempts(issue_number: int) -> list[dict]:
    """Retrieve closed, unmerged PRs for this issue and extract merge verdicts.

    Returns a list of dicts with keys: pr_number, title, merge_verdict,
    review_summary. Entries with no extractable verdict are omitted.
    Capped at the 3 most recently created PRs.
    """
    try:
        prs = _gh_json([
            "pr", "list",
            "--repo", REPO,
            "--state", "closed",
            "--search", f'"Refs {REPO}#{issue_number}" in:body',
            "--json", "number,title,headRefName,createdAt,mergedAt",
            "--limit", "10",
        ]) or []
    except subprocess.CalledProcessError:
        return []

    # Filter to unmerged (closed without merge), sort newest-first, cap at 3.
    unmerged = [p for p in prs if not p.get("mergedAt")]
    unmerged.sort(key=lambda p: p["createdAt"], reverse=True)
    unmerged = unmerged[:3]

    if not unmerged:
        return []

    results = []
    for pr in unmerged:
        pr_number = pr["number"]
        try:
            pr_data = _gh_json([
                "pr", "view", str(pr_number),
                "--repo", REPO,
                "--json", "comments",
            ]) or {}
        except subprocess.CalledProcessError:
            continue

        comments = pr_data.get("comments", [])

        merge_verdict = None
        review_summary = None
        for comment in reversed(comments):
            body = comment.get("body", "")
            if merge_verdict is None and "## Merge Verdict" in body:
                truncated = body[:500]
                if len(body) > 500:
                    truncated += "…"
                merge_verdict = truncated
            if review_summary is None and "### Finding:" in body:
                truncated = body[:300]
                if len(body) > 300:
                    truncated += "…"
                review_summary = truncated

        if merge_verdict is not None:
            results.append({
                "pr_number": pr_number,
                "title": pr["title"],
                "merge_verdict": merge_verdict,
                "review_summary": review_summary,
            })

    return results


def _build_attempt_history_block(attempts: list[dict]) -> str:
    """Format previous fix attempts as a markdown section.

    Returns empty string when attempts is empty so callers can
    unconditionally append without adding spurious content.
    """
    if not attempts:
        return ""
    block = "\n## Previous Fix Attempts\n\n"
    for attempt in attempts:
        block += f"### PR #{attempt['pr_number']}: {attempt['title']}\n\n"
        block += f"**Merge verdict:**\n{attempt['merge_verdict']}\n\n"
        if attempt.get("review_summary"):
            block += f"**Review summary:**\n{attempt['review_summary']}\n\n"
    return block


def _run_plan_agent(issue: dict, plan_index: int, work_dir: Path, attempt_history_block: str = "", first_plan: str = "") -> str:
    """Run a single cai-plan agent and return its stdout.

    Called serially (2×) by _run_plan_select_pipeline — the second call
    receives the first plan to produce an alternative approach.

    Runs with `cwd=/app` and `--add-dir <work_dir>` so the agent
    reads its definition from the canonical location while
    operating on the clone via absolute paths (#342).

    Each invocation is capped at $1.00 via --max-budget-usd to
    prevent runaway exploration sessions (typical run ~$0.60).
    """
    user_message = (
        _work_directory_block(work_dir)
        + "\n"
        + _build_issue_block(issue)
        + attempt_history_block
    )
    if first_plan:
        user_message += (
            "\n## First Plan (for reference)\n\n"
            "Another planning agent produced the following plan. "
            "Your job is to find an **alternative approach** that solves "
            "the same issue differently. Do NOT repeat the same strategy — "
            "propose a meaningfully different solution.\n\n"
            f"{first_plan}\n"
        )
    result = _run_claude_p(
        ["claude", "-p", "--agent", "cai-plan",
         "--dangerously-skip-permissions",
         "--max-budget-usd", "1.00",
         "--add-dir", str(work_dir)],
        category="fix.plan",
        agent="cai-plan",
        input=user_message,
        cwd="/app",
    )
    if result.returncode != 0:
        return f"(Plan {plan_index} failed: exit {result.returncode})"
    return result.stdout or ""


def _run_select_agent(issue: dict, plans: list[str], work_dir: Path) -> str:
    """Run the cai-select agent to choose the best plan.

    Returns the full stdout from the select agent.

    Runs with `cwd=/app` and `--add-dir <work_dir>` so the agent
    reads its definition from the canonical location while
    operating on the clone via absolute paths (#342).
    """
    user_message = _work_directory_block(work_dir) + "\n"
    user_message += _build_issue_block(issue)
    user_message += "\n---\n\n# Candidate Plans\n\n"
    for i, plan in enumerate(plans, 1):
        user_message += f"## Plan {i}\n\n{plan}\n\n---\n\n"
    result = _run_claude_p(
        ["claude", "-p", "--agent", "cai-select",
         "--dangerously-skip-permissions",
         "--add-dir", str(work_dir)],
        category="fix.select",
        agent="cai-select",
        input=user_message,
        cwd="/app",
    )
    if result.returncode != 0:
        return ""
    return result.stdout or ""


def _run_plan_select_pipeline(issue: dict, work_dir: Path, attempt_history_block: str = "") -> str | None:
    """Run the serial 2-plan → select pipeline and return the selected plan.

    Plan 1 runs first; Plan 2 receives Plan 1's output and is asked
    to find an alternative approach. The select agent then picks the best.

    Returns the selected plan text to prepend to the fix agent's
    user message, or None if the pipeline fails.
    """
    issue_number = issue["number"]

    # Step 1: Run Plan 1.
    print(f"[cai fix] running plan agent 1/2 for #{issue_number}", flush=True)
    plan1 = _run_plan_agent(issue, 1, work_dir, attempt_history_block)
    print(f"[cai fix] plan 1: {len(plan1)} chars", flush=True)

    # Step 2: Run Plan 2 with knowledge of Plan 1, asking for an alternative.
    print(f"[cai fix] running plan agent 2/2 for #{issue_number}", flush=True)
    plan2 = _run_plan_agent(issue, 2, work_dir, attempt_history_block, first_plan=plan1)
    print(f"[cai fix] plan 2: {len(plan2)} chars", flush=True)

    plans = [plan1, plan2]

    # Step 3: Run the select agent to pick the best plan.
    print(f"[cai fix] running select agent for #{issue_number}", flush=True)
    selection = _run_select_agent(issue, plans, work_dir)
    if not selection.strip():
        print("[cai fix] select agent produced no output; skipping pipeline", flush=True)
        return None

    print(f"[cai fix] select agent produced {len(selection)} chars", flush=True)
    return selection


def _extract_stored_plan(issue_body: str) -> str | None:
    """Extract the stored plan from an issue body, or None if not present."""
    start_marker = "<!-- cai-plan-start -->"
    end_marker = "<!-- cai-plan-end -->"
    start = issue_body.find(start_marker)
    end = issue_body.find(end_marker)
    if start == -1 or end == -1 or end <= start:
        return None
    content = issue_body[start + len(start_marker):end].strip()
    heading = "## Selected Implementation Plan"
    if content.startswith(heading):
        content = content[len(heading):].strip()
    return content if content else None


def _strip_stored_plan_block(issue_body: str) -> str:
    """Remove an existing cai-plan block from the issue body, if present."""
    start_marker = "<!-- cai-plan-start -->"
    end_marker = "<!-- cai-plan-end -->"
    start = issue_body.find(start_marker)
    end = issue_body.find(end_marker)
    if start == -1 or end == -1 or end <= start:
        return issue_body
    # Remove from start_marker through end_marker plus any trailing newlines.
    after = end + len(end_marker)
    while after < len(issue_body) and issue_body[after] == "\n":
        after += 1
    return issue_body[:start] + issue_body[after:]


def cmd_plan(args) -> int:
    """Run the plan-select pipeline on one :refined issue and store the result."""
    t0 = time.monotonic()

    # 1. Select target issue.
    issue = _select_plan_target(getattr(args, "issue", None))
    if issue is None:
        print("[cai plan] no eligible :refined issues; nothing to do", flush=True)
        log_run("plan", repo=REPO, result="no_eligible_issues", exit=0)
        return 0

    issue_number = issue["number"]
    title = issue["title"]
    print(f"[cai plan] picked #{issue_number}: {title}", flush=True)

    # 2. Clone repo (plan agents need to read the codebase).
    _uid = uuid.uuid4().hex[:8]
    work_dir = Path(f"/tmp/cai-plan-{issue_number}-{_uid}")
    try:
        if work_dir.exists():
            shutil.rmtree(work_dir)
        clone = _run(
            ["git", "clone", "--depth", "1",
             f"https://github.com/{REPO}.git", str(work_dir)],
            capture_output=True,
        )
        if clone.returncode != 0:
            print(f"[cai plan] git clone failed:\n{clone.stderr}",
                  file=sys.stderr)
            log_run("plan", repo=REPO, issue=issue_number,
                    result="clone_failed", exit=1)
            return 1

        # 3. Fetch previous fix attempts for context.
        attempts = _fetch_previous_fix_attempts(issue_number)
        attempt_history_block = _build_attempt_history_block(attempts)
        if attempt_history_block:
            print(
                f"[cai plan] injecting {len(attempts)} previous fix "
                f"attempt(s) for #{issue_number}",
                flush=True,
            )

        # 4. Run plan-select pipeline.
        selected_plan = _run_plan_select_pipeline(
            issue, work_dir, attempt_history_block,
        )
        if selected_plan is None:
            print(f"[cai plan] plan pipeline failed for #{issue_number}",
                  file=sys.stderr)
            dur = f"{int(time.monotonic() - t0)}s"
            log_run("plan", repo=REPO, issue=issue_number,
                    duration=dur, result="pipeline_failed", exit=1)
            return 1

        # 5. Store plan in issue body (strip any old plan block first).
        current_body = _strip_stored_plan_block(issue.get("body", "") or "")
        plan_block = (
            "<!-- cai-plan-start -->\n"
            "## Selected Implementation Plan\n\n"
            f"{selected_plan}\n"
            "<!-- cai-plan-end -->"
        )
        new_body = f"{plan_block}\n\n{current_body}"
        update = _run(
            ["gh", "issue", "edit", str(issue_number),
             "--repo", REPO, "--body", new_body],
            capture_output=True,
        )
        if update.returncode != 0:
            print(f"[cai plan] gh issue edit failed:\n{update.stderr}",
                  file=sys.stderr)
            dur = f"{int(time.monotonic() - t0)}s"
            log_run("plan", repo=REPO, issue=issue_number,
                    duration=dur, result="edit_failed", exit=1)
            return 1

        # 6. Transition labels: :refined → :planned.
        _set_labels(
            issue_number,
            add=[LABEL_PLANNED],
            remove=[LABEL_REFINED],
            log_prefix="cai plan",
        )

        dur = f"{int(time.monotonic() - t0)}s"
        print(
            f"[cai plan] #{issue_number} planned and transitioned to "
            f":planned in {dur}",
            flush=True,
        )
        log_run("plan", repo=REPO, issue=issue_number,
                duration=dur, result="ok", exit=0)
        return 0

    finally:
        if work_dir.exists():
            shutil.rmtree(work_dir, ignore_errors=True)


def _parse_suggested_issues(agent_output: str) -> list[dict]:
    """Extract suggested issues from the subagent's stdout.

    The subagent can emit blocks like:

        ## Suggested Issue
        ### Title
        <title text>
        ### Body
        <body text>

    Returns a list of dicts with 'title' and 'body' keys.
    """
    issues: list[dict] = []
    parts = re.split(r"^## Suggested Issue\s*$", agent_output, flags=re.MULTILINE)
    for part in parts[1:]:  # skip everything before the first marker
        title = ""
        body = ""
        title_match = re.search(
            r"^### Title\s*\n(.*?)(?=^### |\Z)",
            part,
            flags=re.MULTILINE | re.DOTALL,
        )
        body_match = re.search(
            r"^### Body\s*\n(.*?)(?=^## |\Z)",
            part,
            flags=re.MULTILINE | re.DOTALL,
        )
        if title_match:
            title = title_match.group(1).strip()
        if body_match:
            body = body_match.group(1).strip()
        if title:
            issues.append({"title": title, "body": body})
    return issues


def _create_suggested_issues(
    suggested: list[dict], source_issue_number: int,
) -> int:
    """Create GitHub issues raised by the fix subagent. Returns count created."""
    created = 0
    for s in suggested:
        issue_body = (
            f"{s['body']}\n\n"
            f"---\n"
            f"_Raised by the fix subagent while working on "
            f"#{source_issue_number}._\n"
        )
        labels = ",".join(["auto-improve", LABEL_RAISED])
        result = _run(
            [
                "gh", "issue", "create",
                "--repo", REPO,
                "--title", s["title"],
                "--body", issue_body,
                "--label", labels,
            ],
            capture_output=True,
        )
        if result.returncode == 0:
            url = result.stdout.strip()
            print(f"[cai fix] created suggested issue: {url}", flush=True)
            created += 1
        else:
            print(
                f"[cai fix] failed to create suggested issue "
                f"'{s['title']}': {result.stderr}",
                file=sys.stderr,
            )
    return created


# ---------------------------------------------------------------------------
# Multi-step issue helpers
# ---------------------------------------------------------------------------


def _parse_decomposition(agent_output: str) -> list[dict]:
    """Extract ordered steps from a ``## Multi-Step Decomposition`` block.

    Expected format in *agent_output*::

        ## Multi-Step Decomposition

        ### Step 1: <title>
        <body>

        ### Step 2: <title>
        <body>

    Returns a list of ``{"step": int, "title": str, "body": str}`` dicts,
    sorted by step number.  Returns an empty list when the marker is
    missing or the output is malformed.
    """
    marker = "## Multi-Step Decomposition"
    marker_pos = agent_output.find(marker)
    if marker_pos == -1:
        return []

    text = agent_output[marker_pos + len(marker):]
    parts = re.split(r"^### Step (\d+):\s*", text, flags=re.MULTILINE)
    # parts[0] is preamble (before first step), then alternating
    # (step_number, body) pairs.
    steps: list[dict] = []
    i = 1
    while i + 1 < len(parts):
        step_num = int(parts[i])
        raw = parts[i + 1].strip()
        # The title is the first non-empty line; the rest is the body.
        lines = raw.split("\n", 1)
        title = lines[0].strip()
        body = lines[1].strip() if len(lines) > 1 else ""
        if title:
            steps.append({"step": step_num, "title": title, "body": body})
        i += 2

    steps.sort(key=lambda s: s["step"])
    return steps


def _find_sub_issue(parent_number: int, step: int) -> int | None:
    """Return the issue number of an existing sub-issue for *parent_number*
    / *step* (open or closed), or None if none exists.

    Matches sub-issues via the HTML-comment markers embedded in their
    body by ``_create_sub_issues``. Used to make refine idempotent.
    """
    search_query = (
        f'"<!-- parent: #{parent_number} -->" '
        f'"<!-- step: {step} -->" in:body'
    )
    try:
        issues = _gh_json([
            "issue", "list",
            "--repo", REPO,
            "--search", search_query,
            "--state", "all",
            "--json", "number",
            "--limit", "5",
        ]) or []
    except subprocess.CalledProcessError:
        return None
    if not issues:
        return None
    # Return the lowest (earliest-created) matching number for stability.
    return min(int(i["number"]) for i in issues)


def _create_sub_issues(
    steps: list[dict], parent_number: int, parent_title: str,
) -> list[int]:
    """Create GitHub sub-issues for a multi-step decomposition.

    Each sub-issue gets HTML-comment markers for parent and step number,
    enabling the ordering gate in ``_select_fix_target``.

    Returns list of created issue numbers (may be shorter than *steps*
    if some creations fail).
    """
    total = len(steps)
    created: list[int] = []
    for s in steps:
        # Guard against duplicate creation: if a sub-issue for this
        # parent+step already exists (open or closed), reuse it instead
        # of spawning another. This makes refine idempotent across
        # re-runs (e.g. after rollback from :no-action to :raised).
        existing = _find_sub_issue(parent_number, s["step"])
        if existing is not None:
            print(
                f"[cai refine] sub-issue for parent #{parent_number} "
                f"step {s['step']} already exists as #{existing}; "
                f"skipping creation",
                flush=True,
            )
            created.append(existing)
            continue
        body = (
            f"<!-- parent: #{parent_number} -->\n"
            f"<!-- step: {s['step']} -->\n\n"
            f"{s['body']}\n\n"
            f"---\n"
            f"_Sub-issue of #{parent_number} ({parent_title}). "
            f"Step {s['step']} of {total}._\n"
        )
        title = f"[Step {s['step']}/{total}] {s['title']}"
        labels = ",".join(["auto-improve", LABEL_RAISED])
        result = _run(
            [
                "gh", "issue", "create",
                "--repo", REPO,
                "--title", title,
                "--body", body,
                "--label", labels,
            ],
            capture_output=True,
        )
        if result.returncode == 0:
            url = result.stdout.strip()
            # Extract issue number from URL (last path segment).
            try:
                num = int(url.rstrip("/").rsplit("/", 1)[-1])
            except (ValueError, IndexError):
                num = 0
            if num:
                created.append(num)
            print(f"[cai refine] created sub-issue: {url}", flush=True)
        else:
            print(
                f"[cai refine] failed to create sub-issue "
                f"'Step {s['step']}': {result.stderr}",
                file=sys.stderr,
            )
    return created


def _update_parent_checklist(
    parent_number: int,
    sub_issue_numbers: list[int],
    steps: list[dict],
) -> bool:
    """Append a ``## Sub-issues`` checklist to the parent issue body.

    Returns True on success.
    """
    try:
        parent = _gh_json([
            "issue", "view", str(parent_number),
            "--repo", REPO,
            "--json", "body",
        ])
    except subprocess.CalledProcessError:
        return False

    original_body = (parent or {}).get("body") or ""

    # Strip any pre-existing ``## Sub-issues`` section(s) so re-running
    # refine on the same parent (e.g. after rollback from :no-action)
    # replaces the checklist rather than appending a duplicate.
    stripped_body = re.sub(
        r"\n*## Sub-issues\n.*?(?=\n## |\Z)",
        "",
        original_body,
        flags=re.DOTALL,
    ).rstrip()

    # Build checklist lines.
    checklist_lines = []
    for s, num in zip(steps, sub_issue_numbers):
        checklist_lines.append(f"- [ ] #{num} — Step {s['step']}: {s['title']}")
    checklist = "\n".join(checklist_lines)

    new_body = f"{stripped_body}\n\n## Sub-issues\n\n{checklist}\n"

    result = _run(
        ["gh", "issue", "edit", str(parent_number),
         "--repo", REPO, "--body", new_body],
        capture_output=True,
    )
    if result.returncode != 0:
        print(
            f"[cai refine] failed to update parent #{parent_number} checklist: "
            f"{result.stderr}",
            file=sys.stderr,
        )
        return False
    return True


def _issue_is_closed(issue_number: int) -> bool:
    """Return True if the issue is in CLOSED state."""
    try:
        issue = _gh_json([
            "issue", "view", str(issue_number),
            "--repo", REPO,
            "--json", "state",
        ])
    except subprocess.CalledProcessError:
        return False
    return (issue or {}).get("state", "").upper() == "CLOSED"


def _check_parent_step_done(parent_number: int, step: int) -> bool:
    """Return True if the sub-issue for *step* of *parent_number* is closed.

    Searches open+closed issues for the parent/step markers.
    """
    search_query = f'"<!-- parent: #{parent_number} -->" "<!-- step: {step} -->" in:body'
    try:
        issues = _gh_json([
            "issue", "list",
            "--repo", REPO,
            "--search", search_query,
            "--state", "all",
            "--json", "number,state",
            "--limit", "5",
        ]) or []
    except subprocess.CalledProcessError:
        return False
    # Any closed match means the step is done.
    return any(
        (i.get("state") or "").upper() == "CLOSED"
        for i in issues
    )


def _update_parent_checklist_item(
    parent_number: int, sub_issue_number: int, *, checked: bool,
) -> bool:
    """Toggle a single checkbox in the parent's ``## Sub-issues`` checklist.

    Returns True on success.
    """
    try:
        parent = _gh_json([
            "issue", "view", str(parent_number),
            "--repo", REPO,
            "--json", "body",
        ])
    except subprocess.CalledProcessError:
        return False

    body = (parent or {}).get("body") or ""
    old = f"- [ ] #{sub_issue_number}" if checked else f"- [x] #{sub_issue_number}"
    new = f"- [x] #{sub_issue_number}" if checked else f"- [ ] #{sub_issue_number}"
    if old not in body:
        return False  # nothing to update

    new_body = body.replace(old, new, 1)
    result = _run(
        ["gh", "issue", "edit", str(parent_number),
         "--repo", REPO, "--body", new_body],
        capture_output=True,
    )
    return result.returncode == 0


# ---------------------------------------------------------------------------
# Wrapper-side `.claude/agents/*.md` and `.claude/plugins/` writes
# (staging-directory pattern)
# ---------------------------------------------------------------------------
#
# Background: claude-code's headless `claude -p` mode hardcodes a
# self-modification protection on `.claude/agents/*.md` files. The
# protection fires regardless of `--dangerously-skip-permissions`,
# `--permission-mode`, `--add-dir`, `--agent <name>`, OR
# `permissions.allow` rules in `.claude/settings.json` — verified
# empirically with three reproduction tests on the host. The docs
# describe an exemption for `bypassPermissions` mode but that
# exemption applies only to interactive Claude Code sessions, not
# headless `-p` sessions.
#
# This means: a sub-agent invoked via `claude -p --agent cai-fix`
# CANNOT use its Edit/Write tools to modify any `.claude/agents/*.md`
# file, even the one for a different agent. The block is structural
# and unbypassable from inside the session.
#
# Workaround: a "staging directory" pattern.
#
#   1. Before invoking the agent, the wrapper creates empty dirs
#      at `<work_dir>/.cai-staging/agents/` and
#      `<work_dir>/.cai-staging/plugins/`. These paths are NOT under
#      `.claude/`, so claude-code's protection doesn't fire on
#      writes to them.
#
#   2a. The agent's prompt instructs it: when you need to update an
#       `.claude/agents/<name>.md` file, do not Edit the protected
#       path directly. Instead, use the Write tool to write the FULL
#       new content to `<work_dir>/.cai-staging/agents/<name>.md`.
#
#   2b. To create or update plugin files under `.claude/plugins/`,
#       write to `<work_dir>/.cai-staging/plugins/<plugin-path>`
#       preserving the same relative directory structure. For example,
#       to create `.claude/plugins/cai-skills/skills/foo/SKILL.md`,
#       write to `.cai-staging/plugins/cai-skills/skills/foo/SKILL.md`.
#
#   3. After the agent exits successfully, the wrapper:
#      - For each `<name>.md` in `.cai-staging/agents/`: copies it to
#        `<work_dir>/.claude/agents/<name>.md` via `pathlib.write_text`.
#      - For the tree at `.cai-staging/plugins/`: merges it into
#        `<work_dir>/.claude/plugins/` using `shutil.copytree` with
#        `dirs_exist_ok=True`.
#      (The wrapper isn't a claude session and isn't subject to the
#      protection.)
#
#   4. The wrapper removes the staging directory before committing
#      so it doesn't land in the PR. If plugin staging fails, the
#      staging directory is preserved for inspection rather than
#      silently deleted.
#
# Full-file writes (not Edit-style old/new diffs) by design: the
# agent writes the whole replacement content; the wrapper does an
# unconditional write. Simpler than parsing structured blocks, no
# whitespace-ambiguity edge cases, no context-uniqueness rules.
# The trade-off is verbosity — the agent has to emit the entire
# file content — but agent definition files are small (a few
# hundred lines max).


# Paths of the staging directories inside a cloned worktree, relative
# to the clone root.
AGENT_EDIT_STAGING_REL = Path(".cai-staging") / "agents"
PLUGIN_STAGING_REL = Path(".cai-staging") / "plugins"


def _setup_agent_edit_staging(work_dir: Path) -> Path:
    """Create the staging directories where agents write proposed
    `.claude/agents/*.md` and `.claude/plugins/` updates. Idempotent.

    Returns the absolute agent-staging directory path so the caller can
    pass it to the agent via the user message.
    """
    staging = work_dir / AGENT_EDIT_STAGING_REL
    staging.mkdir(parents=True, exist_ok=True)
    plugin_staging = work_dir / PLUGIN_STAGING_REL
    plugin_staging.mkdir(parents=True, exist_ok=True)
    return staging


def _apply_agent_edit_staging(work_dir: Path) -> int:
    """Copy any files staged at `<work_dir>/.cai-staging/agents/`
    back to `<work_dir>/.claude/agents/`, copy any plugin tree staged
    at `<work_dir>/.cai-staging/plugins/` to `<work_dir>/.claude/plugins/`,
    then remove the staging directory so it doesn't land in the PR.

    Security boundaries:

      1. Each staged agent file is copied to `<work_dir>/.claude/agents/`
         using the same basename. If no target exists a new file is
         created; if one exists it is overwritten.
      2. Staged plugin trees are merged into `<work_dir>/.claude/plugins/`
         using shutil.copytree with dirs_exist_ok=True.
      3. The staging dir lives entirely inside `work_dir` so escapes
         via `..` are not possible (the wrapper iterates one
         directory level via `iterdir()` and copies whole files).
      4. The staging dir is removed before commit if all staging
         operations succeeded. If plugin staging fails, the staging
         dir is preserved for inspection and the function returns
         early so staged content is not silently lost.

    Returns the count of files successfully applied. If the staging
    dir doesn't exist or is empty, returns 0 with no side effects.
    """
    staging = work_dir / AGENT_EDIT_STAGING_REL
    applied = 0

    if staging.exists() and staging.is_dir():
        target_dir = work_dir / ".claude" / "agents"
        for staged_file in sorted(staging.iterdir()):
            if not staged_file.is_file():
                continue

            target = target_dir / staged_file.name
            if not target.exists():
                print(
                    f"[cai] agent edit staging: creating new agent file "
                    f".claude/agents/{staged_file.name}",
                    flush=True,
                )

            try:
                content = staged_file.read_text()
                target.write_text(content)
                print(
                    f"[cai] applied staged agent file: "
                    f".claude/agents/{staged_file.name} "
                    f"({len(content)} bytes)",
                    flush=True,
                )
                applied += 1
            except OSError as exc:
                print(
                    f"[cai] agent edit staging: failed to apply "
                    f"{staged_file.name}: {exc}",
                    file=sys.stderr,
                )
                continue

    # Apply any plugin staging: .cai-staging/plugins/ → .claude/plugins/
    plugin_staging = work_dir / PLUGIN_STAGING_REL
    if plugin_staging.exists() and plugin_staging.is_dir():
        plugin_target = work_dir / ".claude" / "plugins"
        try:
            shutil.copytree(str(plugin_staging), str(plugin_target),
                            dirs_exist_ok=True)
            print(
                f"[cai] applied staged plugin tree: .claude/plugins/ "
                f"(merged from .cai-staging/plugins/)",
                flush=True,
            )
            applied += 1
        except OSError as exc:
            print(
                f"[cai] agent edit staging: failed to apply plugin tree: {exc}",
                file=sys.stderr,
            )
            # Preserve .cai-staging so staged plugin files are not
            # silently lost when the copy fails — caller can inspect
            # or retry. Do not fall through to shutil.rmtree below.
            return applied

    # Clean up the entire .cai-staging tree (one level above the
    # agents/ subdir) so nothing leaks into the PR.
    cai_staging_root = work_dir / ".cai-staging"
    if cai_staging_root.exists():
        try:
            shutil.rmtree(cai_staging_root)
        except OSError as exc:
            print(
                f"[cai] agent edit staging: cleanup of "
                f"{cai_staging_root} failed: {exc}",
                file=sys.stderr,
            )

    return applied


def _git(work_dir: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    cmd = ["git", "-C", str(work_dir)] + list(args)
    return subprocess.run(cmd, text=True, check=check, capture_output=True)


def _work_directory_block(work_dir: Path) -> str:
    """Return the standard "## Work directory" user-message section
    that informs a cloned-worktree subagent where its actual work
    happens, and how to update protected `.claude/agents/*.md`
    files via the staging directory.

    All cloned-worktree subagents (cai-fix, cai-revise, cai-rebase,
    cai-review-pr, cai-review-docs, cai-code-audit, cai-propose, cai-propose-review,
    cai-update-check, cai-plan, cai-select, cai-git) are invoked with `cwd=/app`
    rather than `cwd=<clone>`. This makes their canonical agent
    definition (`/app/.claude/agents/<name>.md`) and per-agent memory
    (`/app/.claude/agent-memory/<name>/`) directly available via
    cwd-relative paths.

    The trade-off: the agent must use ABSOLUTE paths to read/edit
    files in the actual clone, since the clone is no longer the
    cwd. This block tells the agent where the clone is and reminds
    it to use absolute paths.

    Self-modification of `.claude/agents/*.md`: claude-code's
    headless `-p` mode hardcodes a protection that blocks
    Edit/Write on any `.claude/agents/*.md` file, regardless of
    `--dangerously-skip-permissions`, `--permission-mode`, or
    `permissions.allow` rules. We work around it with a staging
    directory — see `_setup_agent_edit_staging` /
    `_apply_agent_edit_staging`. The block below tells the agent
    how to use it.
    """
    staging_rel = AGENT_EDIT_STAGING_REL.as_posix()
    staging_abs = (work_dir / AGENT_EDIT_STAGING_REL).as_posix()
    return (
        "## Work directory\n\n"
        "You are running with cwd `/app` so your declarative agent "
        "definition and per-agent memory are read from the canonical "
        "image / volume locations. Your actual work happens on a "
        "fresh clone of the repository at:\n\n"
        f"    {work_dir}\n\n"
        "**You MUST use absolute paths under that directory for all "
        "Read/Edit/Write/Glob/Grep calls that target the work.** "
        "Relative paths resolve to `/app` (the canonical, baked-in "
        "version of the repo) which you should treat as read-only. "
        "Edits to `/app/...` would land in the container's writable "
        "layer and be lost on next restart — they would NOT make it "
        "into git.\n\n"
        "Examples:\n"
        f"  - GOOD: `Read(\"{work_dir}/cai.py\")`\n"
        "  - BAD:  `Read(\"cai.py\")`               (reads /app/cai.py)\n"
        f"  - GOOD: `Edit(\"{work_dir}/parse.py\", ...)`\n"
        "  - BAD:  `Edit(\"parse.py\", ...)`        (edits /app/parse.py)\n\n"
        "If you have Bash in your tool allowlist, the same rule "
        "applies: use `git -C` (or absolute paths) for any git "
        "operation that should target the clone, NOT the cwd.\n\n"
        "  - GOOD: `git -C "
        f"{work_dir} status`\n"
        "  - BAD:  `git status`        (reports /app status, not "
        "the clone's)\n\n"
        "## Updating `.claude/agents/*.md` (self-modification)\n\n"
        "Claude-code's headless `-p` mode hardcodes a write block "
        "on every `.claude/agents/*.md` path, regardless of any "
        "permission flag or settings rule. Edit/Write calls against "
        f"`{work_dir}/.claude/agents/<name>.md` WILL fail with a "
        "sensitive-file protection error — this is not something "
        "you can bypass from inside your session.\n\n"
        "The wrapper provides a **staging directory** at:\n\n"
        f"    {staging_abs}\n\n"
        "To update an `.claude/agents/<name>.md` file, use the "
        "Write tool to write the COMPLETE new file content "
        "(frontmatter + body) to "
        f"`{staging_abs}/<name>.md`. After your session exits "
        "successfully the wrapper copies every file it finds in "
        f"`{staging_rel}/` back over the corresponding "
        "`.claude/agents/<same-name>.md` in the clone, then deletes "
        "the staging directory so it never lands in the PR.\n\n"
        "Rules:\n"
        "  - Staged files are copied unconditionally — new agent "
        "definitions are created if no target exists yet.\n"
        "  - Write the FULL file, not a diff or patch. The wrapper "
        "does an unconditional full-file overwrite.\n"
        "  - Use the same basename as the target "
        f"(e.g. `{staging_abs}/cai-fix.md` → "
        f"`{work_dir}/.claude/agents/cai-fix.md`).\n"
        "  - Do NOT attempt `Edit` or `Write` on the protected "
        f"`{work_dir}/.claude/agents/...` path — it will always "
        "fail. Go through the staging dir.\n\n"
        "Example:\n"
        f"  - GOOD: `Write(\"{staging_abs}/cai-fix.md\", "
        "\"<full new file content>\")`\n"
        f"  - BAD:  `Edit(\"{work_dir}/.claude/agents/cai-fix.md\", "
        "...)`  (blocked by claude-code)\n"
    )


def _pre_screen_issue_actionability(issue: dict) -> tuple[str, str]:
    """Cheap Haiku pre-screen to classify an issue before running plan-select.

    Returns a tuple of (verdict, reason) where verdict is one of:
      "actionable" — proceed to clone + plan-select pipeline
      "spike"      — issue needs research before code can be written
      "ambiguous"  — issue is too vague to identify any concrete change

    On any error (network failure, JSON parse error, unexpected format),
    falls through to ("actionable", "pre-screen error: <msg>") so the
    pipeline is never blocked.
    """
    try:
        title = issue.get("title", "")
        body = issue.get("body", "") or ""
        labels = ", ".join(lbl["name"] for lbl in issue.get("labels", []))

        prompt_text = (
            "You are a triage classifier for GitHub issues in an automated "
            "code-improvement pipeline.\n\n"
            "Given the issue below, classify it as one of:\n"
            '- "actionable": A concrete code change can be identified. '
            "This is the DEFAULT — use it when in doubt.\n"
            '- "spike": The issue clearly requires research, investigation, '
            "or evaluation before any code can be written. No specific file "
            "or function is identifiable.\n"
            '- "ambiguous": The issue is so vague that no file, function, '
            "or specific change can be identified, AND it does not describe "
            "a research task.\n\n"
            "IMPORTANT: Strongly bias toward \"actionable\". Only use "
            '"spike" or "ambiguous" when you are highly confident. If the '
            "issue mentions any specific file, function, code pattern, or "
            'error, classify as "actionable".\n\n'
            "Respond with ONLY a JSON object (no markdown fencing):\n"
            '{"verdict": "actionable"|"spike"|"ambiguous", '
            '"reason": "<one sentence explanation>"}\n\n'
            "## Issue\n"
            f"**Title:** {title}\n"
            f"**Labels:** {labels}\n\n"
            f"{body}"
        )

        # NOTE: intentional deviation from the --agent convention used by other
        # _run_claude_p call sites.  The pre-screen is a pure inline prompt with
        # no tool access and no agent definition file — using --model directly
        # avoids the overhead of an agent file for a call that never needs one.
        proc = _run_claude_p(
            ["claude", "-p", "--model", "claude-haiku-4-5", prompt_text],
            category="fix.pre-screen",
        )

        raw = (proc.stdout or "").strip()
        # Strip markdown code fences if the model ignored the instruction
        if raw.startswith("```"):
            raw = raw.removeprefix("```json").removeprefix("```")
            raw = raw.removesuffix("```").strip()

        parsed = json.loads(raw)
        verdict = parsed.get("verdict", "actionable")
        reason = parsed.get("reason", "")
        if verdict not in ("actionable", "spike", "ambiguous"):
            return ("actionable", f"unexpected verdict {verdict!r}")
        return (verdict, reason)

    except Exception as e:  # noqa: BLE001
        print(
            f"[cai fix] pre-screen error (falling through to actionable): {e}",
            file=sys.stderr,
            flush=True,
        )
        return ("actionable", f"pre-screen error: {e}")


def cmd_fix(args) -> int:
    """Run the fix subagent against one eligible issue."""
    if getattr(args, "issue", None) is not None:
        try:
            issue = _gh_json([
                "issue", "view", str(args.issue),
                "--repo", REPO,
                "--json", "number,title,body,labels,state,createdAt,comments",
            ])
        except subprocess.CalledProcessError as e:
            print(f"[cai fix] gh issue view #{args.issue} failed:\n{e.stderr}", file=sys.stderr)
            log_run("fix", repo=REPO, issue=args.issue, result="issue_lookup_failed", exit=1)
            return 1
        if issue.get("state", "").upper() != "OPEN":
            print(f"[cai fix] issue #{args.issue} is not open; nothing to do", flush=True)
            log_run("fix", repo=REPO, issue=args.issue, result="not_open", exit=0)
            return 0
        label_names = {lbl["name"] for lbl in issue.get("labels", [])}
        if LABEL_IN_PROGRESS in label_names or LABEL_PR_OPEN in label_names:
            print(
                f"[cai fix] issue #{args.issue} is already locked "
                f"({LABEL_IN_PROGRESS} or {LABEL_PR_OPEN} present); skipping",
                flush=True,
            )
            log_run("fix", repo=REPO, issue=args.issue, result="already_locked", exit=0)
            return 0
    else:
        issue = _select_fix_target()
        if issue is None:
            print("[cai fix] no eligible issues; nothing to do", flush=True)
            log_run("fix", repo=REPO, result="no_eligible_issues", exit=0)
            return 0

    issue_number = issue["number"]
    title = issue["title"]
    label_names = {lbl["name"] for lbl in issue.get("labels", [])}
    origin_raised_label = LABEL_REQUESTED if LABEL_REQUESTED in label_names else LABEL_REFINED
    print(f"[cai fix] picked #{issue_number}: {title}", flush=True)

    # 1. Lock — set :in-progress, drop :refined and :requested.
    if not _set_labels(
        issue_number,
        add=[LABEL_IN_PROGRESS],
        remove=[LABEL_REFINED, LABEL_REQUESTED],
    ):
        print(f"[cai fix] could not lock #{issue_number}", file=sys.stderr)
        log_run("fix", repo=REPO, issue=issue_number, result="lock_failed", exit=1)
        return 1
    print(f"[cai fix] locked #{issue_number} (label {LABEL_IN_PROGRESS})", flush=True)
    _write_active_job("fix", "issue", issue_number)

    # Make sure git can authenticate over HTTPS via the gh token. This
    # is also done in entrypoint.sh, but redoing it here is cheap and
    # idempotent and lets ad-hoc `docker run` invocations work too.
    _run(["gh", "auth", "setup-git"], capture_output=True)

    # Pre-screen: cheap Haiku call to triage obvious non-actionable issues
    # before the expensive clone + plan-select pipeline.
    ps_verdict, ps_reason = _pre_screen_issue_actionability(issue)
    print(f"[cai fix] pre-screen: verdict={ps_verdict} reason={ps_reason}", flush=True)

    if ps_verdict == "spike":
        _set_labels(
            issue_number,
            add=[LABEL_NEEDS_SPIKE],
            remove=[LABEL_IN_PROGRESS],
        )
        _run(
            ["gh", "issue", "comment", str(issue_number),
             "--repo", REPO,
             "--body",
             f"## Pre-screen: needs a spike\n\n"
             f"{ps_reason}\n\n---\n"
             f"_Flagged by `cai fix` pre-screen (Haiku). Re-label to "
             f"`{origin_raised_label}` to retry._"],
            capture_output=True,
        )
        log_run("fix", repo=REPO, issue=issue_number, result="pre_screen_spike", exit=0)
        _clear_active_job()
        return 0

    if ps_verdict == "ambiguous":
        _set_labels(
            issue_number,
            add=[origin_raised_label],
            remove=[LABEL_IN_PROGRESS],
        )
        _run(
            ["gh", "issue", "comment", str(issue_number),
             "--repo", REPO,
             "--body",
             f"## Pre-screen: ambiguous issue\n\n"
             f"{ps_reason}\n\n---\n"
             f"_Flagged by `cai fix` pre-screen (Haiku). The issue "
             f"was returned to `{origin_raised_label}` for refinement._"],
            capture_output=True,
        )
        log_run("fix", repo=REPO, issue=issue_number, result="pre_screen_ambiguous", exit=0)
        _clear_active_job()
        return 0

    _uid = uuid.uuid4().hex[:8]
    work_dir = Path(f"/tmp/cai-fix-{issue_number}-{_uid}")
    locked = True

    def rollback() -> None:
        nonlocal locked
        if not locked:
            return
        _set_labels(
            issue_number,
            add=[origin_raised_label],
            remove=[LABEL_IN_PROGRESS],
        )
        locked = False

    try:
        if work_dir.exists():
            shutil.rmtree(work_dir)

        # 2. Clone.
        clone = _run(
            ["git", "clone", "--depth", "1", f"https://github.com/{REPO}.git", str(work_dir)],
            capture_output=True,
        )
        if clone.returncode != 0:
            print(f"[cai fix] git clone failed:\n{clone.stderr}", file=sys.stderr)
            rollback()
            log_run("fix", repo=REPO, issue=issue_number, result="clone_failed", exit=1)
            return 1

        # 3. Configure git identity from the gh token's owner.
        name, email = _gh_user_identity()
        _git(work_dir, "config", "user.name", name)
        _git(work_dir, "config", "user.email", email)
        print(f"[cai fix] git identity: {name} <{email}>", flush=True)

        # 4. Branch.
        branch = f"auto-improve/{issue_number}-{_slugify(title)}"
        _git(work_dir, "checkout", "-b", branch)

        # 4b. Fetch previous fix attempts (closed, unmerged PRs) and
        #     build a history block so plan and fix agents don't repeat
        #     rejected approaches.
        attempts = _fetch_previous_fix_attempts(issue_number)
        attempt_history_block = _build_attempt_history_block(attempts)
        if attempt_history_block:
            print(
                f"[cai fix] injecting {len(attempts)} previous fix attempt(s) for #{issue_number}",
                flush=True,
            )

        # 4c. Run the plan-select pipeline: 2 plan agents in serial
        #     (each capped at $1.00), where the second sees the first's
        #     output and proposes an alternative. A select agent then
        #     picks the best plan. The selected plan is prepended
        #     to the fix agent's user message so it has a concrete
        #     implementation strategy to follow.
        selected_plan = _run_plan_select_pipeline(issue, work_dir, attempt_history_block)

        # 4d. Pre-create the `.cai-staging/agents/` directory so the
        #     agent has somewhere to write proposed updates to its
        #     own `.claude/agents/*.md` file(s). Claude-code's
        #     headless `-p` mode hardcodes a protection on every
        #     `.claude/agents/*.md` path that no permission flag
        #     bypasses, so we route self-modifications through a
        #     non-protected scratch path and copy them back after
        #     the agent exits successfully. See
        #     `_setup_agent_edit_staging` / `_apply_agent_edit_staging`.
        _setup_agent_edit_staging(work_dir)

        # 5. Run the cai-fix declarative subagent.
        #    System prompt, tool allowlist, and hard rules live in
        #    `.claude/agents/cai-fix.md`. The wrapper passes the
        #    work-directory block (telling the agent where its clone
        #    is, how to use absolute paths, and how to stage
        #    `.claude/agents/*.md` updates) plus the dynamic per-run
        #    context (the issue body) as the user message via stdin.
        #
        #    The agent runs with `cwd=/app`, NOT the clone. This
        #    lets it read its own definition and per-agent memory
        #    from `/app/.claude/agents/cai-fix.md` and
        #    `/app/.claude/agent-memory/cai-fix/MEMORY.md` directly
        #    from the image / persistent volume — no copy in/out
        #    needed. `--add-dir` grants the agent's tools access to
        #    the clone (which is outside cwd).
        user_message = (
            _work_directory_block(work_dir)
            + "\n"
            + _build_fix_user_message(issue, attempt_history_block)
        )
        if selected_plan:
            user_message = (
                _work_directory_block(work_dir)
                + "\n"
                + "## Selected Implementation Plan\n\n"
                + "The following plan was selected by the plan-select "
                + "pipeline from 2 serially generated candidates. "
                + "Follow this plan to implement the fix.\n\n"
                + f"{selected_plan}\n\n"
                + "---\n\n"
                + _build_fix_user_message(issue, attempt_history_block)
            )
        print(f"[cai fix] running cai-fix subagent for {work_dir}", flush=True)
        # `--dangerously-skip-permissions` is required for the
        # remaining permission-mode gating (cai-fix needs to edit
        # source files in the clone). Claude-code's hardcoded
        # protection on `.claude/agents/*.md` is NOT bypassed by
        # any flag — we route self-modifications through the
        # staging directory instead (see _work_directory_block).
        agent = _run_claude_p(
            ["claude", "-p", "--agent", "cai-fix",
             "--dangerously-skip-permissions",
             "--add-dir", str(work_dir)],
            category="fix",
            agent="cai-fix",
            input=user_message,
            cwd="/app",
        )
        if agent.stdout:
            print(agent.stdout, flush=True)
        if agent.returncode != 0:
            print(
                f"[cai fix] subagent claude -p failed (exit {agent.returncode}):\n"
                f"{agent.stderr}",
                file=sys.stderr,
            )
            rollback()
            log_run("fix", repo=REPO, issue=issue_number,
                    result="subagent_failed", exit=agent.returncode)
            return agent.returncode

        # 5b. Create any suggested issues the subagent raised.
        agent_text = agent.stdout or ""
        suggested = _parse_suggested_issues(agent_text)
        if suggested:
            n = _create_suggested_issues(suggested, issue_number)
            print(f"[cai fix] created {n}/{len(suggested)} suggested issue(s)", flush=True)

        # 5c. Apply any `.claude/agents/*.md` updates the agent
        #     staged at `<work_dir>/.cai-staging/agents/`. These
        #     exist because claude-code's headless `-p` mode
        #     hardcodes a self-modification block on every
        #     `.claude/agents/*.md` path that no flag bypasses, so
        #     the agent writes full new contents to the staging
        #     dir and the wrapper copies them back via plain
        #     pathlib (not subject to the protection). The staging
        #     dir is removed after apply so it doesn't land in
        #     the PR.
        applied = _apply_agent_edit_staging(work_dir)
        if applied:
            print(
                f"[cai fix] applied {applied} staged "
                f".claude/agents/*.md update(s)",
                flush=True,
            )

        # 6. Inspect the working tree. Empty diff = deliberate
        #    no-action OR a spike-shaped bail-out (the agent
        #    recognised the issue needs a spike, not a code change,
        #    per the bullet in cai-fix.md).
        status = _git(work_dir, "status", "--porcelain", check=False)
        if not status.stdout.strip():
            agent_text = agent.stdout or ""
            reasoning = agent_text.strip()[:2000]

            # Detect the spike marker. The cai-fix agent emits a
            # `## Needs Spike` block when bailing on a spike-shaped
            # issue so the wrapper can route to :needs-spike instead
            # of the default :no-action.
            is_spike = re.search(
                r"^##\s*Needs Spike\b",
                agent_text,
                flags=re.MULTILINE,
            ) is not None

            if is_spike:
                target_label = LABEL_NEEDS_SPIKE
                comment_heading = "## Fix subagent: needs a spike"
                comment_footer = (
                    "_Set by `cai fix` after the subagent recognised "
                    "this issue as spike-shaped (research / verification "
                    "/ evaluation). The cai-spike subagent (#314) will "
                    "pick this up once it ships. Re-label to "
                    f"`{origin_raised_label}` to retry as a routine "
                    "fix instead._"
                )
                log_result = "needs_spike"
                log_label = "auto-improve:needs-spike"
            else:
                target_label = LABEL_NO_ACTION
                comment_heading = "## Fix subagent: no action needed"
                comment_footer = (
                    "_Set by `cai fix` after the subagent reviewed and "
                    "decided no code change was needed. Re-label to "
                    f"`{origin_raised_label}` to retry, or close if "
                    "you agree._"
                )
                log_result = "no_action_needed"
                log_label = "auto-improve:no-action"

            print(
                f"[cai fix] subagent produced no changes for #{issue_number}; "
                f"marking {log_label}",
                flush=True,
            )
            # Post the agent's reasoning as a comment on the issue
            comment_body = (
                f"{comment_heading}\n\n"
                f"{reasoning}\n\n"
                f"---\n"
                f"{comment_footer}"
            )
            _run(
                ["gh", "issue", "comment", str(issue_number),
                 "--repo", REPO,
                 "--body", comment_body],
                capture_output=True,
            )
            # Transition: in-progress -> target_label (NOT back to :raised)
            if not _set_labels(
                issue_number,
                add=[target_label],
                remove=[LABEL_IN_PROGRESS],
            ):
                print(
                    f"[cai fix] WARNING: label transition to {log_label} "
                    f"failed for #{issue_number}; retrying",
                    flush=True,
                )
                if not _set_labels(
                    issue_number,
                    add=[target_label],
                    remove=[LABEL_IN_PROGRESS],
                ):
                    print(
                        f"[cai fix] WARNING: label transition to "
                        f"{log_label} failed twice for #{issue_number} "
                        "— issue may be stuck without a lifecycle label",
                        file=sys.stderr, flush=True,
                    )
                    rollback()
                    log_run("fix", repo=REPO, issue=issue_number,
                            result="label_transition_failed", exit=1)
                    return 1
            locked = False
            log_run("fix", repo=REPO, issue=issue_number,
                    result=log_result, exit=0)
            return 0

        # Count changed files for the log line.
        diff_files = len(status.stdout.strip().splitlines())

        # 7. Commit.
        _git(work_dir, "add", "-A")
        commit_msg = (
            f"auto-improve: {title}\n\n"
            f"Generated by `cai fix` against issue #{issue_number}.\n\n"
            f"Refs {REPO}#{issue_number}"
        )
        _git(work_dir, "commit", "-m", commit_msg)

        # 7b. Run regression tests against the clone's working tree before
        # pushing, so a test failure can be rolled back without leaving any
        # remote state (orphaned branch with no PR).
        test_result = _run(
            [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"],
            cwd=str(work_dir),
            capture_output=True,
        )
        if test_result.returncode != 0:
            print(
                f"[cai fix] regression tests failed — not opening PR\n"
                f"{test_result.stdout}\n{test_result.stderr}",
                file=sys.stderr,
            )
            rollback()
            log_run("fix", repo=REPO, issue=issue_number,
                    result="tests_failed", exit=1)
            return 1

        # 8. Push.
        push = _run(
            ["git", "-C", str(work_dir), "push", "-u", "origin", branch],
            capture_output=True,
        )
        if push.returncode != 0:
            print(f"[cai fix] git push failed:\n{push.stderr}", file=sys.stderr)
            rollback()
            log_run("fix", repo=REPO, issue=issue_number,
                    result="push_failed", exit=1)
            return 1

        # 9. Open the PR.
        agent_output = (agent.stdout or "").strip()
        # Extract the structured PR Summary block the subagent was asked to
        # produce.  Fall back to a truncated raw dump if the block is missing.
        pr_summary = ""
        _marker = "## PR Summary"
        if _marker in agent_output:
            pr_summary = agent_output[agent_output.index(_marker):]
            # Strip any suggested-issue blocks that appear after the PR Summary.
            pr_summary = re.split(
                r"^## Suggested Issue\s*$", pr_summary, flags=re.MULTILINE,
            )[0].rstrip()
            # Trim a trailing ``` / ~~~ fence if the agent wrapped it.
            for fence in ("```", "~~~"):
                if pr_summary.rstrip().endswith(fence):
                    pr_summary = pr_summary.rstrip()[: -len(fence)].rstrip()
        if not pr_summary.strip():
            pr_summary = (
                f"## PR Summary\n\n"
                f"{agent_output[:4000]}"
            )
        pr_body = (
            f"Refs {REPO}#{issue_number}\n\n"
            f"**Issue:** #{issue_number} — {title}\n\n"
            f"{pr_summary}\n\n"
            f"---\n"
            f"_Auto-generated by `cai fix`. The fix subagent runs autonomously "
            f"with full tool permissions — please review the diff carefully._\n"
        )
        pr = _run(
            [
                "gh", "pr", "create",
                "--repo", REPO,
                "--title", f"auto-improve: {title}",
                "--body", pr_body,
                "--head", branch,
                "--base", "main",
            ],
            capture_output=True,
        )
        if pr.returncode != 0:
            print(f"[cai fix] gh pr create failed:\n{pr.stderr}", file=sys.stderr)
            rollback()
            log_run("fix", repo=REPO, issue=issue_number,
                    result="pr_create_failed", exit=1)
            return 1

        pr_url = pr.stdout.strip()
        print(f"[cai fix] opened PR: {pr_url}", flush=True)

        # Extract PR number from the URL (last path segment).
        pr_number = pr_url.rstrip("/").rsplit("/", 1)[-1]

        # 10. Transition label :in-progress -> :pr-open.
        #     This is critical — if it fails, the issue becomes orphaned
        #     from its open PR.  Retry once before giving up.
        if not _set_labels(
            issue_number,
            add=[LABEL_PR_OPEN],
            remove=[LABEL_IN_PROGRESS],
        ):
            print(
                f"[cai fix] label transition to :pr-open failed for #{issue_number}; retrying",
                flush=True,
            )
            if not _set_labels(
                issue_number,
                add=[LABEL_PR_OPEN],
                remove=[LABEL_IN_PROGRESS],
            ):
                print(
                    f"[cai fix] WARNING: label transition to :pr-open failed twice for "
                    f"#{issue_number} — issue may be orphaned from PR {pr_url}",
                    file=sys.stderr, flush=True,
                )
        locked = False
        log_run("fix", repo=REPO, issue=issue_number, branch=branch,
                pr=pr_number, diff_files=diff_files, exit=0)
        return 0

    except Exception as e:
        print(f"[cai fix] unexpected failure: {e!r}", file=sys.stderr)
        rollback()
        log_run("fix", repo=REPO, issue=issue_number,
                result=f"unexpected_error", exit=1)
        return 1
    finally:
        _clear_active_job()
        if work_dir.exists():
            shutil.rmtree(work_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# revise
# ---------------------------------------------------------------------------


# Heading markers used by cmd_fix and cmd_revise when they post
# comments to PRs/issues. The revise subcommand uses these to filter
# out bot-generated comments from the "unaddressed" set, which would
# otherwise cause self-loops (the bot acting on its own output).
#
# Login-based self-filtering doesn't work reliably in cai's common
# deployment pattern: the container uses the human operator's gh token,
# so the bot's "identity" is the same as the user's. Content-based
# marker matching is the robust alternative.
#
# IMPORTANT: only "no-action" / "summary" bot comments belong here.
# Comments that contain ACTIONABLE content for the revise subagent
# (most notably review-pr findings) must NOT be in this list — they
# need to flow through to the unaddressed set so revise can act on
# them. The "## cai pre-merge review (clean)" form is filtered (no
# findings → nothing for revise to do). The plain "## cai pre-merge
# review" form is NOT filtered because it carries `### Finding:`
# blocks that revise should address.
_BOT_COMMENT_MARKERS = (
    "## Fix subagent:",
    "## Revise subagent:",
    "## Revision summary",
    "## cai pre-merge review (clean)",
    "## cai docs review (clean)",
    "## cai merge verdict",
)


def _is_bot_comment(comment: dict) -> bool:
    """Return True if a comment body looks like it was posted by a cai subagent."""
    body = (comment.get("body") or "").lstrip()
    return any(body.startswith(m) for m in _BOT_COMMENT_MARKERS)


# Marker that revise/cmd_fix uses when its subagent decides no code
# changes are needed in response to a comment. The presence of this
# marker AFTER all human comments means the bot has acknowledged the
# request and explicitly chose not to act — so we should NOT keep
# re-processing the same comments forever.
_NO_ADDITIONAL_CHANGES_MARKER = "## Revise subagent: no additional changes"

# Marker that revise posts when an auto-rebase against main fails
# even after the resolver subagent has tried to merge the conflicts.
# If this marker appears AFTER the current commit, we short-circuit
# the loop on the very next tick: the resolver has already taken its
# best shot and failed, and re-trying every cron tick just spams the
# PR with identical failure comments. One failed resolver attempt is
# enough — see #188.
#
# NOTE: this string is intentionally distinct from the legacy
# "## Revise subagent: rebase failed" marker so PRs that were stuck
# under the pre-resolver code path get exactly one fresh attempt
# with the new resolver before being skipped again.
_REBASE_FAILED_MARKER = "## Revise subagent: rebase resolution failed"


def _parse_iso_ts(value):
    """Parse an ISO-8601 UTC timestamp ('2026-04-10T00:23:34Z'), return datetime or None."""
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def _filter_unaddressed_comments(comments: list[dict], commit_ts):
    """Return comments that are truly unaddressed by either code or bot reply.

    A comment is unaddressed if ALL of these are true:
      1. createdAt > commit_ts (it was posted after the current code state)
      2. it is not a cai bot self-comment
      3. there is no later '## Revise subagent: no additional changes'
         reply that already covered it (otherwise the loop would re-process
         the same comment forever, since revise's empty-diff path doesn't
         push a new commit and the createdAt > commit_ts check stays true)

    `commit_ts` should be a timezone-aware datetime in UTC.
    """
    if commit_ts is None:
        return []

    # Find the most recent "no additional changes" reply that is also
    # newer than the commit (older replies belong to a previous round
    # and shouldn't suppress current comments).
    latest_no_changes_ts = None
    for c in comments:
        body = (c.get("body") or "").lstrip()
        if not body.startswith(_NO_ADDITIONAL_CHANGES_MARKER):
            continue
        ts = _parse_iso_ts(c.get("createdAt"))
        if ts is None or ts <= commit_ts:
            continue
        if latest_no_changes_ts is None or ts > latest_no_changes_ts:
            latest_no_changes_ts = ts

    unaddressed = []
    for c in comments:
        ts = _parse_iso_ts(c.get("createdAt"))
        if ts is None or ts <= commit_ts:
            continue
        if _is_bot_comment(c):
            continue
        # The bot already replied "no additional changes" newer than
        # this comment — treat as addressed (without code change).
        if latest_no_changes_ts is not None and ts < latest_no_changes_ts:
            continue
        unaddressed.append(c)
    return unaddressed


def _fetch_review_comments(pr_number: int) -> list[dict]:
    """Fetch line-by-line review comments for a PR, normalized to issue-comment shape.

    `gh pr view --json comments` only returns issue-level comments. Line-
    by-line review comments (left on specific lines in the diff) live on
    a separate REST endpoint. This helper fetches them via `gh api` and
    reshapes each one to match the issue-comment format used by the rest
    of the revise logic: `{author: {login}, createdAt, body}`.

    The body is prefixed with a `(line comment on path:line)` marker so
    the subagent knows where the comment is anchored in the diff.
    """
    try:
        result = _run(
            ["gh", "api", f"repos/{REPO}/pulls/{pr_number}/comments"],
            capture_output=True,
        )
        if result.returncode != 0:
            return []
        raw = json.loads(result.stdout)
    except (json.JSONDecodeError, ValueError):
        return []

    normalized = []
    for c in raw:
        author_login = c.get("user", {}).get("login", "")
        created_at = c.get("created_at", "")
        body = c.get("body", "")
        path = c.get("path", "")
        line_num = c.get("line") or c.get("original_line")
        if path and line_num:
            body = f"(line comment on `{path}:{line_num}`)\n\n{body}"
        elif path:
            body = f"(line comment on `{path}`)\n\n{body}"
        normalized.append({
            "author": {"login": author_login},
            "createdAt": created_at,
            "body": body,
        })
    return normalized


def _rebase_conflict_files(work_dir: Path) -> list[str]:
    """Return the list of files currently in a conflicted (unmerged) state."""
    res = _git(
        work_dir, "diff", "--name-only", "--diff-filter=U", check=False,
    )
    return [line for line in res.stdout.strip().splitlines() if line]


def _select_revise_targets() -> list[dict]:
    """Return PRs needing revision (unaddressed comments since last commit).

    Eligible = branch matches auto-improve/<N>-* AND linked issue has
    label auto-improve:pr-open AND does NOT have label
    auto-improve:revising. Returns a list of dicts with keys:
    pr_number, issue_number, branch, comments (the unaddressed ones).

    Reads BOTH issue-level comments (via `gh pr list --json comments`)
    and line-by-line review comments (via `gh api .../pulls/N/comments`)
    so reviewers can leave either kind of feedback.
    """
    try:
        prs = _gh_json([
            "pr", "list",
            "--repo", REPO,
            "--state", "open",
            "--json", "number,headRefName,comments,labels",
            "--limit", "50",
        ]) or []
    except subprocess.CalledProcessError as e:
        print(f"[cai revise] gh pr list failed:\n{e.stderr}", file=sys.stderr)
        return []

    targets = []
    for pr in prs:
        branch = pr.get("headRefName", "")
        m = re.match(r"^auto-improve/(\d+)-", branch)
        if not m:
            continue
        issue_number = int(m.group(1))

        # Check that the linked issue has :pr-open label.
        try:
            issue = _gh_json([
                "issue", "view", str(issue_number),
                "--repo", REPO,
                "--json", "labels,state",
            ])
        except subprocess.CalledProcessError:
            continue
        if not issue or issue.get("state", "").upper() != "OPEN":
            continue
        label_names = {lbl["name"] for lbl in issue.get("labels", [])}
        if LABEL_PR_OPEN not in label_names:
            continue
        if LABEL_REVISING in label_names:
            continue

        # Skip PRs that are blocked on a human decision — revising
        # code won't unblock them and causes an infinite loop.
        # Issue #399.
        #
        # NOTE: :merge-blocked is handled below, *after* we've parsed
        # comments, so a fresh human comment can auto-clear it and
        # resume the revise loop (no more manual label toggling).
        pr_label_names = {lbl["name"] for lbl in pr.get("labels", [])}
        if LABEL_PR_NEEDS_HUMAN in pr_label_names:
            print(
                f"[cai revise] PR #{pr['number']}: skipping — "
                f"PR has :{LABEL_PR_NEEDS_HUMAN} (needs human decision)",
                flush=True,
            )
            continue

        # Find the most recent commit date via `gh pr view`.
        pr_detail = None
        try:
            pr_detail = _gh_json([
                "pr", "view", str(pr["number"]),
                "--repo", REPO,
                "--json", "commits,mergeable,mergeStateStatus,labels",
            ])
            commits = pr_detail.get("commits", [])
            if commits:
                last_commit_date = commits[-1].get("committedDate", "")
            else:
                last_commit_date = ""
        except Exception:
            last_commit_date = ""

        # Secondary per-PR label check (issue #432): gh pr list may
        # return stale label data for recently-modified PRs.  The
        # per-PR gh pr view call above gives a fresh snapshot.
        if pr_detail:
            detail_label_names = {lbl["name"] for lbl in pr_detail.get("labels", [])}
            if LABEL_PR_NEEDS_HUMAN in detail_label_names:
                print(
                    f"[cai revise] PR #{pr['number']}: skipping — "
                    f"PR has :{LABEL_PR_NEEDS_HUMAN} (fresh per-PR check, "
                    f"needs human decision)",
                    flush=True,
                )
                continue

        if not last_commit_date:
            continue

        # Parse commit timestamp.
        try:
            commit_ts = datetime.strptime(
                last_commit_date, "%Y-%m-%dT%H:%M:%SZ"
            ).replace(tzinfo=timezone.utc)
        except ValueError:
            continue

        # Merge issue-level and line-by-line review comments.
        issue_comments = pr.get("comments", [])
        line_comments = _fetch_review_comments(pr["number"])
        comments = issue_comments + line_comments

        # Filter: createdAt > commit_ts AND not bot AND not already
        # acknowledged by a "no additional changes" reply (loop guard).
        unaddressed = _filter_unaddressed_comments(comments, commit_ts)

        # :merge-blocked auto-clear. If the issue carries :merge-blocked
        # AND a human has posted an unaddressed comment since the last
        # commit, treat the comment as the human decision to resume and
        # strip the label so revise can act on it. If there are no new
        # human comments, keep skipping — the PR is still waiting on a
        # human. Previously humans had to remove the label by hand,
        # which was easy to forget (see chicken-and-egg with cmd_merge
        # holding the PR back on unaddressed review-pr comments that
        # only revise can address).
        if LABEL_MERGE_BLOCKED in label_names:
            if not unaddressed:
                print(
                    f"[cai revise] PR #{pr['number']}: skipping — "
                    f"issue has :{LABEL_MERGE_BLOCKED} (needs human decision)",
                    flush=True,
                )
                continue
            if not _set_labels(
                issue_number,
                remove=[LABEL_MERGE_BLOCKED],
                log_prefix="cai revise",
            ):
                print(
                    f"[cai revise] PR #{pr['number']}: failed to clear "
                    f":{LABEL_MERGE_BLOCKED} on issue #{issue_number}; skipping",
                    file=sys.stderr, flush=True,
                )
                continue
            print(
                f"[cai revise] PR #{pr['number']}: cleared "
                f":{LABEL_MERGE_BLOCKED} on issue #{issue_number} — "
                f"{len(unaddressed)} new human comment(s) since last commit",
                flush=True,
            )

        # Determine if the PR needs a rebase (unmergeable).
        needs_rebase = pr_detail.get("mergeable") == "CONFLICTING" or \
            pr_detail.get("mergeStateStatus") == "DIRTY"

        # Loop guard: if the bot has already posted a rebase-failed
        # comment after the current commit, stop retrying. The conflict
        # will not resolve itself across revise ticks, so one failed
        # attempt is enough — a human (or a fresh fix branch) is
        # required to move forward. See issue #188.
        if needs_rebase and any(
            (c.get("body") or "").lstrip().startswith(_REBASE_FAILED_MARKER)
            and (_parse_iso_ts(c.get("createdAt")) or commit_ts) > commit_ts
            for c in comments
        ):
            print(
                f"[cai revise] PR #{pr['number']}: prior rebase failure "
                "since last commit; skipping (needs human rebase)",
                flush=True,
            )
            needs_rebase = False

        if not unaddressed and not needs_rebase:
            continue

        targets.append({
            "pr_number": pr["number"],
            "issue_number": issue_number,
            "branch": branch,
            "comments": unaddressed,
            "needs_rebase": needs_rebase,
        })

    return targets


def _recover_stuck_rebase_prs() -> int:
    """Close PRs the rebase resolver gave up on so the fix subagent
    can re-attempt them from a fresh branch off current main.

    Trigger condition: an open `auto-improve/<N>-*` PR has a
    `## Revise subagent: rebase resolution failed` comment newer than
    its latest commit. The loop guard from #196 already stops the
    revise step from spamming retry comments — but without recovery
    the PR sits stuck forever, accumulating an ever-larger conflict
    surface every time main moves. Closing it and resetting the issue
    to `:refined` lets the fix subagent open a fresh PR against the
    current main on its next tick (#144 was the original symptom).

    Returns the number of PRs recovered.
    """
    try:
        prs = _gh_json([
            "pr", "list",
            "--repo", REPO,
            "--state", "open",
            "--limit", "100",
            "--json",
            "number,headRefName,comments,commits",
        ])
    except subprocess.CalledProcessError:
        return 0

    recovered = 0
    for pr in prs:
        branch = pr.get("headRefName", "")
        if not branch.startswith("auto-improve/"):
            continue

        # Pull issue number from the branch name (`auto-improve/<N>-*`).
        m = re.match(r"auto-improve/(\d+)-", branch)
        if not m:
            continue
        issue_number = int(m.group(1))
        pr_number = pr["number"]

        commits = pr.get("commits", [])
        last_commit_date = commits[-1].get("committedDate", "") if commits else ""
        commit_ts = _parse_iso_ts(last_commit_date)
        if commit_ts is None:
            continue

        # Look for a `rebase resolution failed` marker newer than the
        # latest commit — that means the resolver tried, failed, and
        # nothing has moved since.
        stuck = False
        for c in pr.get("comments", []):
            body = (c.get("body") or "").lstrip()
            if not body.startswith(_REBASE_FAILED_MARKER):
                continue
            ts = _parse_iso_ts(c.get("createdAt"))
            if ts is None or ts <= commit_ts:
                continue
            stuck = True
            break
        if not stuck:
            continue

        print(
            f"[cai revise] PR #{pr_number}: rebase resolver gave up "
            f"and no commits since; closing and resetting issue "
            f"#{issue_number} to :refined so fix can retry",
            flush=True,
        )

        comment = (
            "## Revise subagent: closing stuck PR for fresh attempt\n\n"
            "The rebase resolver could not land this branch onto "
            "current `main` and no further progress is possible from "
            f"this branch. Closing so the fix subagent can re-open a "
            f"fresh PR for #{issue_number} against the current `main`.\n\n"
            "---\n"
            "_Closed automatically by `cai revise` recovery. The "
            "linked issue has been reset to `auto-improve:refined` and "
            "will be picked up on the next `cai fix` tick._"
        )
        close_res = _run(
            ["gh", "pr", "close", str(pr_number),
             "--repo", REPO, "--delete-branch", "--comment", comment],
            capture_output=True,
        )
        if close_res.returncode != 0:
            print(
                f"[cai revise] PR #{pr_number}: gh pr close failed:\n"
                f"{close_res.stderr}",
                file=sys.stderr,
            )
            continue

        # Reset the linked issue back to the eligible-for-fix state.
        # NOTE: LABEL_MERGE_BLOCKED is intentionally NOT removed here.
        # If cmd_fix opens a fresh PR for this issue, the revise guard
        # in _select_revise_targets() must still see merge-blocked until
        # cmd_merge re-evaluates the new PR and removes it.  cmd_merge
        # explicitly does NOT skip on merge-blocked, so it will evaluate
        # the new PR regardless.  Issue #432.
        _set_labels(
            issue_number,
            add=[LABEL_REFINED],
            remove=[LABEL_PR_OPEN, LABEL_REVISING],
            log_prefix="cai revise",
        )
        log_run("revise", repo=REPO, pr=pr_number, issue=issue_number,
                result="recovered_stuck_rebase", exit=0)
        recovered += 1

    return recovered


def cmd_revise(args) -> int:
    """Iterate on open PRs based on review comments."""
    print("[cai revise] checking for PRs with unaddressed comments", flush=True)

    # Recover any PRs the rebase resolver has given up on, so they
    # don't sit stuck forever. Refs #144.
    recovered = _recover_stuck_rebase_prs()
    if recovered:
        print(
            f"[cai revise] recovered {recovered} stuck PR(s) for fresh fix attempt",
            flush=True,
        )

    if getattr(args, "pr", None) is not None:
        # Direct targeting: look up the specified PR and build a single-item target list.
        try:
            pr_detail = _gh_json([
                "pr", "view", str(args.pr),
                "--repo", REPO,
                "--json", "number,headRefName,comments,labels,commits,mergeable,mergeStateStatus",
            ])
        except subprocess.CalledProcessError as e:
            print(f"[cai revise] gh pr view #{args.pr} failed:\n{e.stderr}", file=sys.stderr)
            log_run("revise", repo=REPO, pr=args.pr, result="pr_lookup_failed", exit=1)
            return 1
        branch = pr_detail.get("headRefName", "")
        m = re.match(r"^auto-improve/(\d+)-", branch)
        if not m:
            print(f"[cai revise] PR #{args.pr} branch '{branch}' is not an auto-improve branch", file=sys.stderr)
            log_run("revise", repo=REPO, pr=args.pr, result="not_auto_improve", exit=1)
            return 1
        issue_number = int(m.group(1))
        # Collect comments (issue-level + line-by-line review).
        issue_comments = pr_detail.get("comments", [])
        line_comments = _fetch_review_comments(pr_detail["number"])
        all_comments = issue_comments + line_comments
        # Use commit timestamp to filter unaddressed comments.
        commits = pr_detail.get("commits", [])
        if commits:
            last_commit_date = commits[-1].get("committedDate", "")
            try:
                commit_ts = datetime.strptime(
                    last_commit_date, "%Y-%m-%dT%H:%M:%SZ"
                ).replace(tzinfo=timezone.utc)
            except ValueError:
                commit_ts = datetime.min.replace(tzinfo=timezone.utc)
        else:
            commit_ts = datetime.min.replace(tzinfo=timezone.utc)
        unaddressed = _filter_unaddressed_comments(all_comments, commit_ts)
        needs_rebase = pr_detail.get("mergeable") == "CONFLICTING" or \
            pr_detail.get("mergeStateStatus") == "DIRTY"
        targets = [{
            "pr_number": pr_detail["number"],
            "issue_number": issue_number,
            "branch": branch,
            "comments": unaddressed,
            "needs_rebase": needs_rebase,
        }]
    else:
        targets = _select_revise_targets()
    if not targets:
        print("[cai revise] no PRs need revision; nothing to do", flush=True)
        log_run("revise", repo=REPO, result="no_targets",
                recovered=recovered, exit=0)
        return 0

    print(f"[cai revise] found {len(targets)} PR(s) to revise", flush=True)

    had_failure = False
    for target in targets:
        pr_number = target["pr_number"]
        issue_number = target["issue_number"]
        branch = target["branch"]
        comments = target["comments"]

        print(
            f"[cai revise] revising PR #{pr_number} (issue #{issue_number}, "
            f"{len(comments)} unaddressed comment(s))",
            flush=True,
        )

        # 1. Lock — add :revising label.
        if not _set_labels(issue_number, add=[LABEL_REVISING], log_prefix="cai revise"):
            print(
                f"[cai revise] could not lock #{issue_number}",
                file=sys.stderr,
            )
            log_run("revise", repo=REPO, pr=pr_number,
                    result="lock_failed", exit=1)
            had_failure = True
            continue

        _run(["gh", "auth", "setup-git"], capture_output=True)
        _write_active_job("revise", "issue", issue_number)

        _uid = uuid.uuid4().hex[:8]
        work_dir = Path(f"/tmp/cai-revise-{issue_number}-{_uid}")

        try:
            if work_dir.exists():
                shutil.rmtree(work_dir)

            # 2. Clone and check out the existing branch.
            clone = _run(
                ["gh", "repo", "clone", REPO, str(work_dir)],
                capture_output=True,
            )
            if clone.returncode != 0:
                print(
                    f"[cai revise] clone failed:\n{clone.stderr}",
                    file=sys.stderr,
                )
                _set_labels(issue_number, remove=[LABEL_REVISING], log_prefix="cai revise")
                log_run("revise", repo=REPO, pr=pr_number,
                        result="clone_failed", exit=1)
                had_failure = True
                continue

            _git(work_dir, "fetch", "origin", branch)
            _git(work_dir, "checkout", branch)

            # 3. Configure git identity.
            name, email = _gh_user_identity()
            _git(work_dir, "config", "user.name", name)
            _git(work_dir, "config", "user.email", email)

            # 3b. Deterministically attempt the rebase onto main.
            #     We always rebase — if main hasn't moved, it's a
            #     no-op. Depending on what's needed, the wrapper
            #     routes to: (a) early exit if clean + no comments,
            #     (b) cai-rebase (haiku) if conflicts + no comments,
            #     or (c) cai-revise (sonnet) if comments ± conflicts.
            _git(work_dir, "fetch", "origin", "main")
            pre_agent_head = _git(
                work_dir, "rev-parse", "HEAD", check=False,
            ).stdout.strip()
            rebase = _git(
                work_dir, "rebase", "origin/main", check=False,
            )

            rebase_merge_dir = work_dir / ".git" / "rebase-merge"
            rebase_apply_dir = work_dir / ".git" / "rebase-apply"
            rebase_in_progress = (
                rebase_merge_dir.exists() or rebase_apply_dir.exists()
            )

            if rebase_in_progress:
                conflict_files = _rebase_conflict_files(work_dir)
                rebase_state_block = (
                    "## Rebase state\n\n"
                    f"**Status:** in progress — {len(conflict_files)} "
                    "conflicted file(s)\n\n"
                    "The wrapper ran `git rebase origin/main` and it "
                    "stopped on conflicts. You must drive the rebase "
                    "to completion before addressing review comments. "
                    "Conflicted files:\n\n"
                    + "\n".join(f"- `{f}`" for f in conflict_files)
                    + "\n"
                )
                print(
                    f"[cai revise] PR #{pr_number}: rebase stopped on "
                    f"{len(conflict_files)} conflict(s); handing to agent",
                    flush=True,
                )
            elif rebase.returncode != 0:
                # Rebase failed but no rebase in progress — anomalous
                # state (git refused to start the rebase at all). Bail
                # loudly rather than hand a broken worktree to the agent.
                print(
                    f"[cai revise] PR #{pr_number}: rebase exit "
                    f"{rebase.returncode} with no in-progress state:\n"
                    f"{rebase.stderr}",
                    file=sys.stderr,
                )
                _set_labels(issue_number, remove=[LABEL_REVISING], log_prefix="cai revise")
                log_run("revise", repo=REPO, pr=pr_number,
                        result="rebase_weird_failure", exit=1)
                had_failure = True
                continue
            else:
                rebase_state_block = (
                    "## Rebase state\n\n"
                    "**Status:** clean — `git rebase origin/main` "
                    "completed without conflicts. You can skip straight "
                    "to addressing review comments (if any).\n"
                )

            # 3c. Early exit: clean rebase with no comments.
            #     If the rebase completed without conflicts AND there
            #     are no unaddressed review comments, skip agent
            #     invocation entirely. Just force-push if HEAD moved
            #     (rebase may have advanced commits) and unlock.
            if not rebase_in_progress and not comments:
                post_rebase_head = _git(
                    work_dir, "rev-parse", "HEAD", check=False,
                ).stdout.strip()
                if pre_agent_head != post_rebase_head:
                    push = _run(
                        ["git", "-C", str(work_dir), "push",
                         "--force-with-lease", "origin", branch],
                        capture_output=True,
                    )
                    if push.returncode != 0:
                        print(
                            f"[cai revise] noop push failed:\n{push.stderr}",
                            file=sys.stderr,
                        )
                        _set_labels(issue_number, remove=[LABEL_REVISING],
                                    log_prefix="cai revise")
                        log_run("revise", repo=REPO, pr=pr_number,
                                result="noop_push_failed", exit=1)
                        had_failure = True
                        continue
                    print(
                        f"[cai revise] clean rebase pushed for PR #{pr_number} "
                        "(no comments to address)",
                        flush=True,
                    )
                else:
                    print(
                        f"[cai revise] PR #{pr_number}: rebase was no-op and "
                        "no comments to address; skipping agent",
                        flush=True,
                    )
                _set_labels(issue_number, remove=[LABEL_REVISING],
                            log_prefix="cai revise")
                log_run("revise", repo=REPO, pr=pr_number,
                        result="noop_clean", exit=0)
                continue

            # 4. Fetch original issue body.
            try:
                issue_data = _gh_json([
                    "issue", "view", str(issue_number),
                    "--repo", REPO,
                    "--json", "number,title,body",
                ])
            except subprocess.CalledProcessError:
                issue_data = {"number": issue_number, "title": "(unknown)", "body": ""}

            # 4b. Describe the PR's current state to the agent.
            #
            #     Historically this block dumped the full unified
            #     `gh pr diff` into the user message — a large token
            #     sink on PRs that touch many lines, especially since
            #     cai-revise runs every cycle for the full lifetime
            #     of a PR. The full diff is now gone entirely: the
            #     agent gets a compact `git diff origin/main..HEAD
            #     --stat` summary as a file-level map, and explores
            #     the clone itself (Read, Grep, Glob, and delegation
            #     to Explore) when it needs the actual content.
            #
            #     When `.cai/pr-context.md` is present (`cai-fix`
            #     writes it on every non-empty PR), the dossier is
            #     the richer map and the agent Reads it first. When
            #     it is missing (legacy PRs, or PRs where cai-fix
            #     exited with zero diff), the stat alone is enough —
            #     the agent uses it as the entry point and explores
            #     from there, then writes a fresh dossier before
            #     exiting so the next revise cycle has one.
            dossier_path = work_dir / ".cai" / "pr-context.md"
            stat_result = _git(
                work_dir, "diff", "origin/main..HEAD", "--stat",
                check=False,
            )
            pr_stat = (stat_result.stdout or "").strip() or (
                "(no changes vs origin/main)"
            )
            if dossier_path.exists():
                pr_state_block = (
                    f"## Current PR state\n\n"
                    f"A PR context dossier is present at "
                    f"`{work_dir}/.cai/pr-context.md` — **Read it "
                    f"first.** It lists the files touched, key "
                    f"symbols, design decisions, out-of-scope gaps, "
                    f"and invariants the change relies on. Use it as "
                    f"the ground-truth map of what this PR is doing "
                    f"and Read specific files in the clone for the "
                    f"actual current content.\n\n"
                    f"The full unified diff is **not** included — "
                    f"the dossier plus on-demand Reads is cheaper "
                    f"and more accurate. A `git diff "
                    f"origin/main..HEAD --stat` summary follows as a "
                    f"file-level map:\n\n"
                    f"```\n{pr_stat}\n```\n\n"
                )
            else:
                pr_state_block = (
                    f"## Current PR state\n\n"
                    f"_No `.cai/pr-context.md` dossier was found — "
                    f"this is a legacy PR or one where `cai-fix` "
                    f"exited with zero diff. The full unified diff "
                    f"is **not** included either — it is a token "
                    f"sink on large PRs and you can reconstruct the "
                    f"same information more accurately by Reading "
                    f"files in the clone directly._\n\n"
                    f"A `git diff origin/main..HEAD --stat` summary "
                    f"follows as a file-level map. **Use it as your "
                    f"entry point:** Read the listed files in the "
                    f"clone to see the actual current content, use "
                    f"Grep/Glob or the Explore subagent for any "
                    f"broader context you need, and — if you make "
                    f"code changes in this revision — create a "
                    f"minimal dossier at "
                    f"`{work_dir}/.cai/pr-context.md` before exiting "
                    f"(see `.claude/agents/cai-fix.md` → 'Before you "
                    f"exit: write the PR context dossier') so the "
                    f"next revise cycle starts with one.\n\n"
                    f"```\n{pr_stat}\n```\n\n"
                )

            # 5. Build the user message. The system prompt, tool
            #    allowlist (Agent + edit tools), and hard rules all
            #    live in `.claude/agents/cai-revise.md`.
            comments_section = "## Unaddressed review comments\n\n"
            if comments:
                for c in comments:
                    author = c.get("author", {}).get("login", "unknown")
                    body = c.get("body", "")
                    created = c.get("createdAt", "")
                    comments_section += (
                        f"### Comment by @{author} ({created})\n\n"
                        f"{body}\n\n"
                    )
            else:
                comments_section += (
                    "(none — only the rebase needed attention)\n"
                )

            user_message = (
                _work_directory_block(work_dir)
                + "\n"
                + f"{rebase_state_block}\n"
                + f"## Original issue\n\n"
                + f"### #{issue_data['number']} — {issue_data.get('title', '')}\n\n"
                + f"{issue_data.get('body') or '(no body)'}\n\n"
                + pr_state_block
                + comments_section
            )

            # 5b. Pre-create the `.cai-staging/agents/` directory so
            #     the agent has somewhere to write proposed updates
            #     to its own `.claude/agents/*.md` file(s). See
            #     `_setup_agent_edit_staging` for why we need this
            #     workaround.
            _setup_agent_edit_staging(work_dir)

            # 5c. Choose agent: rebase-only conflicts → haiku agent,
            #     otherwise → full cai-revise.
            rebase_only = rebase_in_progress and not comments
            agent_name = "cai-rebase" if rebase_only else "cai-revise"

            # 6. Invoke the declared subagent.
            #    Runs with `cwd=/app` and `--add-dir <work_dir>` so
            #    the agent reads its own definition (and memory)
            #    from the canonical /app paths while operating on
            #    the clone via absolute paths.
            #
            #    `--dangerously-skip-permissions` is required for
            #    the permission gating on file Edit/Write in the
            #    clone. Claude-code's hardcoded `.claude/agents/*.md`
            #    protection is NOT bypassed by any flag — we route
            #    self-modifications through the staging directory
            #    instead (see _work_directory_block).
            #
            #    cai-revise/cai-rebase delegate git rebase ops to the
            #    cai-git haiku subagent via the Agent tool instead of
            #    running git commands directly — see the respective
            #    agent definition files for details.
            print(
                f"[cai revise] running {agent_name} subagent for {work_dir}",
                flush=True,
            )
            agent = _run_claude_p(
                ["claude", "-p", "--agent", agent_name,
                 "--dangerously-skip-permissions",
                 "--add-dir", str(work_dir)],
                category="revise",
                agent=agent_name,
                input=user_message,
                cwd="/app",
            )
            if agent.stdout:
                print(agent.stdout, flush=True)

            # 6b. Apply any `.claude/agents/*.md` updates the agent
            #     staged at `<work_dir>/.cai-staging/agents/`. We
            #     apply UNCONDITIONALLY (even on agent non-zero
            #     exit) because cai-revise's return code is
            #     dominated by rebase outcome — the agent may have
            #     completed a valid self-modification before
            #     hitting an unrelated rebase failure, and we'd
            #     rather preserve that work than silently discard
            #     it. If we end up rolling back the branch below,
            #     the staged edits go with it.
            applied = _apply_agent_edit_staging(work_dir)
            if applied:
                print(
                    f"[cai revise] applied {applied} staged "
                    f".claude/agents/*.md update(s)",
                    flush=True,
                )

            agent_summary = (agent.stdout or "").strip()[:4000]

            # 7. Trust but verify the final state. The agent may have
            #    (a) resolved a rebase and addressed comments, (b)
            #    resolved a rebase only, (c) addressed comments only,
            #    or (d) aborted the rebase because a conflict was
            #    ambiguous. All four cases need distinct handling.
            rebase_still_in_progress = (
                rebase_merge_dir.exists() or rebase_apply_dir.exists()
            )
            remaining_conflicts = _rebase_conflict_files(work_dir)

            rebase_failure = (
                rebase_still_in_progress
                or bool(remaining_conflicts)
                or agent.returncode != 0
            )

            if not rebase_failure:
                # Also check: HEAD must contain origin/main as an
                # ancestor. If the agent ran `git rebase --abort`,
                # the rebase is "not in progress" but HEAD is back
                # where it started — not on top of main.
                ancestry = _run(
                    ["git", "-C", str(work_dir), "merge-base",
                     "--is-ancestor", "origin/main", "HEAD"],
                    capture_output=True,
                )
                if ancestry.returncode != 0:
                    rebase_failure = True

            if rebase_failure:
                if rebase_still_in_progress:
                    _git(work_dir, "rebase", "--abort", check=False)
                comment_body = (
                    "## Revise subagent: rebase resolution failed\n\n"
                    "Could not auto-rebase against current main, and "
                    "the revise subagent could not resolve the "
                    "conflicts cleanly. Please rebase manually."
                )
                if agent_summary:
                    comment_body += (
                        "\n\n<details><summary>Agent notes</summary>"
                        f"\n\n{agent_summary}\n\n</details>"
                    )
                _run(
                    ["gh", "pr", "comment", str(pr_number),
                     "--repo", REPO, "--body", comment_body],
                    capture_output=True,
                )
                print(
                    f"[cai revise] rebase/agent failed for PR #{pr_number}; "
                    "posted comment",
                    flush=True,
                )
                _set_labels(issue_number, remove=[LABEL_REVISING], log_prefix="cai revise")
                log_run("revise", repo=REPO, pr=pr_number,
                        result="rebase_failed", exit=0)
                continue

            # 8. Determine what work actually happened.
            post_agent_head = _git(
                work_dir, "rev-parse", "HEAD", check=False,
            ).stdout.strip()
            status = _git(work_dir, "status", "--porcelain", check=False)
            has_uncommitted = bool(status.stdout.strip())
            head_changed = pre_agent_head != post_agent_head

            if not has_uncommitted and not head_changed:
                # Nothing happened — rebase was clean AND the agent
                # decided no comments were actionable.
                reasoning = agent_summary or "(no output from agent)"
                comment_body = (
                    f"## Revise subagent: no additional changes\n\n"
                    f"{reasoning}\n\n"
                    f"---\n"
                    f"_The revise subagent reviewed the comments but "
                    f"did not find actionable changes to make._"
                )
                _run(
                    ["gh", "pr", "comment", str(pr_number),
                     "--repo", REPO, "--body", comment_body],
                    capture_output=True,
                )
                _set_labels(issue_number, remove=[LABEL_REVISING], log_prefix="cai revise")
                print(
                    f"[cai revise] no changes for PR #{pr_number}; "
                    "posted comment",
                    flush=True,
                )
                log_run("revise", repo=REPO, pr=pr_number,
                        comments_addressed=0, exit=0)
                continue

            # 9. Commit any uncommitted review-comment edits.
            if has_uncommitted:
                _git(work_dir, "add", "-A")
                commit_msg = (
                    f"auto-improve: revise per review comments\n\n"
                    f"Refs {REPO}#{issue_number}"
                )
                _git(work_dir, "commit", "-m", commit_msg)

            # 10. Force-push the rebased and/or revised branch.
            push = _run(
                ["git", "-C", str(work_dir), "push", "--force-with-lease",
                 "origin", branch],
                capture_output=True,
            )
            if push.returncode != 0:
                print(
                    f"[cai revise] git push failed:\n{push.stderr}",
                    file=sys.stderr,
                )
                _set_labels(issue_number, remove=[LABEL_REVISING], log_prefix="cai revise")
                log_run("revise", repo=REPO, pr=pr_number,
                        result="push_failed", exit=1)
                had_failure = True
                continue

            print(
                f"[cai revise] force-pushed revision to {branch}",
                flush=True,
            )

            # 11. Post a summary comment describing what happened.
            if head_changed and has_uncommitted:
                summary_suffix = (
                    f"{len(comments)} review comment(s) addressed; "
                    "rebase was resolved by the subagent"
                )
            elif head_changed:
                summary_suffix = "rebase was resolved by the subagent"
            else:
                summary_suffix = (
                    f"{len(comments)} review comment(s) addressed; "
                    "rebase was already clean"
                )
            revision_comment = (
                f"## Revision summary\n\n"
                f"{agent_summary}\n\n"
                f"---\n"
                f"_Applied by `cai revise`. {summary_suffix}._\n"
            )
            _run(
                ["gh", "pr", "comment", str(pr_number),
                 "--repo", REPO, "--body", revision_comment],
                capture_output=True,
            )

            # 12. Remove lock label.
            _set_labels(issue_number, remove=[LABEL_REVISING], log_prefix="cai revise")
            log_run("revise", repo=REPO, pr=pr_number,
                    comments_addressed=len(comments), exit=0)

        except Exception as e:
            print(f"[cai revise] unexpected failure: {e!r}", file=sys.stderr)
            _set_labels(issue_number, remove=[LABEL_REVISING], log_prefix="cai revise")
            log_run("revise", repo=REPO, pr=pr_number,
                    result="unexpected_error", exit=1)
            had_failure = True
        finally:
            _clear_active_job()
            if work_dir.exists():
                shutil.rmtree(work_dir, ignore_errors=True)

    return 1 if had_failure else 0


# ---------------------------------------------------------------------------
# verify
# ---------------------------------------------------------------------------

def _find_linked_pr(issue_number: int):
    """Search PRs whose body references this issue. Returns the most recent."""
    try:
        prs = _gh_json([
            "pr", "list",
            "--repo", REPO,
            "--state", "all",
            "--search", f'"Refs {REPO}#{issue_number}" in:body',
            "--json", "number,state,mergedAt,headRefName,createdAt",
            "--limit", "20",
        ]) or []
    except subprocess.CalledProcessError as e:
        print(
            f"[cai verify] gh pr list (issue #{issue_number}) failed:\n{e.stderr}",
            file=sys.stderr,
        )
        return None
    if not prs:
        return None
    # Most recently created first.
    prs.sort(key=lambda p: p["createdAt"], reverse=True)
    return prs[0]


def cmd_verify(args) -> int:
    """Walk :pr-open issues and transition labels based on PR state."""
    print("[cai verify] checking pr-open issues", flush=True)
    try:
        issues = _gh_json([
            "issue", "list",
            "--repo", REPO,
            "--label", LABEL_PR_OPEN,
            "--state", "open",
            "--json", "number,title,labels",
            "--limit", "100",
        ]) or []
    except subprocess.CalledProcessError as e:
        print(f"[cai verify] gh issue list failed:\n{e.stderr}", file=sys.stderr)
        log_run("verify", repo=REPO, checked=0, transitioned=0, exit=1)
        return 1

    transitioned = 0
    pr_open_issue_nums = {i["number"] for i in issues}

    # Handle MERGED transitions inline; CLOSED and no-linked-PR recovery uses the shared helper.
    remaining = []
    for issue in issues:
        num = issue["number"]
        pr = _find_linked_pr(num)
        if pr is None:
            remaining.append(issue)
            continue
        state = (pr.get("state") or "").upper()
        if state == "MERGED":
            _set_labels(num, add=[LABEL_MERGED], remove=[LABEL_PR_OPEN, LABEL_MERGE_BLOCKED, LABEL_REVISING], log_prefix="cai verify")
            print(f"[cai verify] #{num}: PR #{pr['number']} merged → :merged", flush=True)
            transitioned += 1
        elif state == "CLOSED":
            remaining.append(issue)
        else:
            print(f"[cai verify] #{num}: PR #{pr['number']} still {state}", flush=True)

    transitioned += len(_recover_stale_pr_open(remaining, log_prefix="cai verify"))

    # Recovery: find open auto-improve PRs whose linked issue is missing
    # the :pr-open label.  This heals issues where the label transition
    # in cmd_fix step 10 failed silently.
    try:
        open_prs = _gh_json([
            "pr", "list",
            "--repo", REPO,
            "--state", "open",
            "--base", "main",
            "--json", "number,headRefName",
            "--limit", "100",
        ]) or []
    except subprocess.CalledProcessError:
        open_prs = []

    for opr in open_prs:
        branch = opr.get("headRefName", "")
        m = re.match(r"^auto-improve/(\d+)-", branch)
        if not m:
            continue
        issue_num = int(m.group(1))
        if issue_num in pr_open_issue_nums:
            continue
        # Check the issue's current state.
        try:
            iss = _gh_json([
                "issue", "view", str(issue_num),
                "--repo", REPO,
                "--json", "state,labels",
            ])
        except subprocess.CalledProcessError:
            continue
        if (iss.get("state") or "").upper() != "OPEN":
            continue
        iss_labels = {l["name"] for l in iss.get("labels", [])}
        if LABEL_PR_OPEN in iss_labels:
            continue
        # Issue is open, has an open PR, but missing :pr-open — recover.
        remove = [l for l in (LABEL_IN_PROGRESS, LABEL_REFINED, LABEL_PLANNED, LABEL_PLAN_APPROVED, LABEL_RAISED, LABEL_HUMAN_SUBMITTED, LABEL_AUDIT_RAISED) if l in iss_labels]
        if _set_labels(issue_num, add=[LABEL_PR_OPEN], remove=remove, log_prefix="cai verify"):
            print(
                f"[cai verify] recovered #{issue_num}: added :pr-open "
                f"(open PR #{opr['number']} on branch {branch})",
                flush=True,
            )
            transitioned += 1

    # Check parent issues for completion: close parents whose
    # sub-issues are all closed.
    try:
        parent_issues = _gh_json([
            "issue", "list",
            "--repo", REPO,
            "--label", LABEL_PARENT,
            "--state", "open",
            "--json", "number,body",
            "--limit", "50",
        ]) or []
    except subprocess.CalledProcessError:
        parent_issues = []

    for parent in parent_issues:
        body = parent.get("body") or ""
        sub_nums = re.findall(r"- \[[ x]\] #(\d+)", body)
        if not sub_nums:
            continue
        if all(_issue_is_closed(int(sn)) for sn in sub_nums):
            _run(
                ["gh", "issue", "close", str(parent["number"]),
                 "--repo", REPO,
                 "--comment",
                 "All sub-issues completed. Closing parent."],
                capture_output=True,
            )
            print(
                f"[cai verify] parent #{parent['number']}: "
                f"all sub-issues done — closed",
                flush=True,
            )

    print(f"[cai verify] done ({transitioned} transitioned)", flush=True)
    log_run("verify", repo=REPO, checked=len(issues), transitioned=transitioned, exit=0)
    return 0


# ---------------------------------------------------------------------------
# audit
# ---------------------------------------------------------------------------

_STALE_IN_PROGRESS_HOURS = 6
_STALE_REVISING_HOURS = 1
_STALE_NO_ACTION_DAYS = 7
_STALE_MERGED_DAYS = 14



def _cleanup_orphaned_branches() -> list[str]:
    """Delete remote auto-improve/* branches with no open PR.

    A branch is considered orphaned if it starts with 'auto-improve/' but
    has no open PR associated with it and is not owned by an
    :in-progress or :revising issue (which may not have opened their PR yet).
    Returns list of deleted branch names.
    """
    deleted: list[str] = []

    # 1. Fetch all remote branches.
    try:
        branches_data = _gh_json([
            "api", f"repos/{REPO}/branches",
            "--paginate",
        ]) or []
    except (subprocess.CalledProcessError, Exception):
        return deleted

    auto_branches = {
        b["name"] for b in branches_data
        if isinstance(b, dict) and b.get("name", "").startswith("auto-improve/")
    }
    if not auto_branches:
        return deleted

    # 2. Fetch all open PRs to find branches that already have an active PR.
    try:
        open_prs = _gh_json([
            "pr", "list",
            "--repo", REPO,
            "--state", "open",
            "--json", "headRefName",
            "--limit", "200",
        ]) or []
    except subprocess.CalledProcessError:
        return deleted

    branches_with_open_pr = {pr.get("headRefName", "") for pr in open_prs}

    # 3. Protect branches owned by :in-progress or :revising issues
    #    (the fix agent may have pushed the branch but not yet opened the PR).
    protected_prefixes: set[str] = set()
    for lock_label in (LABEL_IN_PROGRESS, LABEL_REVISING):
        try:
            issues = _gh_json([
                "issue", "list",
                "--repo", REPO,
                "--label", lock_label,
                "--state", "open",
                "--json", "number",
                "--limit", "100",
            ]) or []
        except subprocess.CalledProcessError:
            continue
        for issue in issues:
            num = issue.get("number")
            if num:
                protected_prefixes.add(f"auto-improve/{num}-")

    # 4. Delete orphaned branches.
    for branch in sorted(auto_branches):
        if branch in branches_with_open_pr:
            continue
        if any(branch.startswith(p) for p in protected_prefixes):
            continue
        result = _run([
            "gh", "api",
            "--method", "DELETE",
            f"repos/{REPO}/git/refs/heads/{branch}",
        ], capture_output=True)
        if result.returncode == 0:
            deleted.append(branch)
            print(f"[cai audit] deleted orphaned branch: {branch}", flush=True)

    return deleted


def _rollback_stale_in_progress(*, immediate: bool = False) -> list[dict]:
    """Deterministic rollback: :in-progress or :revising issues with no recent activity.

    When ``immediate=True`` every locked issue is rolled back regardless of age
    (used by ``cmd_cycle`` on container restart where all in-flight locks are
    guaranteed to be orphaned).

    Returns the list of issues that were rolled back.
    """
    all_issues = []
    for lock_label in (LABEL_IN_PROGRESS, LABEL_REVISING):
        try:
            issues = _gh_json([
                "issue", "list",
                "--repo", REPO,
                "--label", lock_label,
                "--state", "open",
                "--json", "number,title,updatedAt,createdAt,labels",
                "--limit", "100",
            ]) or []
        except subprocess.CalledProcessError as e:
            print(
                f"[cai audit] gh issue list ({lock_label}) failed:\n{e.stderr}",
                file=sys.stderr,
            )
            continue
        for issue in issues:
            issue["_lock_label"] = lock_label
            all_issues.append(issue)

    if not all_issues:
        return []

    issues = all_issues

    # Read the log tail to find the most recent [fix] line per issue.
    fix_timestamps: dict[int, float] = {}
    if LOG_PATH.exists():
        try:
            lines = LOG_PATH.read_text().splitlines()[-200:]
        except Exception:
            lines = []
        for line in lines:
            if "[fix]" not in line and "[revise]" not in line and "[spike]" not in line:
                continue
            # Extract issue number from "issue=<N>"
            m = re.search(r"issue=(\d+)", line)
            if not m:
                continue
            issue_num = int(m.group(1))
            # Extract timestamp from start of line (ISO format)
            ts_match = re.match(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)", line)
            if ts_match:
                try:
                    ts = datetime.strptime(ts_match.group(1), "%Y-%m-%dT%H:%M:%SZ").replace(
                        tzinfo=timezone.utc
                    ).timestamp()
                    fix_timestamps[issue_num] = max(fix_timestamps.get(issue_num, 0), ts)
                except ValueError:
                    pass

    now = datetime.now(timezone.utc).timestamp()
    rolled_back = []

    for issue in issues:
        issue_num = issue["number"]
        lock_label = issue.get("_lock_label", LABEL_IN_PROGRESS)
        ttl_hours = _STALE_REVISING_HOURS if lock_label == LABEL_REVISING else _STALE_IN_PROGRESS_HOURS
        threshold = 0 if immediate else ttl_hours * 3600
        last_fix = fix_timestamps.get(issue_num)
        if last_fix is not None:
            age = now - last_fix
        else:
            # No fix log line — use the issue's updatedAt as a fallback.
            try:
                updated = datetime.strptime(
                    issue["updatedAt"], "%Y-%m-%dT%H:%M:%SZ"
                ).replace(tzinfo=timezone.utc).timestamp()
            except (ValueError, KeyError):
                updated = 0
            age = now - updated

        if age > threshold:
            if lock_label == LABEL_REVISING:
                # Revising lock: just remove the lock, leave :pr-open.
                ok = _set_labels(issue_num, remove=[LABEL_REVISING], log_prefix="cai audit")
            else:
                # In-progress lock: roll back to the appropriate label.
                # Check originating label: spike-provenance issues go back to
                # :needs-spike; audit-raised go back to :audit-raised; all
                # others go back to :refined.
                issue_labels = {lbl["name"] for lbl in issue.get("labels", [])}
                if LABEL_AUDIT_RAISED in issue_labels:
                    raised_label = LABEL_AUDIT_RAISED
                elif LABEL_NEEDS_SPIKE in issue_labels:
                    raised_label = LABEL_NEEDS_SPIKE
                else:
                    raised_label = LABEL_REFINED
                ok = _set_labels(
                    issue_num,
                    add=[raised_label],
                    remove=[LABEL_IN_PROGRESS],
                    log_prefix="cai audit",
                )
            if ok:
                rolled_back.append(issue)
                log_run(
                    "audit",
                    action="stale_lock_rollback",
                    issue=issue_num,
                    lock_label=lock_label,
                    stale_hours=f"{age / 3600:.1f}",
                )
                print(
                    f"[cai audit] rolled back #{issue_num} "
                    f"(removed {lock_label}, stale {age / 3600:.1f}h)",
                    flush=True,
                )

    return rolled_back


def _unstuck_stale_no_action() -> list[dict]:
    """Roll stale :no-action issues back to :raised so refine (and subsequently fix) can retry with new context."""
    try:
        issues = _gh_json([
            "issue", "list",
            "--repo", REPO,
            "--label", LABEL_NO_ACTION,
            "--state", "open",
            "--json", "number,title,updatedAt",
            "--limit", "100",
        ]) or []
    except subprocess.CalledProcessError as e:
        print(
            f"[cai audit] gh issue list ({LABEL_NO_ACTION}) failed:\n{e.stderr}",
            file=sys.stderr,
        )
        return []

    now = datetime.now(timezone.utc).timestamp()
    threshold = _STALE_NO_ACTION_DAYS * 86400
    unstuck = []

    for issue in issues:
        try:
            updated = datetime.strptime(
                issue["updatedAt"], "%Y-%m-%dT%H:%M:%SZ"
            ).replace(tzinfo=timezone.utc).timestamp()
        except (ValueError, KeyError):
            updated = 0
        age = now - updated
        if age <= threshold:
            continue
        issue_num = issue["number"]
        ok = _set_labels(
            issue_num,
            add=[LABEL_RAISED],
            remove=[LABEL_NO_ACTION],
            log_prefix="cai audit",
        )
        if ok:
            unstuck.append(issue)
            log_run(
                "audit",
                action="stale_no_action_unstuck",
                issue=issue_num,
                stale_days=f"{age / 86400:.0f}",
            )
            print(
                f"[cai audit] unstuck #{issue_num} "
                f"(stale :no-action → :raised, {age / 86400:.0f} days)",
                flush=True,
            )

    return unstuck


def _flag_stale_merged() -> list[dict]:
    """Flag stale :merged issues for human intervention.

    These issues had their PRs merged but confirm has not resolved them.
    After the threshold, flag for human review since the automation
    cannot determine whether the fix actually worked.
    """
    try:
        issues = _gh_json([
            "issue", "list",
            "--repo", REPO,
            "--label", LABEL_MERGED,
            "--state", "open",
            "--json", "number,title,updatedAt,labels",
            "--limit", "100",
        ]) or []
    except subprocess.CalledProcessError as e:
        print(
            f"[cai audit] gh issue list ({LABEL_MERGED}) failed:\n{e.stderr}",
            file=sys.stderr,
        )
        return []

    now = datetime.now(timezone.utc).timestamp()
    threshold = _STALE_MERGED_DAYS * 86400
    flagged = []

    for issue in issues:
        issue_labels = {lbl["name"] for lbl in issue.get("labels", [])}
        if LABEL_PR_NEEDS_HUMAN in issue_labels:
            continue
        try:
            updated = datetime.strptime(
                issue["updatedAt"], "%Y-%m-%dT%H:%M:%SZ"
            ).replace(tzinfo=timezone.utc).timestamp()
        except (ValueError, KeyError):
            updated = 0
        age = now - updated
        if age <= threshold:
            continue
        issue_num = issue["number"]
        ok = _set_labels(issue_num, add=[LABEL_PR_NEEDS_HUMAN], log_prefix="cai audit")
        if ok:
            flagged.append(issue)
            log_run(
                "audit",
                action="stale_merged_flag",
                issue=issue_num,
                stale_days=f"{age / 86400:.0f}",
            )
            print(
                f"[cai audit] flagged #{issue_num} for human review "
                f"(stale :merged, {age / 86400:.0f} days)",
                flush=True,
            )

    return flagged


def cmd_audit(args) -> int:
    """Run the periodic queue/PR consistency audit."""
    print("[cai audit] running audit", flush=True)
    t0 = time.monotonic()

    # Step 1: Deterministic rollback of stale :in-progress issues.
    rolled_back = _rollback_stale_in_progress()

    # Step 1b: Delete orphaned auto-improve/* branches with no open PR
    #           (covers merged/closed-PR branches and branches with no PR at all).
    deleted_orphaned = _cleanup_orphaned_branches()
    if deleted_orphaned:
        print(
            f"[cai audit] cleaned up {len(deleted_orphaned)} orphaned branch(es)",
            flush=True,
        )

    # Step 1c: Unstuck stale :no-action issues (roll back to :raised).
    unstuck_no_action = _unstuck_stale_no_action()

    # Step 1d: Flag stale :merged issues for human review.
    flagged_merged = _flag_stale_merged()

    # Step 1e: Recover :pr-open issues whose linked PR was closed (unmerged).
    try:
        pr_open_issues = _gh_json([
            "issue", "list",
            "--repo", REPO,
            "--label", LABEL_PR_OPEN,
            "--state", "open",
            "--json", "number,title,body,labels,createdAt,comments",
            "--limit", "100",
        ]) or []
    except subprocess.CalledProcessError:
        pr_open_issues = []
    recovered_pr_open = _recover_stale_pr_open(pr_open_issues, log_prefix="cai audit")

    # Step 2: Gather GitHub state for the claude-driven semantic checks.

    # 2a. Open auto-improve issues (full detail).
    try:
        open_issues = _gh_json([
            "issue", "list",
            "--repo", REPO,
            "--label", "auto-improve",
            "--state", "open",
            "--json", "number,title,labels,body,createdAt,updatedAt",
            "--limit", "100",
        ]) or []
    except subprocess.CalledProcessError:
        open_issues = []

    # 2b. Recent PRs (last 30 or last 7 days, whichever is larger).
    try:
        recent_prs = _gh_json([
            "pr", "list",
            "--repo", REPO,
            "--state", "all",
            "--json", "number,title,state,mergedAt,createdAt,headRefName,body,labels",
            "--limit", "30",
        ]) or []
    except subprocess.CalledProcessError:
        recent_prs = []

    # 2c. Log tail.
    log_tail = ""
    if LOG_PATH.exists():
        try:
            lines = LOG_PATH.read_text().splitlines()[-200:]
            log_tail = "\n".join(lines)
        except Exception:
            log_tail = "(could not read log)"

    # Build the user message. The system prompt, tool allowlist,
    # and model choice all live in `.claude/agents/cai-audit.md` —
    # the wrapper only passes dynamic per-run context via stdin.
    issues_section = "## Open auto-improve issues\n\n"
    if open_issues:
        for oi in open_issues:
            label_names = [lbl["name"] for lbl in oi.get("labels", [])]
            issues_section += (
                f"### #{oi['number']} — {oi['title']}\n"
                f"- **Labels:** {', '.join(label_names)}\n"
                f"- **Created:** {oi['createdAt']}\n"
                f"- **Updated:** {oi['updatedAt']}\n"
                f"- **Body:** {(oi.get('body') or '(empty)')[:500]}\n\n"
            )
    else:
        issues_section += "(none)\n"

    prs_section = "## Recent PRs\n\n"
    if recent_prs:
        for pr in recent_prs:
            label_names = [lbl["name"] for lbl in pr.get("labels", [])]
            head_ref = pr.get("headRefName", "")
            body_snippet = (pr.get("body") or "")[:200].replace("\n", " ").strip()
            prs_section += (
                f"- PR #{pr['number']}: {pr['title']} "
                f"[{pr.get('state', 'unknown')}] "
                f"(created {pr['createdAt']}"
                f"{', merged ' + pr['mergedAt'] if pr.get('mergedAt') else ''})"
                f"{' labels: ' + ', '.join(label_names) if label_names else ''}"
                f"{' branch: ' + head_ref if head_ref else ''}"
                f"{' body: ' + body_snippet if body_snippet else ''}\n"
            )
    else:
        prs_section += "(none)\n"

    # 2d. Recently closed auto-improve issues.
    closed_issues = _fetch_closed_auto_improve_issues(limit=20)
    closed_section = "## Recently closed auto-improve issues\n\n"
    if closed_issues:
        for ci in closed_issues:
            closed_section += (
                f"- #{ci['number']}: {ci['title']} "
                f"[labels: {', '.join(ci['labels'])}] "
                f"(closed {ci['closedAt']}"
                f"{', rationale by ' + ci['rationale_author'] + ': ' + ci['rationale'][:200] if ci.get('rationale') else ', no rationale'})\n"
            )
    else:
        closed_section += "(none)\n"

    log_section = "## Log tail (last ~200 lines)\n\n```\n" + (log_tail or "(empty)") + "\n```\n"

    deterministic_section = ""
    if rolled_back:
        deterministic_section += "## Stale lock rollbacks performed this run\n\n"
        for rb in rolled_back:
            deterministic_section += f"- #{rb['number']}: {rb['title']}\n"
        deterministic_section += "\n"
    if unstuck_no_action:
        deterministic_section += "## Stale :no-action issues rolled back to :raised this run\n\n"
        for ci in unstuck_no_action:
            deterministic_section += f"- #{ci['number']}: {ci['title']}\n"
        deterministic_section += "\n"
    if flagged_merged:
        deterministic_section += "## Stale :merged issues flagged for human review this run\n\n"
        for ci in flagged_merged:
            deterministic_section += f"- #{ci['number']}: {ci['title']}\n"
        deterministic_section += "\n"
    if recovered_pr_open:
        deterministic_section += "## Stale :pr-open issues recovered (closed-unmerged PR) this run\n\n"
        for ri in recovered_pr_open:
            deterministic_section += f"- #{ri['number']}: {ri['title']}\n"
        deterministic_section += "\n"

    # Cost summary so the audit agent can flag cost outliers — same
    # window as the run-log tail (last 7 days, top 10 invocations).
    cost_section = _build_cost_summary(days=7, top_n=10)
    if not cost_section:
        cost_section = "## Cost summary\n\n(no cost-log entries yet)\n"

    user_message = (
        f"{issues_section}\n"
        f"{prs_section}\n"
        f"{log_section}\n"
        f"{cost_section}\n"
        f"{closed_section}\n"
        f"{deterministic_section}"
    )

    # Step 3: Invoke the declared cai-audit subagent.
    _write_active_job("audit", "none", None)
    try:
        audit = _run_claude_p(
            ["claude", "-p", "--agent", "cai-audit"],
            category="audit",
            agent="cai-audit",
            input=user_message,
        )
        print(audit.stdout, flush=True)
        if audit.returncode != 0:
            print(
                f"[cai audit] claude -p failed (exit {audit.returncode}):\n"
                f"{audit.stderr}",
                flush=True,
            )
            dur = f"{int(time.monotonic() - t0)}s"
            log_run("audit", repo=REPO, duration=dur,
                    pr_open_recovered=len(recovered_pr_open),
                    branches_cleaned=len(deleted_orphaned),
                    no_action_unstuck=len(unstuck_no_action),
                    merged_flagged=len(flagged_merged),
                    exit=audit.returncode)
            return audit.returncode

        # Step 4: Publish findings via publish.py with audit namespace.
        print("[cai audit] publishing audit findings", flush=True)
        published = _run(
            ["python", str(PUBLISH_SCRIPT), "--namespace", "audit"],
            input=audit.stdout,
        )
        dur = f"{int(time.monotonic() - t0)}s"
        log_run("audit", repo=REPO, rollbacks=len(rolled_back),
                pr_open_recovered=len(recovered_pr_open),
                branches_cleaned=len(deleted_orphaned),
                no_action_unstuck=len(unstuck_no_action),
                merged_flagged=len(flagged_merged),
                duration=dur, exit=published.returncode)
        return published.returncode
    finally:
        _clear_active_job()


# ---------------------------------------------------------------------------
# audit-triage — autonomous resolution of `audit:raised` findings
# ---------------------------------------------------------------------------


def _parse_triage_verdicts(text: str) -> list[dict]:
    """Parse `### Verdict: #N` blocks emitted by the audit-triage agent.

    Each verdict is a dict with keys: number (int), action (str),
    target (int|None), confidence (str), reasoning (str). Verdicts
    that fail to parse the basic shape are skipped.
    """
    verdicts: list[dict] = []
    blocks = re.split(r"^### Verdict:\s*", text, flags=re.MULTILINE)
    for block in blocks[1:]:
        lines = block.strip().splitlines()
        if not lines:
            continue
        header_match = re.match(r"#(\d+)", lines[0])
        if not header_match:
            continue
        body = "\n".join(lines[1:])
        action_m = re.search(
            r"^- \*\*Action:\*\*\s*`?(\w+)`?", body, flags=re.MULTILINE,
        )
        target_m = re.search(
            r"^- \*\*Target:\*\*\s*#(\d+)", body, flags=re.MULTILINE,
        )
        conf_m = re.search(
            r"^- \*\*Confidence:\*\*\s*`?(high|medium|low)`?",
            body, flags=re.MULTILINE | re.IGNORECASE,
        )
        reason_m = re.search(
            r"^- \*\*Reasoning:\*\*\s*(.+)$", body, flags=re.MULTILINE,
        )
        if not action_m or not conf_m:
            continue
        verdicts.append({
            "number": int(header_match.group(1)),
            "action": action_m.group(1).lower(),
            "target": int(target_m.group(1)) if target_m else None,
            "confidence": conf_m.group(1).lower(),
            "reasoning": reason_m.group(1).strip() if reason_m else "",
        })
    return verdicts


def cmd_audit_triage(args) -> int:
    """Autonomously resolve `audit:raised` findings without opening a PR.

    Calls a triage subagent that classifies each open `audit:raised`
    issue as one of: close_duplicate, close_resolved, passthrough,
    escalate. The wrapper then executes deterministically — only
    `close_*` verdicts at `high` confidence are acted on; everything
    else is left for the fix subagent or escalated to human triage
    via the `audit:needs-human` label.

    Refs #193.
    """
    print("[cai audit-triage] running audit triage", flush=True)
    t0 = time.monotonic()

    # 1. List `audit:raised` issues.
    try:
        raised_issues = _gh_json([
            "issue", "list", "--repo", REPO,
            "--label", LABEL_AUDIT_RAISED,
            "--state", "open",
            "--json", "number,title,labels,body,createdAt,updatedAt",
            "--limit", "100",
        ]) or []
    except subprocess.CalledProcessError as e:
        print(
            f"[cai audit-triage] gh issue list failed:\n{e.stderr}",
            file=sys.stderr,
        )
        log_run("audit-triage", repo=REPO, exit=1)
        return 1

    if not raised_issues:
        print(
            "[cai audit-triage] no audit:raised issues; nothing to do",
            flush=True,
        )
        log_run("audit-triage", repo=REPO, raised=0, closed_dup=0,
                closed_res=0, passthrough=0, escalated=0, exit=0)
        return 0

    print(
        f"[cai audit-triage] found {len(raised_issues)} audit:raised issue(s)",
        flush=True,
    )

    # 2. Gather context: all OTHER open auto-improve* issues + recent PRs.
    raised_numbers = {oi["number"] for oi in raised_issues}
    try:
        context_issues = _gh_json([
            "issue", "list", "--repo", REPO,
            "--label", "auto-improve",
            "--state", "open",
            "--json", "number,title,labels,body",
            "--limit", "100",
        ]) or []
    except subprocess.CalledProcessError:
        context_issues = []
    try:
        audit_context = _gh_json([
            "issue", "list", "--repo", REPO,
            "--label", "audit",
            "--state", "open",
            "--json", "number,title,labels,body",
            "--limit", "100",
        ]) or []
    except subprocess.CalledProcessError:
        audit_context = []
    # De-dupe by issue number; keep audit issues that aren't in raised set.
    seen = set()
    other_issues: list[dict] = []
    for oi in context_issues + audit_context:
        n = oi["number"]
        if n in seen or n in raised_numbers:
            continue
        seen.add(n)
        other_issues.append(oi)

    try:
        recent_prs = _gh_json([
            "pr", "list", "--repo", REPO,
            "--state", "all",
            "--json", "number,title,state,mergedAt,createdAt",
            "--limit", "30",
        ]) or []
    except subprocess.CalledProcessError:
        recent_prs = []

    # 3. Build the user message. System prompt, tool allowlist, and
    #    model (sonnet) all live in `.claude/agents/cai-audit-triage.md`.
    raised_section = "## audit:raised issues to triage\n\n"
    for oi in raised_issues:
        labels = ", ".join(lbl["name"] for lbl in oi.get("labels", []))
        raised_section += (
            f"### #{oi['number']} — {oi['title']}\n"
            f"- **Labels:** {labels}\n"
            f"- **Created:** {oi['createdAt']}\n"
            f"- **Body:**\n\n"
            f"{(oi.get('body') or '(empty)')}\n\n"
            "---\n\n"
        )

    other_section = "## Other open issues (for duplicate / state checks)\n\n"
    if other_issues:
        for oi in other_issues:
            labels = ", ".join(lbl["name"] for lbl in oi.get("labels", []))
            excerpt = (oi.get("body") or "(empty)")[:400]
            other_section += (
                f"### #{oi['number']} — {oi['title']}\n"
                f"- **Labels:** {labels}\n"
                f"- **Body excerpt:** {excerpt}\n\n"
            )
    else:
        other_section += "(none)\n\n"

    pr_section = "## Recent PRs\n\n"
    if recent_prs:
        for pr in recent_prs:
            merged = (
                f", merged {pr['mergedAt']}" if pr.get("mergedAt") else ""
            )
            pr_section += (
                f"- PR #{pr['number']}: {pr['title']} "
                f"[{pr.get('state', 'unknown')}] "
                f"(created {pr['createdAt']}{merged})\n"
            )
    else:
        pr_section += "(none)\n"

    user_message = (
        f"{raised_section}\n"
        f"{other_section}\n"
        f"{pr_section}\n"
    )

    # 4. Invoke the declared cai-audit-triage subagent.
    _write_active_job("audit-triage", "none", None)
    try:
        triage = _run_claude_p(
            ["claude", "-p", "--agent", "cai-audit-triage"],
            category="audit-triage",
            agent="cai-audit-triage",
            input=user_message,
        )
    finally:
        _clear_active_job()
    print(triage.stdout, flush=True)
    if triage.returncode != 0:
        print(
            f"[cai audit-triage] claude -p failed (exit {triage.returncode}):\n"
            f"{triage.stderr}",
            file=sys.stderr,
        )
        log_run("audit-triage", repo=REPO, raised=len(raised_issues),
                exit=triage.returncode)
        return triage.returncode

    # 5. Parse and execute verdicts.
    verdicts = _parse_triage_verdicts(triage.stdout)
    closed_dup = 0
    closed_res = 0
    passthrough = 0
    escalated = 0
    skipped = 0

    for v in verdicts:
        n = v["number"]
        if n not in raised_numbers:
            print(
                f"[cai audit-triage] verdict for #{n} is not in the "
                "raised set; skipping",
                flush=True,
            )
            skipped += 1
            continue

        action = v["action"]
        confidence = v["confidence"]
        reason = v["reasoning"]
        target = v["target"]

        if action == "close_duplicate":
            if confidence != "high" or target is None:
                print(
                    f"[cai audit-triage] #{n}: close_duplicate but "
                    f"confidence={confidence} target={target}; "
                    "downgrading to passthrough",
                    flush=True,
                )
                passthrough += 1
                continue
            comment = (
                "## Audit triage agent: closing as duplicate\n\n"
                f"Closing as duplicate of #{target}.\n\n"
                f"**Reasoning:** {reason}\n\n"
                "---\n"
                "_Closed automatically by `cai audit-triage`. "
                "Reopen if this assessment is wrong._"
            )
            close_res = _run(
                ["gh", "issue", "close", str(n),
                 "--repo", REPO, "--comment", comment],
                capture_output=True,
            )
            if close_res.returncode == 0:
                print(
                    f"[cai audit-triage] #{n}: closed as duplicate of #{target}",
                    flush=True,
                )
                closed_dup += 1
            else:
                print(
                    f"[cai audit-triage] #{n}: gh issue close failed:\n"
                    f"{close_res.stderr}",
                    file=sys.stderr,
                )
                skipped += 1

        elif action == "close_resolved":
            if confidence != "high":
                print(
                    f"[cai audit-triage] #{n}: close_resolved but "
                    f"confidence={confidence}; downgrading to passthrough",
                    flush=True,
                )
                passthrough += 1
                continue
            comment = (
                "## Audit triage agent: closing as resolved\n\n"
                f"**Reasoning:** {reason}\n\n"
                "---\n"
                "_Closed automatically by `cai audit-triage` because the "
                "underlying problem appears to be resolved (PR merged, "
                "state cleared, etc.). Reopen if this assessment is wrong._"
            )
            close_res = _run(
                ["gh", "issue", "close", str(n),
                 "--repo", REPO, "--comment", comment],
                capture_output=True,
            )
            if close_res.returncode == 0:
                print(
                    f"[cai audit-triage] #{n}: closed as resolved",
                    flush=True,
                )
                closed_res += 1
            else:
                print(
                    f"[cai audit-triage] #{n}: gh issue close failed:\n"
                    f"{close_res.stderr}",
                    file=sys.stderr,
                )
                skipped += 1

        elif action == "escalate":
            comment = (
                "## Audit triage agent: escalating to human\n\n"
                f"**Reasoning:** {reason}\n\n"
                "---\n"
                "_The audit triage agent could not resolve this finding "
                "autonomously. Re-labelled `audit:needs-human` for human "
                "triage._"
            )
            _run(
                ["gh", "issue", "comment", str(n),
                 "--repo", REPO, "--body", comment],
                capture_output=True,
            )
            _set_labels(
                n,
                add=[LABEL_AUDIT_NEEDS_HUMAN],
                remove=[LABEL_AUDIT_RAISED],
                log_prefix="cai audit-triage",
            )
            print(
                f"[cai audit-triage] #{n}: escalated to audit:needs-human",
                flush=True,
            )
            escalated += 1

        else:
            # passthrough — relabel to auto-improve:raised so the refine
            # subagent can structure it, then transition to :refined for
            # the fix subagent (fix no longer selects audit:raised directly,
            # ensuring all audit issues go through triage first).
            _set_labels(
                n,
                add=[LABEL_RAISED],
                remove=[LABEL_AUDIT_RAISED],
                log_prefix="cai audit-triage",
            )
            print(
                f"[cai audit-triage] #{n}: passthrough → auto-improve:raised "
                f"(action={action}, confidence={confidence})",
                flush=True,
            )
            passthrough += 1

    dur = f"{int(time.monotonic() - t0)}s"
    print(
        f"[cai audit-triage] raised={len(raised_issues)} "
        f"closed_dup={closed_dup} closed_res={closed_res} "
        f"passthrough={passthrough} escalated={escalated} skipped={skipped}",
        flush=True,
    )
    log_run(
        "audit-triage", repo=REPO,
        raised=len(raised_issues),
        closed_dup=closed_dup,
        closed_res=closed_res,
        passthrough=passthrough,
        escalated=escalated,
        skipped=skipped,
        duration=dur,
        exit=0,
    )
    return 0


# ---------------------------------------------------------------------------
# cost-report — human-readable view of /var/log/cai/cai-cost.jsonl
# ---------------------------------------------------------------------------


def cmd_cost_report(args) -> int:
    """Print a human-readable cost report from the cost log.

    Reads `/var/log/cai/cai-cost.jsonl` (written by `_run_claude_p`),
    aggregates by `--by` (category | agent | day), and prints two
    fixed-width tables: the per-group totals and the top-N most
    expensive individual invocations.

    Invoked from the host via the existing alias documented in
    README.md (`docker compose ... exec cai python /app/cai.py`):

        cai cost-report
        cai cost-report --days 30 --top 20 --by agent
    """
    rows = _load_cost_log(days=args.days)
    if not rows:
        print(
            f"[cai cost-report] no rows in {COST_LOG_PATH} for the "
            f"last {args.days} day(s)"
        )
        return 0

    # Group rows by the requested key.
    def group_key(r: dict) -> str:
        if args.by == "category":
            return r.get("category") or "(unknown)"
        if args.by == "agent":
            return r.get("agent") or "(none)"
        if args.by == "day":
            ts = r.get("ts") or ""
            return ts.split("T", 1)[0] or "(unknown)"
        return "(unknown)"

    groups: dict[str, dict] = {}
    grand_total = 0.0
    grand_in = 0
    grand_out = 0
    for r in rows:
        key = group_key(r)
        try:
            cost = float(r.get("cost_usd") or 0.0)
        except (TypeError, ValueError):
            cost = 0.0
        in_t = int(r.get("input_tokens") or 0)
        out_t = int(r.get("output_tokens") or 0)
        bucket = groups.setdefault(
            key, {"calls": 0, "cost": 0.0, "in": 0, "out": 0},
        )
        bucket["calls"] += 1
        bucket["cost"] += cost
        bucket["in"] += in_t
        bucket["out"] += out_t
        grand_total += cost
        grand_in += in_t
        grand_out += out_t

    # Header.
    print(
        f"\n=== Cost report — last {args.days} day(s), "
        f"{len(rows)} invocations, total ${grand_total:.4f} ===\n"
    )

    # Per-group totals (sorted by cost descending).
    sorted_groups = sorted(
        groups.items(), key=lambda kv: -kv[1]["cost"],
    )
    key_width = max(len(args.by), max(len(k) for k in groups) if groups else 0)
    key_width = max(key_width, 12)
    header = (
        f"{args.by:<{key_width}}  {'calls':>6}  {'cost':>10}  "
        f"{'share':>7}  {'mean':>10}  {'in_tok':>10}  {'out_tok':>10}"
    )
    print(header)
    print("-" * len(header))
    for key, b in sorted_groups:
        share = (b["cost"] / grand_total * 100.0) if grand_total else 0.0
        mean = b["cost"] / b["calls"] if b["calls"] else 0.0
        print(
            f"{key:<{key_width}}  {b['calls']:>6}  ${b['cost']:>9.4f}  "
            f"{share:>6.1f}%  ${mean:>9.4f}  {b['in']:>10}  {b['out']:>10}"
        )
    print(
        f"{'TOTAL':<{key_width}}  {len(rows):>6}  ${grand_total:>9.4f}  "
        f"{100.0:>6.1f}%  "
        f"${(grand_total / len(rows) if rows else 0):>9.4f}  "
        f"{grand_in:>10}  {grand_out:>10}"
    )

    # Top-N most expensive invocations.
    top = sorted(
        rows,
        key=lambda r: float(r.get("cost_usd") or 0.0),
        reverse=True,
    )[: args.top]
    print(f"\n--- Top {len(top)} most expensive invocations ---\n")
    top_header = (
        f"{'ts':<20}  {'category':<14}  {'agent':<20}  "
        f"{'cost':>10}  {'turns':>5}  {'in_tok':>10}  {'out_tok':>10}"
    )
    print(top_header)
    print("-" * len(top_header))
    for r in top:
        try:
            cost = float(r.get("cost_usd") or 0.0)
        except (TypeError, ValueError):
            cost = 0.0
        ts = (r.get("ts") or "")[:19]
        cat = (r.get("category") or "")[:14]
        ag = (r.get("agent") or "")[:20]
        turns = r.get("num_turns") or 0
        in_t = int(r.get("input_tokens") or 0)
        out_t = int(r.get("output_tokens") or 0)
        print(
            f"{ts:<20}  {cat:<14}  {ag:<20}  ${cost:>9.4f}  "
            f"{turns:>5}  {in_t:>10}  {out_t:>10}"
        )

    # Last-hour snapshot — cost per agent. Useful for spotting a
    # runaway subagent right now, independent of the `--days` window.
    hour_cutoff = datetime.now(timezone.utc).timestamp() - 3600
    hour_groups: dict[str, dict] = {}
    hour_total = 0.0
    hour_calls = 0
    for r in rows:
        ts = r.get("ts") or ""
        try:
            row_ts = datetime.strptime(
                ts, "%Y-%m-%dT%H:%M:%SZ",
            ).replace(tzinfo=timezone.utc).timestamp()
        except ValueError:
            continue
        if row_ts < hour_cutoff:
            continue
        try:
            cost = float(r.get("cost_usd") or 0.0)
        except (TypeError, ValueError):
            cost = 0.0
        in_t = int(r.get("input_tokens") or 0)
        out_t = int(r.get("output_tokens") or 0)
        ag = r.get("agent") or "(none)"
        bucket = hour_groups.setdefault(
            ag, {"calls": 0, "cost": 0.0, "in": 0, "out": 0},
        )
        bucket["calls"] += 1
        bucket["cost"] += cost
        bucket["in"] += in_t
        bucket["out"] += out_t
        hour_total += cost
        hour_calls += 1

    print(
        f"\n--- Last hour snapshot — {hour_calls} invocations, "
        f"total ${hour_total:.4f} ---\n"
    )
    if not hour_groups:
        print("(no invocations in the last hour)")
    else:
        hour_key_width = max(
            len("agent"), max(len(k) for k in hour_groups),
        )
        hour_key_width = max(hour_key_width, 12)
        hour_header = (
            f"{'agent':<{hour_key_width}}  {'calls':>6}  {'cost':>10}  "
            f"{'share':>7}  {'mean':>10}  {'in_tok':>10}  {'out_tok':>10}"
        )
        print(hour_header)
        print("-" * len(hour_header))
        for key, b in sorted(
            hour_groups.items(), key=lambda kv: -kv[1]["cost"],
        ):
            share = (b["cost"] / hour_total * 100.0) if hour_total else 0.0
            mean = b["cost"] / b["calls"] if b["calls"] else 0.0
            print(
                f"{key:<{hour_key_width}}  {b['calls']:>6}  "
                f"${b['cost']:>9.4f}  {share:>6.1f}%  "
                f"${mean:>9.4f}  {b['in']:>10}  {b['out']:>10}"
            )
    print()

    # Category success rates from outcome log.
    print("--- Category Success Rates (trailing 90 days) ---\n")
    cat_counts = _load_outcome_counts(days=90)
    if cat_counts:
        cat_width = max(12, max(len(k) for k in cat_counts))
        cat_header = (
            f"{'category':<{cat_width}}  {'attempts':>8}  "
            f"{'solved':>6}  {'rate':>7}  {'flag':>6}"
        )
        print(cat_header)
        print("-" * len(cat_header))
        for cat, c in sorted(cat_counts.items(), key=lambda kv: kv[1]["total"], reverse=True):
            rate = c["solved"] / c["total"] if c["total"] else 0.0
            flag = "⚠ LOW" if rate < 0.40 else ""
            print(
                f"{cat:<{cat_width}}  {c['total']:>8}  "
                f"{c['solved']:>6}  {rate:>6.0%}  {flag:>6}"
            )
    else:
        print("(no outcome data yet)")
    print()
    return 0


# ---------------------------------------------------------------------------
# code-audit — read the repo source and flag concrete inconsistencies
# ---------------------------------------------------------------------------


def _read_code_audit_memory() -> str:
    """Return the contents of the code-audit memory file, or empty string."""
    if not CODE_AUDIT_MEMORY.exists():
        return ""
    try:
        return CODE_AUDIT_MEMORY.read_text().strip()
    except OSError:
        return ""


def _save_code_audit_memory(agent_output: str) -> None:
    """Extract the ## Memory Update block from agent output and persist it.

    Each run overwrites the memory file with the latest update so the
    next run sees only the most recent state.
    """
    match = re.search(
        r"^## Memory Update\s*\n(.*)",
        agent_output,
        flags=re.MULTILINE | re.DOTALL,
    )
    if not match:
        return
    try:
        CODE_AUDIT_MEMORY.parent.mkdir(parents=True, exist_ok=True)
        CODE_AUDIT_MEMORY.write_text(match.group(0).strip() + "\n")
    except OSError as exc:
        print(f"[cai code-audit] could not write memory: {exc}", flush=True)


# ---------------------------------------------------------------------------
# cost-optimize — weekly cost-reduction proposals
# ---------------------------------------------------------------------------


def _read_cost_optimize_memory() -> str:
    """Return the contents of the cost-optimize memory file, or empty string."""
    if not COST_OPTIMIZE_MEMORY.exists():
        return ""
    try:
        return COST_OPTIMIZE_MEMORY.read_text().strip()
    except OSError:
        return ""


def _save_cost_optimize_memory(agent_output: str) -> None:
    """Extract the ## Memory Update block from agent output and persist it."""
    match = re.search(
        r"^## Memory Update\s*\n(.*)",
        agent_output,
        flags=re.MULTILINE | re.DOTALL,
    )
    if not match:
        return
    try:
        COST_OPTIMIZE_MEMORY.parent.mkdir(parents=True, exist_ok=True)
        COST_OPTIMIZE_MEMORY.write_text(match.group(0).strip() + "\n")
    except OSError as exc:
        print(f"[cai cost-optimize] could not write memory: {exc}", flush=True)


def cmd_cost_optimize(args) -> int:
    """Analyze cost data and propose one optimization or evaluate a prior proposal."""
    print("[cai cost-optimize] running weekly cost optimization", flush=True)
    t0 = time.monotonic()

    # 1. Build cost data for the agent.
    rows_14d = _load_cost_log(days=14)
    if not rows_14d:
        print("[cai cost-optimize] no cost data available; skipping", flush=True)
        dur = f"{int(time.monotonic() - t0)}s"
        log_run("cost-optimize", repo=REPO, result="no_data", duration=dur, exit=0)
        return 0

    cost_summary = _build_cost_summary(days=14, top_n=20)

    # Per-agent WoW breakdown.
    now_ts = datetime.now(timezone.utc).timestamp()
    boundary = now_ts - 7 * 86400
    last_7d = [r for r in rows_14d if _row_ts(r) >= boundary]
    prior_7d = [r for r in rows_14d if _row_ts(r) < boundary]

    def _by_agent_detailed(rows: list[dict]) -> dict[str, dict]:
        agg: dict[str, dict] = {}
        for r in rows:
            agent = r.get("agent") or "(none)"
            try:
                cost = float(r.get("cost_usd") or 0.0)
            except (TypeError, ValueError):
                cost = 0.0
            tokens_in = int(r.get("input_tokens") or 0)
            tokens_out = int(r.get("output_tokens") or 0)
            cache_read = int(r.get("cache_read_input_tokens") or 0)
            bucket = agg.setdefault(
                agent,
                {"calls": 0, "cost": 0.0, "tokens_in": 0,
                 "tokens_out": 0, "cache_read": 0},
            )
            bucket["calls"] += 1
            bucket["cost"] += cost
            bucket["tokens_in"] += tokens_in
            bucket["tokens_out"] += tokens_out
            bucket["cache_read"] += cache_read
        return agg

    last_by_agent = _by_agent_detailed(last_7d)
    prior_by_agent = _by_agent_detailed(prior_7d)
    all_agents = sorted(set(list(last_by_agent) + list(prior_by_agent)))

    agent_lines = []
    for agent in all_agents:
        lb = last_by_agent.get(
            agent, {"calls": 0, "cost": 0.0, "tokens_in": 0, "cache_read": 0}
        )
        pb = prior_by_agent.get(agent, {"calls": 0, "cost": 0.0})
        if pb["cost"] > 0:
            delta = (lb["cost"] - pb["cost"]) / pb["cost"] * 100
            delta_str = f"{delta:+.1f}%"
        else:
            delta_str = "n/a"
        cache_pct = (
            lb["cache_read"] / lb["tokens_in"] * 100
            if lb["tokens_in"] > 0 else 0.0
        )
        agent_lines.append(
            f"| {agent} | {lb['calls']} | ${lb['cost']:.4f}"
            f" | {delta_str} | {cache_pct:.1f}% |"
        )

    agent_table = (
        "### Per-agent WoW breakdown (last 7d vs prior 7d)\n\n"
        "| agent | calls (7d) | cost (7d) | WoW Δ | cache hit % |\n"
        "|---|---|---|---|---|\n"
        + "\n".join(agent_lines)
        + "\n"
    )

    # 2. Build user message.
    memory = _read_cost_optimize_memory()
    memory_section = "## Previous proposals\n\n"
    if memory:
        memory_section += memory + "\n"
    else:
        memory_section += "(first run — no prior proposals)\n"

    user_message = (
        "## Cost data\n\n"
        + cost_summary + "\n\n"
        + agent_table + "\n\n"
        + memory_section
    )

    # 3. Run the cost-optimize agent.
    print("[cai cost-optimize] running agent", flush=True)
    _write_active_job("cost-optimize", "none", None)
    try:
        result = _run_claude_p(
            ["claude", "-p", "--agent", "cai-cost-optimize",
             "--permission-mode", "acceptEdits"],
            category="cost-optimize",
            agent="cai-cost-optimize",
            input=user_message,
            cwd="/app",
        )
    finally:
        _clear_active_job()
    if result.stdout:
        print(result.stdout, flush=True)
    if result.returncode != 0:
        print(
            f"[cai cost-optimize] agent failed (exit {result.returncode}):\n"
            f"{result.stderr}",
            file=sys.stderr, flush=True,
        )
        dur = f"{int(time.monotonic() - t0)}s"
        log_run("cost-optimize", repo=REPO, result="agent_failed",
                duration=dur, exit=result.returncode)
        return result.returncode

    # 4. Save memory.
    _save_cost_optimize_memory(result.stdout)

    # 5. Handle proposal output.
    proposal_match = re.search(
        r"^### Proposal:\s*(.+)$", result.stdout, flags=re.MULTILINE
    )
    if proposal_match:
        proposal_title = proposal_match.group(1).strip()

        # Extract key for dedup.
        key_match = re.search(r"\*\*Key:\*\*\s*(\S+)", result.stdout)
        proposal_key = key_match.group(1).strip() if key_match else uuid.uuid4().hex[:8]

        # Extract proposal block.
        block_match = re.search(
            r"(### Proposal:.*?)(?=\n## Memory Update|\Z)",
            result.stdout, flags=re.DOTALL,
        )
        proposal_body = (
            block_match.group(1).strip() if block_match else result.stdout.strip()
        )

        # Dedup check.
        fingerprint = f"<!-- fingerprint: cost-optimize-{proposal_key} -->"
        dup_check = _run(
            ["gh", "issue", "list",
             "--repo", REPO,
             "--search", f"cost-optimize-{proposal_key} in:body",
             "--state", "all",
             "--limit", "1",
             "--json", "number"],
            capture_output=True,
        )
        if (
            dup_check.returncode == 0
            and dup_check.stdout.strip() not in ("", "[]")
        ):
            print(
                f"[cai cost-optimize] duplicate for key {proposal_key}; skipping",
                flush=True,
            )
            dur = f"{int(time.monotonic() - t0)}s"
            log_run("cost-optimize", repo=REPO, result="duplicate",
                    duration=dur, exit=0)
            return 0

        # Create issue.
        issue_body = (
            f"{proposal_body}\n\n"
            "---\n"
            "_Proposed by the weekly cost-optimization agent"
            " (`cai cost-optimize`)._\n\n"
            f"{fingerprint}\n"
        )
        labels = ",".join(["auto-improve", LABEL_RAISED])
        gh_result = _run(
            ["gh", "issue", "create",
             "--repo", REPO,
             "--title", f"[cost] {proposal_title}",
             "--body", issue_body,
             "--label", labels],
            capture_output=True,
        )
        if gh_result.returncode == 0:
            url = gh_result.stdout.strip()
            print(f"[cai cost-optimize] created proposal issue: {url}", flush=True)
        else:
            print(
                f"[cai cost-optimize] failed to create issue: {gh_result.stderr}",
                file=sys.stderr, flush=True,
            )
        dur = f"{int(time.monotonic() - t0)}s"
        log_run("cost-optimize", repo=REPO, result="proposal_created",
                duration=dur, exit=gh_result.returncode)
        return gh_result.returncode

    # 6. Handle evaluation output.
    eval_match = re.search(
        r"^### Evaluation:\s*(.+)$", result.stdout, flags=re.MULTILINE
    )
    if eval_match:
        print(
            f"[cai cost-optimize] evaluation: {eval_match.group(1).strip()}",
            flush=True,
        )
        dur = f"{int(time.monotonic() - t0)}s"
        log_run("cost-optimize", repo=REPO, result="evaluation_done",
                duration=dur, exit=0)
        return 0

    # 7. No recognized output.
    print("[cai cost-optimize] no proposal or evaluation found in output", flush=True)
    dur = f"{int(time.monotonic() - t0)}s"
    log_run("cost-optimize", repo=REPO, result="no_output", duration=dur, exit=0)
    return 0


# ---------------------------------------------------------------------------
# propose — creative improvement proposals
# ---------------------------------------------------------------------------


def _read_propose_memory() -> str:
    """Return the contents of the propose memory file, or empty string."""
    if not PROPOSE_MEMORY.exists():
        return ""
    try:
        return PROPOSE_MEMORY.read_text().strip()
    except OSError:
        return ""


def _save_propose_memory(agent_output: str) -> None:
    """Extract the ## Memory Update block from agent output and persist it."""
    match = re.search(
        r"^## Memory Update\s*\n(.*)",
        agent_output,
        flags=re.MULTILINE | re.DOTALL,
    )
    if not match:
        return
    try:
        PROPOSE_MEMORY.parent.mkdir(parents=True, exist_ok=True)
        PROPOSE_MEMORY.write_text(match.group(0).strip() + "\n")
    except OSError as exc:
        print(f"[cai propose] could not write memory: {exc}", flush=True)


def cmd_propose(args) -> int:
    """Clone the repo and run the creative + review agents to propose improvements."""
    print("[cai propose] running creative improvement proposal", flush=True)
    t0 = time.monotonic()

    # 1. Clone repo into a temporary directory (read-only).
    _uid = uuid.uuid4().hex[:8]
    work_dir = Path(f"/tmp/cai-propose-{_uid}")

    if work_dir.exists():
        shutil.rmtree(work_dir)

    clone = _run(
        ["git", "clone", "--depth", "1",
         f"https://github.com/{REPO}.git", str(work_dir)],
        capture_output=True,
    )
    if clone.returncode != 0:
        print(
            f"[cai propose] git clone failed:\n{clone.stderr}",
            file=sys.stderr, flush=True,
        )
        dur = f"{int(time.monotonic() - t0)}s"
        log_run("propose", repo=REPO, result="clone_failed",
                duration=dur, exit=1)
        return 1

    # 2. Build user message for the creative agent.
    memory = _read_propose_memory()
    memory_section = "## Memory from previous runs\n\n"
    if memory:
        memory_section += memory + "\n"
    else:
        memory_section += "(first run — no prior memory)\n"

    user_message = _work_directory_block(work_dir) + "\n" + memory_section

    # 3. Run the creative proposal agent.
    print(f"[cai propose] running creative agent for {work_dir}", flush=True)
    _write_active_job("propose", "none", None)
    try:
        creative = _run_claude_p(
            ["claude", "-p", "--agent", "cai-propose",
             "--permission-mode", "acceptEdits",
             "--add-dir", str(work_dir)],
            category="propose",
            agent="cai-propose",
            input=user_message,
            cwd="/app",
        )
    finally:
        _clear_active_job()
    if creative.stdout:
        print(creative.stdout, flush=True)
    if creative.returncode != 0:
        print(
            f"[cai propose] creative agent failed (exit {creative.returncode}):\n"
            f"{creative.stderr}",
            file=sys.stderr, flush=True,
        )
        shutil.rmtree(work_dir, ignore_errors=True)
        dur = f"{int(time.monotonic() - t0)}s"
        log_run("propose", repo=REPO, result="creative_agent_failed",
                duration=dur, exit=creative.returncode)
        return creative.returncode

    # 4. Save the creative agent's memory for next run.
    _save_propose_memory(creative.stdout)

    # 5. Check if the creative agent had nothing to propose.
    if "No proposal." in creative.stdout:
        print("[cai propose] creative agent had no proposal; done", flush=True)
        shutil.rmtree(work_dir, ignore_errors=True)
        dur = f"{int(time.monotonic() - t0)}s"
        log_run("propose", repo=REPO, result="no_proposal", duration=dur, exit=0)
        return 0

    # 6. Extract the proposal title and text.
    title_match = re.search(
        r"^### Proposal:\s*(.+)$", creative.stdout, flags=re.MULTILINE
    )
    proposal_title = title_match.group(1).strip() if title_match else "Improvement proposal"

    # Extract proposal block (everything from ### Proposal: to ## Memory Update or end).
    proposal_match = re.search(
        r"(### Proposal:.*?)(?=\n## Memory Update|\Z)",
        creative.stdout,
        flags=re.DOTALL,
    )
    proposal_text = proposal_match.group(1).strip() if proposal_match else creative.stdout.strip()

    # Extract the key for deduplication.
    key_match = re.search(
        r"\*\*Key:\*\*\s*(\S+)", creative.stdout
    )
    proposal_key = key_match.group(1).strip() if key_match else _uid

    # 7. Run the review agent with the proposal.
    review_message = (
        _work_directory_block(work_dir) + "\n"
        "## Proposal\n\n" + proposal_text + "\n"
    )

    print("[cai propose] running review agent", flush=True)
    _write_active_job("propose-review", "none", None)
    try:
        review = _run_claude_p(
            ["claude", "-p", "--agent", "cai-propose-review",
             "--permission-mode", "acceptEdits",
             "--add-dir", str(work_dir)],
            category="propose",
            agent="cai-propose-review",
            input=review_message,
            cwd="/app",
        )
    finally:
        _clear_active_job()
    if review.stdout:
        print(review.stdout, flush=True)
    if review.returncode != 0:
        print(
            f"[cai propose] review agent failed (exit {review.returncode}):\n"
            f"{review.stderr}",
            file=sys.stderr, flush=True,
        )
        shutil.rmtree(work_dir, ignore_errors=True)
        dur = f"{int(time.monotonic() - t0)}s"
        log_run("propose", repo=REPO, result="review_agent_failed",
                duration=dur, exit=review.returncode)
        return review.returncode

    # 8. Check the verdict.
    if "### Verdict: reject" in review.stdout:
        print("[cai propose] proposal rejected by review agent; done", flush=True)
        shutil.rmtree(work_dir, ignore_errors=True)
        dur = f"{int(time.monotonic() - t0)}s"
        log_run("propose", repo=REPO, result="rejected", duration=dur, exit=0)
        return 0

    # 9. Extract the refined issue body.
    refined_match = re.search(
        r"## Refined Issue\s*\n(.*)",
        review.stdout,
        flags=re.DOTALL,
    )
    if not refined_match:
        print(
            "[cai propose] review agent approved but no ## Refined Issue found; "
            "skipping issue creation",
            file=sys.stderr, flush=True,
        )
        shutil.rmtree(work_dir, ignore_errors=True)
        dur = f"{int(time.monotonic() - t0)}s"
        log_run("propose", repo=REPO, result="no_refined_issue", duration=dur, exit=0)
        return 0

    refined_body = refined_match.group(1).strip()

    # 10. Check for duplicates before creating the issue.
    fingerprint = f"<!-- fingerprint: propose-{proposal_key} -->"
    dup_check = _run(
        ["gh", "issue", "list",
         "--repo", REPO,
         "--search", f"propose-{proposal_key} in:body",
         "--state", "all",
         "--limit", "1",
         "--json", "number"],
        capture_output=True,
    )
    if dup_check.returncode == 0 and dup_check.stdout.strip() not in ("", "[]"):
        print(
            f"[cai propose] duplicate found for key {proposal_key}; skipping",
            flush=True,
        )
        shutil.rmtree(work_dir, ignore_errors=True)
        dur = f"{int(time.monotonic() - t0)}s"
        log_run("propose", repo=REPO, result="duplicate", duration=dur, exit=0)
        return 0

    # 11. Create the GitHub issue.
    issue_body = (
        f"{refined_body}\n\n"
        f"---\n"
        f"_Proposed by the weekly creative improvement agent "
        f"(`cai propose`)._\n\n"
        f"{fingerprint}\n"
    )
    labels = ",".join(["auto-improve", LABEL_RAISED])
    result = _run(
        ["gh", "issue", "create",
         "--repo", REPO,
         "--title", proposal_title,
         "--body", issue_body,
         "--label", labels],
        capture_output=True,
    )
    if result.returncode == 0:
        url = result.stdout.strip()
        print(f"[cai propose] created proposal issue: {url}", flush=True)
    else:
        print(
            f"[cai propose] failed to create issue: {result.stderr}",
            file=sys.stderr, flush=True,
        )

    # 12. Clean up.
    shutil.rmtree(work_dir, ignore_errors=True)

    dur = f"{int(time.monotonic() - t0)}s"
    log_run("propose", repo=REPO, duration=dur, exit=result.returncode)
    return result.returncode


def _read_update_check_memory() -> str:
    """Return the contents of the update-check memory file, or empty string."""
    if not UPDATE_CHECK_MEMORY.exists():
        return ""
    try:
        return UPDATE_CHECK_MEMORY.read_text().strip()
    except OSError:
        return ""


def _save_update_check_memory(agent_output: str) -> None:
    """Extract the ## Memory Update block from agent output and persist it.

    Each run overwrites the memory file with the latest update so the
    next run sees only the most recent state.
    """
    match = re.search(
        r"^## Memory Update\s*\n(.*)",
        agent_output,
        flags=re.MULTILINE | re.DOTALL,
    )
    if not match:
        return
    try:
        UPDATE_CHECK_MEMORY.parent.mkdir(parents=True, exist_ok=True)
        UPDATE_CHECK_MEMORY.write_text(match.group(0).strip() + "\n")
    except OSError as exc:
        print(f"[cai update-check] could not write memory: {exc}", flush=True)


def cmd_code_audit(args) -> int:
    """Clone the repo and run the code-audit agent to find inconsistencies."""
    print("[cai code-audit] running code audit", flush=True)
    t0 = time.monotonic()

    # 1. Clone repo into a temporary directory (read-only audit).
    _uid = uuid.uuid4().hex[:8]
    work_dir = Path(f"/tmp/cai-code-audit-{_uid}")

    if work_dir.exists():
        shutil.rmtree(work_dir)

    clone = _run(
        ["git", "clone", "--depth", "1",
         f"https://github.com/{REPO}.git", str(work_dir)],
        capture_output=True,
    )
    if clone.returncode != 0:
        print(
            f"[cai code-audit] git clone failed:\n{clone.stderr}",
            file=sys.stderr, flush=True,
        )
        dur = f"{int(time.monotonic() - t0)}s"
        log_run("code-audit", repo=REPO, result="clone_failed",
                duration=dur, exit=1)
        return 1

    # 2. Build the user message with the runtime memory from the
    #    named-volume log directory (cai_logs). System prompt, tool allowlist
    #    (Read/Grep/Glob), and model (sonnet) all live in
    #    `.claude/agents/cai-code-audit.md`. Durable per-agent
    #    learnings live in its `memory: project` pool, which the
    #    agent reads directly from /app/.claude/agent-memory/cai-code-audit/
    #    (the cai_agent_memory volume) — no copy in/out (#342).
    memory = _read_code_audit_memory()

    memory_section = "## Memory from previous runs\n\n"
    if memory:
        memory_section += memory + "\n"
    else:
        memory_section += "(first run — no prior memory)\n"

    user_message = _work_directory_block(work_dir) + "\n" + memory_section

    # 3. Invoke the declared cai-code-audit subagent.
    #    Runs with `cwd=/app` and `--add-dir <work_dir>` (#342) so
    #    the agent reads its definition + memory from the canonical
    #    /app paths while auditing the clone via absolute paths.
    print(f"[cai code-audit] running agent for {work_dir}", flush=True)
    _write_active_job("code-audit", "none", None)
    try:
        agent = _run_claude_p(
            ["claude", "-p", "--agent", "cai-code-audit",
             "--permission-mode", "acceptEdits",
             "--add-dir", str(work_dir)],
            category="code-audit",
            agent="cai-code-audit",
            input=user_message,
            cwd="/app",
        )
    finally:
        _clear_active_job()
    if agent.stdout:
        print(agent.stdout, flush=True)
    if agent.returncode != 0:
        print(
            f"[cai code-audit] claude -p failed (exit {agent.returncode}):\n"
            f"{agent.stderr}",
            file=sys.stderr, flush=True,
        )
        shutil.rmtree(work_dir, ignore_errors=True)
        dur = f"{int(time.monotonic() - t0)}s"
        log_run("code-audit", repo=REPO, result="agent_failed",
                duration=dur, exit=agent.returncode)
        return agent.returncode

    # 4. Save the memory update for next run (runtime rotation
    #    state in /var/log/cai/code-audit-memory.md).
    _save_code_audit_memory(agent.stdout)

    # 5. Publish findings via publish.py with code-audit namespace.
    print("[cai code-audit] publishing findings", flush=True)
    published = _run(
        ["python", str(PUBLISH_SCRIPT), "--namespace", "code-audit"],
        input=agent.stdout,
    )

    # 6. Clean up.
    shutil.rmtree(work_dir, ignore_errors=True)

    dur = f"{int(time.monotonic() - t0)}s"
    log_run("code-audit", repo=REPO, duration=dur, exit=published.returncode)
    return published.returncode


# ---------------------------------------------------------------------------
# update-check
# ---------------------------------------------------------------------------


def cmd_update_check(args) -> int:
    """Clone the repo and check Claude Code releases for workspace improvements."""
    print("[cai update-check] checking for updates", flush=True)
    t0 = time.monotonic()

    # 1. Clone repo into a temporary directory.
    _uid = uuid.uuid4().hex[:8]
    work_dir = Path(f"/tmp/cai-update-check-{_uid}")

    if work_dir.exists():
        shutil.rmtree(work_dir)

    clone = _run(
        ["git", "clone", "--depth", "1",
         f"https://github.com/{REPO}.git", str(work_dir)],
        capture_output=True,
    )
    if clone.returncode != 0:
        print(
            f"[cai update-check] git clone failed:\n{clone.stderr}",
            file=sys.stderr, flush=True,
        )
        dur = f"{int(time.monotonic() - t0)}s"
        log_run("update-check", repo=REPO, result="clone_failed",
                duration=dur, exit=1)
        return 1

    # 2. Read current pinned version from Dockerfile.
    try:
        dockerfile = (work_dir / "Dockerfile").read_text()
        version_match = re.search(
            r"ARG\s+CLAUDE_CODE_VERSION=(\S+)", dockerfile
        )
        current_version = version_match.group(1) if version_match else "unknown"
    except OSError:
        current_version = "unknown"

    # 3. Fetch latest releases from GitHub API.
    releases_result = _run(
        ["gh", "api", "repos/anthropics/claude-code/releases",
         "--jq", ".[:5]"],
        capture_output=True,
    )
    if releases_result.returncode != 0:
        print(
            f"[cai update-check] failed to fetch releases:\n{releases_result.stderr}",
            file=sys.stderr, flush=True,
        )
        shutil.rmtree(work_dir, ignore_errors=True)
        dur = f"{int(time.monotonic() - t0)}s"
        log_run("update-check", repo=REPO, result="api_failed",
                duration=dur, exit=1)
        return 1

    releases_json = releases_result.stdout if releases_result.stdout else "[]"

    # 4. Read workspace settings.
    try:
        settings = (work_dir / ".claude" / "settings.json").read_text()
    except OSError:
        settings = "{}"

    # 5. Build the user message. The system prompt, tool allowlist, and
    #    model all live in `.claude/agents/cai-update-check.md`. Durable
    #    per-agent learnings live in its `memory: project` pool.
    memory = _read_update_check_memory()

    memory_section = "## Memory from previous runs\n\n"
    if memory:
        memory_section += memory + "\n"
    else:
        memory_section += "(first run — no prior memory)\n"

    user_message = (
        _work_directory_block(work_dir) + "\n"
        + f"## Current pinned version\n\n`{current_version}`\n\n"
        + f"## Latest Claude Code releases\n\n```json\n{releases_json}\n```\n\n"
        + f"## Current workspace settings\n\n```json\n{settings}\n```\n\n"
        + memory_section
    )

    # 6. Invoke the declared cai-update-check subagent.
    #    Runs with `cwd=/app` and `--add-dir <work_dir>` so the agent
    #    reads its definition + memory from the canonical /app paths
    #    while examining the clone via absolute paths.
    print(f"[cai update-check] running agent for {work_dir}", flush=True)
    _write_active_job("update-check", "none", None)
    try:
        agent = _run_claude_p(
            ["claude", "-p", "--agent", "cai-update-check",
             "--permission-mode", "acceptEdits",
             "--add-dir", str(work_dir)],
            category="update-check",
            agent="cai-update-check",
            input=user_message,
            cwd="/app",
        )
    finally:
        _clear_active_job()
    if agent.stdout:
        print(agent.stdout, flush=True)
    if agent.returncode != 0:
        print(
            f"[cai update-check] claude -p failed (exit {agent.returncode}):\n"
            f"{agent.stderr}",
            file=sys.stderr, flush=True,
        )
        shutil.rmtree(work_dir, ignore_errors=True)
        dur = f"{int(time.monotonic() - t0)}s"
        log_run("update-check", repo=REPO, result="agent_failed",
                duration=dur, exit=agent.returncode)
        return agent.returncode

    # 7. Save the memory update for next run.
    _save_update_check_memory(agent.stdout)

    # 8. Publish findings via publish.py with update-check namespace.
    print("[cai update-check] publishing findings", flush=True)
    published = _run(
        ["python", str(PUBLISH_SCRIPT), "--namespace", "update-check"],
        input=agent.stdout,
    )

    # 9. Clean up.
    shutil.rmtree(work_dir, ignore_errors=True)

    dur = f"{int(time.monotonic() - t0)}s"
    log_run("update-check", repo=REPO, duration=dur, exit=published.returncode)
    return published.returncode


# ---------------------------------------------------------------------------
# confirm
# ---------------------------------------------------------------------------


def _parse_verdicts(text: str) -> list[tuple[int, str, str]]:
    """Parse `### Verdict: #N` blocks. Returns (issue_num, status, reasoning)."""
    verdicts = []
    blocks = re.split(r"^### Verdict:\s*", text, flags=re.MULTILINE)
    for block in blocks[1:]:
        lines = block.strip().splitlines()
        if not lines:
            continue
        # Header line: "#N — title"
        header_match = re.match(r"#(\d+)", lines[0])
        if not header_match:
            continue
        issue_num = int(header_match.group(1))
        body = "\n".join(lines[1:])
        status_match = re.search(
            r"^- \*\*Status:\*\*\s*(.+)$", body, flags=re.MULTILINE
        )
        reasoning_match = re.search(
            r"^- \*\*Reasoning:\*\*\s*(.+)$", body, flags=re.MULTILINE
        )
        status = status_match.group(1).strip().strip("`").lower() if status_match else ""
        reasoning = reasoning_match.group(1).strip() if reasoning_match else ""
        if status in ("solved", "unsolved", "inconclusive"):
            verdicts.append((issue_num, status, reasoning))
    return verdicts


def cmd_confirm(args) -> int:
    """Re-analyze the recent window to verify :merged issues are solved.

    For unsolved issues, logs the outcome and either re-queues to
    :refined (up to 3 attempts) or escalates to :needs-human-review
    after max attempts.
    """
    print("[cai confirm] checking merged issues against recent signals", flush=True)
    t0 = time.monotonic()

    # 1. Query open :merged issues.
    if getattr(args, "issue", None) is not None:
        # Direct targeting: look up the specified issue.
        try:
            target_issue = _gh_json([
                "issue", "view", str(args.issue),
                "--repo", REPO,
                "--json", "number,title,body,labels",
            ])
        except subprocess.CalledProcessError as e:
            print(f"[cai confirm] gh issue view #{args.issue} failed:\n{e.stderr}", file=sys.stderr)
            log_run("confirm", repo=REPO, merged_checked=0, solved=0,
                    unsolved=0, inconclusive=0, exit=1)
            return 1
        merged_issues = [target_issue]
    else:
        try:
            merged_issues = _gh_json([
                "issue", "list", "--repo", REPO,
                "--label", LABEL_MERGED,
                "--state", "open",
                "--json", "number,title,body,labels",
                "--limit", "100",
            ]) or []
        except subprocess.CalledProcessError as e:
            print(f"[cai confirm] gh issue list failed:\n{e.stderr}", file=sys.stderr)
            log_run("confirm", repo=REPO, merged_checked=0, solved=0,
                    unsolved=0, inconclusive=0, exit=1)
            return 1

    if not merged_issues:
        print("[cai confirm] no merged issues; nothing to do", flush=True)
        log_run("confirm", repo=REPO, merged_checked=0, solved=0,
                unsolved=0, inconclusive=0, exit=0)
        return 0

    print(f"[cai confirm] found {len(merged_issues)} merged issue(s)", flush=True)

    # 2. Run parse.py against the transcript dir (global window settings).
    parsed = _run(
        ["python", str(PARSE_SCRIPT), str(TRANSCRIPT_DIR)],
        capture_output=True,
    )
    if parsed.returncode != 0:
        print(
            f"[cai confirm] parse.py failed (exit {parsed.returncode}):\n{parsed.stderr}",
            flush=True,
        )
        dur = f"{int(time.monotonic() - t0)}s"
        log_run("confirm", repo=REPO, duration=dur, exit=parsed.returncode)
        return parsed.returncode

    parsed_signals = parsed.stdout.strip()

    # Extract stats from parse.py's JSON output.
    try:
        signals = json.loads(parsed_signals)
    except (json.JSONDecodeError, ValueError):
        signals = {}
    token_usage = signals.get("token_usage", {})
    in_tokens = token_usage.get("input_tokens", 0)
    out_tokens = token_usage.get("output_tokens", 0)
    session_count = signals.get("session_count", 0)

    if in_tokens > 0 and in_tokens < 500:
        print(
            f"[cai confirm] WARNING: in_tokens={in_tokens} is below the "
            f"expected floor of 500 — the transcript window may be too "
            f"narrow or session files may be nearly empty",
            flush=True,
        )

    # 2b. For each merged issue, fetch the associated merged PR diff.
    MAX_DIFF_LEN = 8000
    for mi in merged_issues:
        num = mi["number"]
        try:
            prs = _gh_json([
                "pr", "list", "--repo", REPO,
                "--search", f'"Refs {REPO}#{num}" in:body',
                "--state", "merged",
                "--json", "number",
                "--limit", "1",
            ]) or []
        except subprocess.CalledProcessError:
            prs = []
        if prs:
            pr_num = prs[0]["number"]
            diff_result = _run(
                ["gh", "pr", "diff", str(pr_num), "--repo", REPO],
                capture_output=True,
            )
            if diff_result.returncode == 0 and diff_result.stdout.strip():
                diff_text = diff_result.stdout.strip()
                if len(diff_text) > MAX_DIFF_LEN:
                    diff_text = diff_text[:MAX_DIFF_LEN] + "\n... (truncated)"
                mi["_pr_diff"] = diff_text
                mi["_pr_number"] = pr_num

    # 3. Build the user message (parsed signals + merged issues +
    #    PR diffs). The system prompt, tool allowlist, and model
    #    choice all live in `.claude/agents/cai-confirm.md` — the
    #    wrapper only passes dynamic per-run context via stdin.
    issues_section = "## Merged issues to verify\n\n"
    for mi in merged_issues:
        issues_section += (
            f"### #{mi['number']} — {mi['title']}\n\n"
            f"{mi.get('body') or '(no body)'}\n\n"
        )
        if mi.get("_pr_diff"):
            issues_section += (
                f"#### Merged PR diff (PR #{mi['_pr_number']})\n\n"
                f"```diff\n{mi['_pr_diff']}\n```\n\n"
            )

    user_message = (
        "## Parsed signals\n\n"
        "```json\n"
        f"{parsed_signals}\n"
        "```\n\n"
        f"{issues_section}"
    )

    # 4. Invoke the declared cai-confirm subagent.
    _write_active_job("confirm", "none", None)
    try:
        confirm = _run_claude_p(
            ["claude", "-p", "--agent", "cai-confirm"],
            category="confirm",
            agent="cai-confirm",
            input=user_message,
        )
    finally:
        _clear_active_job()
    if confirm.returncode != 0:
        print(
            f"[cai confirm] claude -p failed (exit {confirm.returncode}):\n"
            f"{confirm.stderr}",
            flush=True,
        )
        dur = f"{int(time.monotonic() - t0)}s"
        log_run("confirm", repo=REPO, merged_checked=len(merged_issues),
                solved=0, unsolved=0, inconclusive=0,
                sessions=session_count, in_tokens=in_tokens, out_tokens=out_tokens,
                duration=dur, exit=confirm.returncode)
        return confirm.returncode

    # 5. Parse verdicts.
    verdicts = _parse_verdicts(confirm.stdout)
    merged_nums = {mi["number"] for mi in merged_issues}
    merged_by_num = {mi["number"]: mi for mi in merged_issues}

    solved = 0
    unsolved = 0
    inconclusive = 0

    for issue_num, status, reasoning in verdicts:
        if issue_num not in merged_nums:
            continue
        mi = merged_by_num[issue_num]
        if status == "solved":
            cat = _get_issue_category(mi)
            prior_attempts = len(_fetch_previous_fix_attempts(issue_num))
            _log_outcome(issue_num, cat, "solved", prior_attempts)
            _set_labels(issue_num, add=[LABEL_SOLVED], remove=[LABEL_MERGED], log_prefix="cai confirm")
            _run(
                ["gh", "issue", "close", str(issue_num),
                 "--repo", REPO,
                 "--comment",
                 f"Confirmed solved: {reasoning}"],
                capture_output=True,
            )
            print(f"[cai confirm] #{issue_num}: solved — closed", flush=True)
            # Update parent checklist if this is a sub-issue.
            sub_body = mi.get("body") or ""
            parent_match = re.search(r"<!-- parent: #(\d+) -->", sub_body)
            if parent_match:
                _update_parent_checklist_item(
                    int(parent_match.group(1)), issue_num, checked=True,
                )
            solved += 1
        elif status == "unsolved":
            cat = _get_issue_category(mi)
            prior_attempts = len(_fetch_previous_fix_attempts(issue_num))
            _log_outcome(issue_num, cat, "unsolved", prior_attempts)

            # Parse re-queue count from issue body (use findall to handle
            # multiple appended blocks; take the last match's count).
            issue_body = mi.get("body") or ""
            requeue_matches = re.findall(
                r"## Confirm re-queue \(attempt (\d+)\)", issue_body
            )
            requeue_count = int(requeue_matches[-1]) if requeue_matches else 0

            if requeue_count < 3:
                new_count = requeue_count + 1
                requeue_block = (
                    f"\n\n## Confirm re-queue (attempt {new_count})\n\n"
                    f"Fix confirmed unsolved. Re-queued for another attempt."
                )
                _run(
                    ["gh", "issue", "edit", str(issue_num),
                     "--repo", REPO,
                     "--body", issue_body + requeue_block],
                    capture_output=True,
                )
                _set_labels(
                    issue_num,
                    add=[LABEL_REFINED],
                    remove=[LABEL_MERGED],
                    log_prefix="cai confirm",
                )
                _run(
                    ["gh", "issue", "comment", str(issue_num),
                     "--repo", REPO,
                     "--body",
                     f"Confirm check: fix did not resolve the issue. "
                     f"Re-queued as `auto-improve:refined` (attempt {new_count}/3)."],
                    capture_output=True,
                )
                print(f"[cai confirm] #{issue_num}: unsolved — re-queued (attempt {new_count}/3)", flush=True)
            else:
                # Cap reached — escalate to human.
                _set_labels(
                    issue_num,
                    add=[LABEL_PR_NEEDS_HUMAN],
                    remove=[LABEL_MERGED],
                    log_prefix="cai confirm",
                )
                _run(
                    ["gh", "issue", "comment", str(issue_num),
                     "--repo", REPO,
                     "--body",
                     "Confirm check: fix did not resolve the issue after 3 re-queue attempts. "
                     "Escalating to `needs-human-review`."],
                    capture_output=True,
                )
                print(f"[cai confirm] #{issue_num}: unsolved — max re-queues reached, needs-human", flush=True)
            unsolved += 1
        elif status == "inconclusive":
            cat = _get_issue_category(mi)
            prior_attempts = len(_fetch_previous_fix_attempts(issue_num))
            _log_outcome(issue_num, cat, "inconclusive", prior_attempts)
            # Post reasoning to the issue so humans can see why, but
            # avoid duplicate comments if the same reasoning was already
            # posted in the most recent comment.
            body = f"Confirm check: inconclusive — {reasoning}"
            try:
                comments = _gh_json([
                    "issue", "view", str(issue_num),
                    "--repo", REPO,
                    "--json", "comments",
                ]) or {}
                last_body = ""
                clist = comments.get("comments") or []
                if clist:
                    last_body = (clist[-1].get("body") or "").strip()
            except subprocess.CalledProcessError:
                last_body = ""
            if last_body != body.strip():
                _run(
                    ["gh", "issue", "comment", str(issue_num),
                     "--repo", REPO,
                     "--body", body],
                    capture_output=True,
                )
            print(f"[cai confirm] #{issue_num}: inconclusive — {reasoning}", flush=True)
            inconclusive += 1

    dur = f"{int(time.monotonic() - t0)}s"
    print(
        f"[cai confirm] merged_checked={len(merged_issues)} "
        f"solved={solved} unsolved={unsolved} inconclusive={inconclusive} "
        f"sessions={session_count} in_tokens={in_tokens} out_tokens={out_tokens}",
        flush=True,
    )
    log_run("confirm", repo=REPO, merged_checked=len(merged_issues),
            solved=solved, unsolved=unsolved, inconclusive=inconclusive,
            sessions=session_count, in_tokens=in_tokens, out_tokens=out_tokens,
            duration=dur, exit=0)
    return 0


# ---------------------------------------------------------------------------
# review-pr
# ---------------------------------------------------------------------------


def _log_review_pr_findings(pr_number: int, head_sha: str, agent_output: str) -> None:
    """Append one JSON line recording the finding categories for a PR review.

    Silently no-ops on any I/O error so logging failures never break the
    review workflow.
    """
    try:
        categories = re.findall(r"### Finding:\s*(\w+)", agent_output)
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        entry = {
            "ts": ts,
            "pr": pr_number,
            "sha": head_sha[:8],
            "categories": categories,
        }
        REVIEW_PR_PATTERN_LOG.parent.mkdir(parents=True, exist_ok=True)
        with REVIEW_PR_PATTERN_LOG.open("a") as fh:
            fh.write(json.dumps(entry) + "\n")
            fh.flush()
    except Exception:  # noqa: BLE001
        pass


# review-pr posts two comment variants depending on whether the
# review found ripple effects:
#
#   * `_REVIEW_COMMENT_HEADING_FINDINGS` — actionable, contains
#     `### Finding:` blocks. NOT in `_BOT_COMMENT_MARKERS` so the
#     revise subagent picks them up and addresses them.
#
#   * `_REVIEW_COMMENT_HEADING_CLEAN` — informational only, says
#     "no ripple effects found". IS in `_BOT_COMMENT_MARKERS` so the
#     revise subagent skips it (no actionable content).
#
# Both forms include the head SHA at the end of the heading line so
# the SHA-idempotency check can recognize either as "already
# reviewed at this commit".
_REVIEW_COMMENT_HEADING_FINDINGS = "## cai pre-merge review"
_REVIEW_COMMENT_HEADING_CLEAN = "## cai pre-merge review (clean)"


def cmd_review_pr(args) -> int:
    """Review open PRs for ripple effects and post findings as PR comments."""
    print("[cai review-pr] checking open PRs against main", flush=True)
    t0 = time.monotonic()

    if getattr(args, "pr", None) is not None:
        # Direct targeting: look up the specified PR.
        try:
            target_pr = _gh_json([
                "pr", "view", str(args.pr),
                "--repo", REPO,
                "--json", "number,title,author,headRefOid,comments",
            ])
        except subprocess.CalledProcessError as e:
            print(f"[cai review-pr] gh pr view #{args.pr} failed:\n{e.stderr}", file=sys.stderr)
            log_run("review_pr", repo=REPO, result="pr_lookup_failed", exit=1)
            return 1
        prs = [target_pr]
    else:
        try:
            prs = _gh_json([
                "pr", "list",
                "--repo", REPO,
                "--state", "open",
                "--base", "main",
                "--json", "number,title,author,headRefOid,comments",
                "--limit", "50",
            ]) or []
        except subprocess.CalledProcessError as e:
            print(f"[cai review-pr] gh pr list failed:\n{e.stderr}", file=sys.stderr)
            log_run("review_pr", repo=REPO, result="pr_list_failed", exit=1)
            return 1

    if not prs:
        print("[cai review-pr] no open PRs; nothing to do", flush=True)
        log_run("review_pr", repo=REPO, result="no_open_prs", exit=0)
        return 0

    reviewed = 0
    skipped = 0

    for pr in prs:
        pr_number = pr["number"]
        head_sha = pr["headRefOid"]
        title = pr["title"]

        # Check if we already posted a review for this SHA. Match
        # either heading variant (findings or clean) — both include
        # the head SHA after the em-dash, so a substring check on
        # `head_sha` against the comment's first line is enough.
        already_reviewed = False
        for comment in pr.get("comments", []):
            body = (comment.get("body") or "")
            first_line = body.split("\n", 1)[0]
            if (
                first_line.startswith(_REVIEW_COMMENT_HEADING_FINDINGS)
                and head_sha in first_line
            ):
                already_reviewed = True
                break
        if already_reviewed:
            print(
                f"[cai review-pr] PR #{pr_number}: already reviewed at {head_sha[:8]}; skipping",
                flush=True,
            )
            skipped += 1
            continue

        print(f"[cai review-pr] reviewing PR #{pr_number}: {title}", flush=True)

        # Get the diff.
        diff_result = _run(
            ["gh", "pr", "diff", str(pr_number), "--repo", REPO],
            capture_output=True,
        )
        if diff_result.returncode != 0:
            print(
                f"[cai review-pr] could not fetch diff for PR #{pr_number}:\n"
                f"{diff_result.stderr}",
                file=sys.stderr,
            )
            continue
        pr_diff = diff_result.stdout

        # Clone the repo for the agent to walk.
        _uid = uuid.uuid4().hex[:8]
        work_dir = Path(f"/tmp/cai-review-{pr_number}-{_uid}")
        try:
            if work_dir.exists():
                shutil.rmtree(work_dir)

            clone = _run(
                ["git", "clone", "--depth", "1",
                 f"https://github.com/{REPO}.git", str(work_dir)],
                capture_output=True,
            )
            if clone.returncode != 0:
                print(
                    f"[cai review-pr] clone failed for PR #{pr_number}:\n{clone.stderr}",
                    file=sys.stderr,
                )
                continue

            # Build the user message. The system prompt, tool
            # allowlist (Read/Grep/Glob), and hard rules all
            # live in `.claude/agents/cai-review-pr.md`. The wrapper
            # passes the work-directory block (so the agent knows
            # where the cloned PR is) plus the dynamic per-run
            # context via stdin (#342).
            author_login = pr.get("author", {}).get("login", "unknown")
            user_message = (
                _work_directory_block(work_dir)
                + "\n"
                + f"## PR metadata\n\n"
                + f"- **Number:** #{pr_number}\n"
                + f"- **Title:** {title}\n"
                + f"- **Author:** @{author_login}\n"
                + f"- **Base:** main\n"
                + f"- **HEAD SHA:** {head_sha}\n\n"
                + f"## PR diff\n\n"
                + f"```diff\n{pr_diff}\n```\n"
            )

            # Invoke the declared cai-review-pr subagent.
            # Runs with `cwd=/app` and `--add-dir <work_dir>` (#342)
            # so it reads its definition + memory from the canonical
            # /app paths while reviewing the cloned PR via absolute
            # paths.
            _write_active_job("review-pr", "pr", pr_number)
            agent = _run_claude_p(
                ["claude", "-p", "--agent", "cai-review-pr",
                 "--permission-mode", "acceptEdits",
                 "--max-budget-usd", "0.50",
                 "--add-dir", str(work_dir)],
                category="review-pr",
                agent="cai-review-pr",
                input=user_message,
                cwd="/app",
            )
            if agent.stdout:
                print(agent.stdout, flush=True)
            if agent.returncode != 0:
                print(
                    f"[cai review-pr] agent failed for PR #{pr_number} "
                    f"(exit {agent.returncode}):\n{agent.stderr}",
                    file=sys.stderr,
                )
                continue

            agent_output = (agent.stdout or "").strip()

            # Determine if there are findings.
            has_findings = (
                "### Finding:" in agent_output
                and "No ripple effects found" not in agent_output
            )

            if has_findings:
                # Findings comments use the actionable heading form
                # so the revise subagent picks them up on its next
                # tick (`_BOT_COMMENT_MARKERS` does NOT match this).
                comment_body = (
                    f"{_REVIEW_COMMENT_HEADING_FINDINGS} \u2014 {head_sha}\n\n"
                    f"{agent_output}\n\n"
                    f"---\n"
                    f"_Pre-merge consistency review by `cai review-pr`. "
                    f"Address the findings above or explain why they don't "
                    f"apply, then push a new commit to trigger a re-review._"
                )
            else:
                # Clean comments use the (clean) heading variant so
                # `_BOT_COMMENT_MARKERS` filters them out — no need
                # for revise to act on a "no findings" report.
                comment_body = (
                    f"{_REVIEW_COMMENT_HEADING_CLEAN} \u2014 {head_sha}\n\n"
                    f"No ripple effects found.\n\n"
                    f"---\n"
                    f"_Pre-merge consistency review by `cai review-pr`._"
                )

            _run(
                ["gh", "pr", "comment", str(pr_number),
                 "--repo", REPO, "--body", comment_body],
                capture_output=True,
            )

            _log_review_pr_findings(pr_number, head_sha, agent_output)

            finding_word = "with findings" if has_findings else "clean"
            print(
                f"[cai review-pr] posted review on PR #{pr_number} ({finding_word})",
                flush=True,
            )
            reviewed += 1

        except Exception as e:
            print(
                f"[cai review-pr] unexpected failure for PR #{pr_number}: {e!r}",
                file=sys.stderr,
            )
        finally:
            _clear_active_job()
            if work_dir.exists():
                shutil.rmtree(work_dir, ignore_errors=True)

    dur = f"{int(time.monotonic() - t0)}s"
    print(
        f"[cai review-pr] reviewed={reviewed} skipped={skipped}",
        flush=True,
    )
    log_run("review_pr", repo=REPO, reviewed=reviewed, skipped=skipped,
            duration=dur, exit=0)
    return 0


# ---------------------------------------------------------------------------
# review-docs — pre-merge documentation review
# ---------------------------------------------------------------------------

# docs-review posts two comment variants depending on the outcome:
#
#   * `_DOCS_REVIEW_COMMENT_HEADING_FINDINGS` — used in two cases:
#     (1) The agent fixed stale docs and pushed a commit to the PR
#         branch. The comment is posted at the *new* SHA and may
#         contain `### Fixed: stale_docs` blocks plus any remaining
#         `### Finding: stale_docs` blocks for issues it could not
#         fix automatically.
#     (2) The agent found unfixable issues and did not push. The
#         comment is posted at the original SHA and contains
#         `### Finding: stale_docs` blocks that the revise subagent
#         can pick up and address.
#     NOT in `_BOT_COMMENT_MARKERS` so the revise subagent considers
#     it actionable; when all items are already fixed (case 1) the
#     revise agent will see no open findings and skip it naturally.
#
#   * `_DOCS_REVIEW_COMMENT_HEADING_CLEAN` — informational only, says
#     "no documentation updates needed". IS in `_BOT_COMMENT_MARKERS`
#     so the revise subagent skips it (no actionable content).
#
# Both forms include the head SHA at the end of the heading line so
# the SHA-idempotency check can recognize either as "already
# reviewed at this commit".
_DOCS_REVIEW_COMMENT_HEADING_FINDINGS = "## cai docs review"
_DOCS_REVIEW_COMMENT_HEADING_CLEAN = "## cai docs review (clean)"


def cmd_review_docs(args) -> int:
    """Fix stale documentation on open PRs and post findings for issues that cannot be fixed automatically."""
    print("[cai review-docs] checking open PRs against main", flush=True)
    t0 = time.monotonic()

    if getattr(args, "pr", None) is not None:
        # Direct targeting: look up the specified PR.
        try:
            target_pr = _gh_json([
                "pr", "view", str(args.pr),
                "--repo", REPO,
                "--json", "number,title,author,headRefOid,headRefName,comments",
            ])
        except subprocess.CalledProcessError as e:
            print(f"[cai review-docs] gh pr view #{args.pr} failed:\n{e.stderr}", file=sys.stderr)
            log_run("review_docs", repo=REPO, result="pr_lookup_failed", exit=1)
            return 1
        prs = [target_pr]
    else:
        try:
            prs = _gh_json([
                "pr", "list",
                "--repo", REPO,
                "--state", "open",
                "--base", "main",
                "--json", "number,title,author,headRefOid,headRefName,comments",
                "--limit", "50",
            ]) or []
        except subprocess.CalledProcessError as e:
            print(f"[cai review-docs] gh pr list failed:\n{e.stderr}", file=sys.stderr)
            log_run("review_docs", repo=REPO, result="pr_list_failed", exit=1)
            return 1

    if not prs:
        print("[cai review-docs] no open PRs; nothing to do", flush=True)
        log_run("review_docs", repo=REPO, result="no_open_prs", exit=0)
        return 0

    reviewed = 0
    skipped = 0

    for pr in prs:
        pr_number = pr["number"]
        head_sha = pr["headRefOid"]
        branch = pr.get("headRefName", "")
        title = pr["title"]

        # Check if we already posted a docs review for this SHA.
        already_reviewed = False
        for comment in pr.get("comments", []):
            body = (comment.get("body") or "")
            first_line = body.split("\n", 1)[0]
            if (
                first_line.startswith(_DOCS_REVIEW_COMMENT_HEADING_FINDINGS)
                and head_sha in first_line
            ):
                already_reviewed = True
                break
        if already_reviewed:
            print(
                f"[cai review-docs] PR #{pr_number}: already reviewed at {head_sha[:8]}; skipping",
                flush=True,
            )
            skipped += 1
            continue

        # Gate: only review docs after review-pr has reviewed this SHA.
        # This enforces the review-pr → review-docs → merge ordering.
        has_code_review_at_sha = False
        for comment in pr.get("comments", []):
            body = (comment.get("body") or "")
            first_line = body.split("\n", 1)[0]
            if (
                first_line.startswith(_REVIEW_COMMENT_HEADING_FINDINGS)
                and head_sha in first_line
            ):
                has_code_review_at_sha = True
                break

        if not has_code_review_at_sha:
            print(
                f"[cai review-docs] PR #{pr_number}: review-pr has not reviewed "
                f"{head_sha[:8]} yet; waiting",
                flush=True,
            )
            skipped += 1
            continue

        print(f"[cai review-docs] reviewing PR #{pr_number}: {title}", flush=True)

        # Get the diff.
        diff_result = _run(
            ["gh", "pr", "diff", str(pr_number), "--repo", REPO],
            capture_output=True,
        )
        if diff_result.returncode != 0:
            print(
                f"[cai review-docs] could not fetch diff for PR #{pr_number}:\n"
                f"{diff_result.stderr}",
                file=sys.stderr,
            )
            continue
        pr_diff = diff_result.stdout

        # Clone the repo and check out the PR branch so the agent can edit docs.
        _uid = uuid.uuid4().hex[:8]
        work_dir = Path(f"/tmp/cai-review-docs-{pr_number}-{_uid}")
        try:
            if work_dir.exists():
                shutil.rmtree(work_dir)

            _run(["gh", "auth", "setup-git"], capture_output=True)
            clone = _run(
                ["gh", "repo", "clone", REPO, str(work_dir)],
                capture_output=True,
            )
            if clone.returncode != 0:
                print(
                    f"[cai review-docs] clone failed for PR #{pr_number}:\n{clone.stderr}",
                    file=sys.stderr,
                )
                continue

            _git(work_dir, "fetch", "origin", branch)
            _git(work_dir, "checkout", branch)

            # Configure git identity so the agent can commit.
            name, email = _gh_user_identity()
            _git(work_dir, "config", "user.name", name)
            _git(work_dir, "config", "user.email", email)

            author_login = pr.get("author", {}).get("login", "unknown")
            user_message = (
                _work_directory_block(work_dir)
                + "\n"
                + f"## PR metadata\n\n"
                + f"- **Number:** #{pr_number}\n"
                + f"- **Title:** {title}\n"
                + f"- **Author:** @{author_login}\n"
                + f"- **Base:** main\n"
                + f"- **HEAD SHA:** {head_sha}\n\n"
                + f"## PR diff\n\n"
                + f"```diff\n{pr_diff}\n```\n"
            )

            # Invoke the declared cai-review-docs subagent.
            _write_active_job("review-docs", "pr", pr_number)
            agent = _run_claude_p(
                ["claude", "-p", "--agent", "cai-review-docs",
                 "--permission-mode", "acceptEdits",
                 "--max-budget-usd", "0.50",
                 "--add-dir", str(work_dir)],
                category="review-docs",
                agent="cai-review-docs",
                input=user_message,
                cwd="/app",
            )
            if agent.stdout:
                print(agent.stdout, flush=True)
            if agent.returncode != 0:
                print(
                    f"[cai review-docs] agent failed for PR #{pr_number} "
                    f"(exit {agent.returncode}):\n{agent.stderr}",
                    file=sys.stderr,
                )
                continue

            agent_output = (agent.stdout or "").strip()

            # Check if the agent made any doc changes.
            status_result = _git(work_dir, "status", "--porcelain", check=False)
            has_doc_changes = bool(status_result.stdout.strip())

            if has_doc_changes:
                # Commit and push the doc fixes.
                _git(work_dir, "add", "-A")
                _git(work_dir, "commit", "-m",
                     "docs: update documentation per review-docs\n\n"
                     "Applied by cai review-docs.")
                push = _run(
                    ["git", "-C", str(work_dir), "push", "origin", branch],
                    capture_output=True,
                )
                if push.returncode != 0:
                    print(
                        f"[cai review-docs] push failed for PR #{pr_number}:\n"
                        f"{push.stderr}",
                        file=sys.stderr,
                    )
                    continue
                new_sha = _git(work_dir, "rev-parse", "HEAD").stdout.strip()
                comment_body = (
                    f"{_DOCS_REVIEW_COMMENT_HEADING_FINDINGS} \u2014 {new_sha}\n\n"
                    f"{agent_output}\n\n"
                    f"---\n"
                    f"_Documentation updated automatically by `cai review-docs`._"
                )
                print(
                    f"[cai review-docs] pushed doc fixes to PR #{pr_number}",
                    flush=True,
                )
            else:
                # No file changes — post clean or findings comment at original SHA.
                has_text_findings = (
                    "### Finding:" in agent_output
                    and "No documentation updates needed" not in agent_output
                )
                if has_text_findings:
                    comment_body = (
                        f"{_DOCS_REVIEW_COMMENT_HEADING_FINDINGS} \u2014 {head_sha}\n\n"
                        f"{agent_output}\n\n"
                        f"---\n"
                        f"_Pre-merge documentation review by `cai review-docs`. "
                        f"Address the findings above or explain why they don't "
                        f"apply, then push a new commit to trigger a re-review._"
                    )
                else:
                    comment_body = (
                        f"{_DOCS_REVIEW_COMMENT_HEADING_CLEAN} \u2014 {head_sha}\n\n"
                        f"No documentation updates needed.\n\n"
                        f"---\n"
                        f"_Pre-merge documentation review by `cai review-docs`._"
                    )

            _run(
                ["gh", "pr", "comment", str(pr_number),
                 "--repo", REPO, "--body", comment_body],
                capture_output=True,
            )

            result_word = "fixes pushed" if has_doc_changes else (
                "with findings" if not has_doc_changes and "### Finding:" in agent_output
                else "clean"
            )
            print(
                f"[cai review-docs] posted review on PR #{pr_number} ({result_word})",
                flush=True,
            )
            reviewed += 1

        except Exception as e:
            print(
                f"[cai review-docs] unexpected failure for PR #{pr_number}: {e!r}",
                file=sys.stderr,
            )
        finally:
            _clear_active_job()
            if work_dir.exists():
                shutil.rmtree(work_dir, ignore_errors=True)

    dur = f"{int(time.monotonic() - t0)}s"
    print(
        f"[cai review-docs] reviewed={reviewed} skipped={skipped}",
        flush=True,
    )
    log_run("review_docs", repo=REPO, reviewed=reviewed, skipped=skipped,
            duration=dur, exit=0)
    return 0


# ---------------------------------------------------------------------------
# Merge — confidence-gated auto-merge for bot PRs
# ---------------------------------------------------------------------------

_MERGE_COMMENT_HEADING = "## cai merge verdict"

# Confidence threshold: only verdicts at or above this level trigger a merge.
# "high" = only high merges, "medium" = high + medium merge, "disabled" = never merge.
_MERGE_THRESHOLD = os.environ.get("CAI_MERGE_CONFIDENCE_THRESHOLD", "high").lower()

_CONFIDENCE_RANKS = {"high": 3, "medium": 2, "low": 1}


def _pr_set_needs_human(pr_number: int, needs: bool) -> None:
    """Add or remove the `needs-human-review` label on a PR.

    Idempotent: gh silently no-ops if the label is already in the
    requested state. Logged but not fatal on failure — labelling is a
    UX nicety, not a correctness requirement.
    """
    flag = "--add-label" if needs else "--remove-label"
    res = _run(
        ["gh", "pr", "edit", str(pr_number),
         "--repo", REPO, flag, LABEL_PR_NEEDS_HUMAN],
        capture_output=True,
    )
    if res.returncode != 0:
        action = "add" if needs else "remove"
        print(
            f"[cai merge] PR #{pr_number}: could not {action} "
            f"label `{LABEL_PR_NEEDS_HUMAN}`:\n{res.stderr}",
            file=sys.stderr,
        )


def _pr_label_sweep() -> tuple[int, int]:
    """Sync `needs-human-review` across every open bot PR.

    Run after the merge loop so that PRs the merge step did NOT
    process this tick (e.g., idempotency-skipped because no human
    comment landed since the last verdict, or in `:revising` state)
    still pick up the label whenever the bot has signalled it cannot
    move forward alone. Without this sweep, a PR that hits
    `rebase resolution failed` once would never be labelled, since
    that failure path doesn't go through the merge agent. Refs #223.

    Signals (each scoped to comments AFTER the latest commit so a
    fresh push naturally clears them):

    - latest `## cai merge verdict` is below the auto-merge threshold
      (or rejects, but the PR is still open)
    - any `## Revise subagent: rebase resolution failed` comment
    - any `## Revise subagent: no additional changes` comment
    - mergeStateStatus is DIRTY (unresolved conflict against main)

    Returns (added, removed) for the run summary.
    """
    try:
        prs = _gh_json([
            "pr", "list",
            "--repo", REPO,
            "--state", "open",
            "--limit", "100",
            "--json",
            "number,labels,comments,mergeStateStatus,headRefName,commits",
        ])
    except subprocess.CalledProcessError:
        return (0, 0)

    threshold_rank = _CONFIDENCE_RANKS.get(_MERGE_THRESHOLD, _CONFIDENCE_RANKS["high"])
    added = 0
    removed = 0

    for pr in prs:
        branch = pr.get("headRefName", "")
        if not branch.startswith("auto-improve/"):
            continue

        pr_number = pr["number"]
        comments = pr.get("comments", [])
        merge_state = pr.get("mergeStateStatus", "")
        labels = {l.get("name", "") for l in pr.get("labels", [])}
        currently_labeled = LABEL_PR_NEEDS_HUMAN in labels

        # Scope signals to comments newer than the latest commit so
        # that a fresh push (rebase, revise, etc.) clears stale
        # markers from earlier rounds.
        commits = pr.get("commits", [])
        last_commit_date = commits[-1].get("committedDate", "") if commits else ""
        commit_ts = _parse_iso_ts(last_commit_date)

        needs = False

        # Signal: latest merge verdict is below threshold (or reject).
        latest_verdict_ts = None
        latest_verdict = None
        for c in comments:
            body = (c.get("body") or "").lstrip()
            if not body.startswith(_MERGE_COMMENT_HEADING):
                continue
            v_ts = _parse_iso_ts(c.get("createdAt"))
            if v_ts is None:
                continue
            if latest_verdict_ts is None or v_ts > latest_verdict_ts:
                latest_verdict_ts = v_ts
                latest_verdict = _parse_merge_verdict(body)
        if latest_verdict and (commit_ts is None or latest_verdict_ts > commit_ts):
            action = latest_verdict.get("action", "")
            v_rank = _CONFIDENCE_RANKS.get(latest_verdict.get("confidence", ""), 0)
            if action in ("hold", "reject") or v_rank < threshold_rank:
                needs = True

        # Signal: bot bailed on rebase or review comments.
        if not needs:
            for c in comments:
                body = (c.get("body") or "").lstrip()
                if not (body.startswith(_REBASE_FAILED_MARKER) or
                        body.startswith(_NO_ADDITIONAL_CHANGES_MARKER)):
                    continue
                ts = _parse_iso_ts(c.get("createdAt"))
                if commit_ts is not None and (ts is None or ts <= commit_ts):
                    continue
                needs = True
                break

        # Signal: PR has unresolved merge conflict against main.
        if not needs and merge_state == "DIRTY":
            needs = True

        if needs and not currently_labeled:
            _pr_set_needs_human(pr_number, True)
            added += 1
        elif not needs and currently_labeled:
            _pr_set_needs_human(pr_number, False)
            removed += 1

    return (added, removed)


def _parse_merge_verdict(text: str) -> dict | None:
    """Extract confidence, action, and reasoning from the agent's output."""
    conf_m = re.search(r"\*\*Confidence:\*\*\s*(high|medium|low)", text, re.IGNORECASE)
    act_m = re.search(r"\*\*Action:\*\*\s*(merge|hold|reject)", text, re.IGNORECASE)
    reason_m = re.search(r"\*\*Reasoning:\*\*\s*(.+)", text, re.IGNORECASE)
    if not conf_m or not act_m:
        return None
    return {
        "confidence": conf_m.group(1).lower(),
        "action": act_m.group(1).lower(),
        "reasoning": reason_m.group(1).strip() if reason_m else "(no reasoning provided)",
    }


def cmd_merge(args) -> int:
    """Confidence-gated auto-merge for bot PRs."""
    print("[cai merge] checking open PRs for auto-merge", flush=True)
    t0 = time.monotonic()

    if _MERGE_THRESHOLD == "disabled":
        print("[cai merge] CAI_MERGE_CONFIDENCE_THRESHOLD=disabled; skipping", flush=True)
        log_run("merge", repo=REPO, result="disabled", exit=0)
        return 0

    if _MERGE_THRESHOLD not in ("high", "medium"):
        print(
            f"[cai merge] unknown threshold '{_MERGE_THRESHOLD}'; defaulting to 'high'",
            flush=True,
        )

    threshold_rank = _CONFIDENCE_RANKS.get(_MERGE_THRESHOLD, _CONFIDENCE_RANKS["high"])

    # Fetch open PRs.
    if getattr(args, "pr", None) is not None:
        # Direct targeting: look up the specified PR.
        try:
            target_pr = _gh_json([
                "pr", "view", str(args.pr),
                "--repo", REPO,
                "--json", "number,title,headRefName,headRefOid,comments,mergeable",
            ])
        except subprocess.CalledProcessError as e:
            print(f"[cai merge] gh pr view #{args.pr} failed:\n{e.stderr}", file=sys.stderr)
            log_run("merge", repo=REPO, result="pr_lookup_failed", exit=1)
            return 1
        prs = [target_pr]
    else:
        try:
            prs = _gh_json([
                "pr", "list",
                "--repo", REPO,
                "--state", "open",
                "--base", "main",
                "--json", "number,title,headRefName,headRefOid,comments,mergeable",
                "--limit", "50",
            ]) or []
        except subprocess.CalledProcessError as e:
            print(f"[cai merge] gh pr list failed:\n{e.stderr}", file=sys.stderr)
            log_run("merge", repo=REPO, result="pr_list_failed", exit=1)
            return 1

    if not prs:
        print("[cai merge] no open PRs; nothing to do", flush=True)
        log_run("merge", repo=REPO, result="no_open_prs", exit=0)
        return 0

    evaluated = 0
    merged = 0
    held = 0
    closed = 0

    for pr in prs:
        pr_number = pr["number"]
        head_sha = pr["headRefOid"]
        branch = pr.get("headRefName", "")
        title = pr["title"]

        # Safety filter 1: only bot PRs.
        m = re.match(r"^auto-improve/(\d+)-", branch)
        if not m:
            continue
        issue_number = int(m.group(1))

        # Safety filter 4: unmergeable PRs (conflicts).
        mergeable = pr.get("mergeable", "")
        if mergeable == "CONFLICTING":
            print(
                f"[cai merge] PR #{pr_number}: unmergeable (conflicts); skipping",
                flush=True,
            )
            continue

        # Safety filter 2: linked issue must be in :pr-open state.
        try:
            issue = _gh_json([
                "issue", "view", str(issue_number),
                "--repo", REPO,
                "--json", "labels,state",
            ])
        except subprocess.CalledProcessError:
            print(
                f"[cai merge] PR #{pr_number}: could not fetch issue #{issue_number}; skipping",
                flush=True,
            )
            continue

        issue_labels = [l["name"] for l in issue.get("labels", [])]
        if LABEL_PR_OPEN not in issue_labels:
            continue
        # NOTE: do NOT skip on `merge-blocked`. The label
        # is informational only — it records "the last evaluation
        # decided not to merge". Re-evaluation gating is purely
        # SHA-based (see safety filter 6 below): if the PR's HEAD SHA
        # has a prior merge-verdict comment, we skip; otherwise we
        # re-evaluate.
        #
        # NOTE (issue #399): `cai revise` no longer runs on PRs that
        # carry the `merge-blocked` or `needs-human-review` label, so
        # the automatic SHA-based re-evaluation loop no longer applies
        # to blocked PRs. A human must manually clear the
        # `merge-blocked` label (and any `needs-human-review` label)
        # to restart the merge-evaluation cycle for those PRs.

        # Safety filter 7: require `cai review-pr` to have reviewed
        # the current head SHA before we run a merge verdict on it.
        #
        # Without this gate, `cmd_merge` runs on every new commit — in
        # particular, every commit that `cmd_revise` pushes in response
        # to a prior review's findings — BEFORE the reviewer has had a
        # chance to walk the new diff. That produces a verdict on an
        # un-reviewed SHA (uninformed by the ripple-effect scan) and,
        # via `_pr_label_sweep`, flips the PR to `needs-human-review`
        # while the fix/review/revise loop is still actively making
        # progress. The PR then flaps through the label on every cycle
        # until the reviewer finally reports clean. Refs #351
        # post-mortem: 5+ premature verdicts, each one flagging the PR
        # for human triage even though revise was still addressing
        # ripple findings one round at a time.
        #
        # Matches either heading variant (findings or clean) because
        # both start with `_REVIEW_COMMENT_HEADING_FINDINGS` and both
        # include the head SHA on the heading line. Mirrors the
        # already-reviewed check in `cmd_review_pr`.
        has_review_at_sha = False
        for comment in pr.get("comments", []):
            body = (comment.get("body") or "")
            first_line = body.split("\n", 1)[0]
            if (
                first_line.startswith(_REVIEW_COMMENT_HEADING_FINDINGS)
                and head_sha in first_line
            ):
                has_review_at_sha = True
                break

        if not has_review_at_sha:
            print(
                f"[cai merge] PR #{pr_number}: review-pr has not reviewed "
                f"{head_sha[:8]} yet; waiting",
                flush=True,
            )
            continue

        # Safety filter 7b: require `cai review-docs` to have reviewed
        # the current head SHA before running a merge verdict.
        has_docs_review_at_sha = False
        for comment in pr.get("comments", []):
            body = (comment.get("body") or "")
            first_line = body.split("\n", 1)[0]
            if (
                first_line.startswith(_DOCS_REVIEW_COMMENT_HEADING_FINDINGS)
                and head_sha in first_line
            ):
                has_docs_review_at_sha = True
                break

        if not has_docs_review_at_sha:
            print(
                f"[cai merge] PR #{pr_number}: review-docs has not reviewed "
                f"{head_sha[:8]} yet; waiting",
                flush=True,
            )
            continue

        # Safety filter 3: unaddressed review comments → let revise handle.
        # Mirror the revise subcommand's filter logic via the shared helper
        # so a "no additional changes" reply correctly suppresses the loop.
        all_comments = list(pr.get("comments", []))
        try:
            all_comments.extend(_fetch_review_comments(pr_number))
        except Exception:
            pass

        # Fetch the most recent commit timestamp on the branch.
        try:
            commits = _gh_json([
                "pr", "view", str(pr_number),
                "--repo", REPO,
                "--json", "commits",
            ])
            commit_list = commits.get("commits", [])
            last_commit_date = commit_list[-1].get("committedDate", "") if commit_list else ""
        except (subprocess.CalledProcessError, KeyError):
            last_commit_date = ""

        commit_ts = _parse_iso_ts(last_commit_date)
        unaddressed = (
            _filter_unaddressed_comments(all_comments, commit_ts)
            if commit_ts is not None
            else []
        )
        has_unaddressed = bool(unaddressed)

        if has_unaddressed:
            print(
                f"[cai merge] PR #{pr_number}: has unaddressed review comments; skipping",
                flush=True,
            )
            continue

        # Safety filter 5: failed CI checks.
        try:
            pr_detail = _gh_json([
                "pr", "view", str(pr_number),
                "--repo", REPO,
                "--json", "statusCheckRollup",
            ])
            for check in pr_detail.get("statusCheckRollup", []):
                conclusion = (check.get("conclusion") or "").upper()
                status = (check.get("status") or "").upper()
                if conclusion == "FAILURE" or status == "FAILURE":
                    print(
                        f"[cai merge] PR #{pr_number}: has failed CI checks; skipping",
                        flush=True,
                    )
                    has_unaddressed = True  # reuse flag to skip
                    break
        except (subprocess.CalledProcessError, json.JSONDecodeError, TypeError):
            pass  # no CI checks is fine

        if has_unaddressed:
            continue

        # Safety filter 6: already evaluated at this SHA, AND no new
        # human comment has been posted since the most recent verdict.
        #
        # The original idempotency was SHA-only: once a verdict existed
        # for the current HEAD SHA, we'd skip forever. That left PRs
        # parked when the verdict's reasoning was effectively addressed
        # by conversation (e.g. the user clarifies an ambiguity in a
        # comment, no code change needed). Now we re-evaluate when
        # there's any human comment newer than the latest verdict —
        # the merge agent gets the comment thread as context and can
        # flip its verdict based on the discussion.
        latest_verdict_ts = None
        for comment in pr.get("comments", []):
            body = (comment.get("body") or "")
            if not body.startswith(f"{_MERGE_COMMENT_HEADING} \u2014 {head_sha}"):
                continue
            v_ts = _parse_iso_ts(comment.get("createdAt"))
            if v_ts is None:
                continue
            if latest_verdict_ts is None or v_ts > latest_verdict_ts:
                latest_verdict_ts = v_ts

        if latest_verdict_ts is not None:
            # Look for any human comment newer than the latest verdict.
            has_newer_human_comment = False
            for c in all_comments:
                if _is_bot_comment(c):
                    continue
                c_ts = _parse_iso_ts(c.get("createdAt"))
                if c_ts is not None and c_ts > latest_verdict_ts:
                    has_newer_human_comment = True
                    break
            if not has_newer_human_comment:
                print(
                    f"[cai merge] PR #{pr_number}: already evaluated at {head_sha[:8]}; skipping",
                    flush=True,
                )
                continue
            print(
                f"[cai merge] PR #{pr_number}: re-evaluating — new human comment since last verdict",
                flush=True,
            )

        # All filters passed — evaluate with the model.
        print(f"[cai merge] evaluating PR #{pr_number}: {title}", flush=True)

        # Fetch issue body.
        try:
            issue_full = _gh_json([
                "issue", "view", str(issue_number),
                "--repo", REPO,
                "--json", "number,title,body",
            ])
        except subprocess.CalledProcessError:
            issue_full = {"number": issue_number, "title": "(unknown)", "body": ""}

        # Fetch PR diff.
        diff_result = _run(
            ["gh", "pr", "diff", str(pr_number), "--repo", REPO],
            capture_output=True,
        )
        if diff_result.returncode != 0:
            print(
                f"[cai merge] could not fetch diff for PR #{pr_number}; skipping",
                file=sys.stderr,
            )
            continue
        pr_diff = diff_result.stdout

        # Gather PR comments for context.
        comment_texts = []
        for c in all_comments:
            body = (c.get("body") or "").strip()
            if body:
                comment_texts.append(body)
        comments_section = "\n\n---\n\n".join(comment_texts) if comment_texts else "(no comments)"

        # Build the user message. The system prompt, tool allowlist,
        # and model (opus-4-6) all live in `.claude/agents/cai-merge.md`.
        # The wrapper only passes dynamic per-run context via stdin.
        user_message = (
            f"## Linked issue\n\n"
            f"### #{issue_full.get('number', issue_number)} \u2014 {issue_full.get('title', '')}\n\n"
            f"{issue_full.get('body') or '(no body)'}\n\n"
            f"## PR diff\n\n"
            f"```diff\n{pr_diff}\n```\n\n"
            f"## PR comments\n\n"
            f"{comments_section}\n"
        )

        # Invoke the declared cai-merge subagent.
        _write_active_job("merge", "pr", pr_number)
        agent = _run_claude_p(
            ["claude", "-p", "--agent", "cai-merge"],
            category="merge",
            agent="cai-merge",
            input=user_message,
        )
        _clear_active_job()
        if agent.returncode != 0:
            print(
                f"[cai merge] model failed for PR #{pr_number} "
                f"(exit {agent.returncode}):\n{agent.stderr}",
                file=sys.stderr,
            )
            continue

        agent_output = (agent.stdout or "").strip()
        verdict = _parse_merge_verdict(agent_output)

        if not verdict:
            print(
                f"[cai merge] PR #{pr_number}: could not parse verdict; skipping",
                flush=True,
            )
            continue

        confidence = verdict["confidence"]
        action = verdict["action"]
        reasoning = verdict["reasoning"]
        evaluated += 1

        # Post the verdict as a PR comment.
        comment_body = (
            f"{_MERGE_COMMENT_HEADING} \u2014 {head_sha}\n\n"
            f"{agent_output}\n\n"
            f"---\n"
            f"_Auto-merge review by `cai merge`. "
            f"Threshold: `{_MERGE_THRESHOLD}`, verdict: `{confidence}`, action: `{action}`._"
        )
        _run(
            ["gh", "pr", "comment", str(pr_number),
             "--repo", REPO, "--body", comment_body],
            capture_output=True,
        )

        # Decide whether to merge, hold, or reject.
        verdict_rank = _CONFIDENCE_RANKS.get(confidence, 0)

        if action == "reject" and verdict_rank >= threshold_rank:
            # High-confidence reject: close the PR and mark the issue as no-action.
            print(
                f"[cai merge] PR #{pr_number}: verdict={confidence} reject >= threshold={_MERGE_THRESHOLD}; closing",
                flush=True,
            )
            close_result = _run(
                ["gh", "pr", "close", str(pr_number),
                 "--repo", REPO, "--delete-branch"],
                capture_output=True,
            )
            if close_result.returncode == 0:
                print(f"[cai merge] PR #{pr_number}: closed successfully", flush=True)
                if not _set_labels(issue_number, add=[LABEL_NO_ACTION], remove=[LABEL_PR_OPEN, LABEL_MERGE_BLOCKED, LABEL_REVISING], log_prefix="cai merge"):
                    print(
                        f"[cai merge] WARNING: label transition to :no-action failed for "
                        f"#{issue_number} after closing PR #{pr_number}; retrying",
                        flush=True,
                    )
                    if not _set_labels(issue_number, add=[LABEL_NO_ACTION], remove=[LABEL_PR_OPEN, LABEL_MERGE_BLOCKED, LABEL_REVISING], log_prefix="cai merge"):
                        print(
                            f"[cai merge] WARNING: label transition to :no-action failed twice for "
                            f"#{issue_number} — issue may be stuck without a lifecycle label",
                            file=sys.stderr, flush=True,
                        )
                        _pr_set_needs_human(pr_number, True)
                        held += 1
                        continue
                closed += 1
            else:
                print(
                    f"[cai merge] PR #{pr_number}: close failed:\n{close_result.stderr}",
                    file=sys.stderr,
                )
                if not _issue_has_label(issue_number, LABEL_MERGED):
                    if not _set_labels(issue_number, add=[LABEL_MERGE_BLOCKED], log_prefix="cai merge"):
                        print(
                            f"[cai merge] WARNING: failed to add :merge-blocked label to "
                            f"#{issue_number} after close failure on PR #{pr_number}",
                            file=sys.stderr, flush=True,
                        )
                # Close failed → PR is still open and needs human attention.
                _pr_set_needs_human(pr_number, True)
                held += 1
        elif action == "merge" and verdict_rank >= threshold_rank:
            print(
                f"[cai merge] PR #{pr_number}: verdict={confidence} >= threshold={_MERGE_THRESHOLD}; merging",
                flush=True,
            )
            merge_result = _run(
                ["gh", "pr", "merge", str(pr_number),
                 "--repo", REPO, "--merge", "--delete-branch"],
                capture_output=True,
            )
            if merge_result.returncode == 0:
                print(f"[cai merge] PR #{pr_number}: merged successfully", flush=True)
                if not _set_labels(issue_number, add=[LABEL_MERGED], remove=[LABEL_PR_OPEN, LABEL_MERGE_BLOCKED, LABEL_REVISING], log_prefix="cai merge"):
                    print(
                        f"[cai merge] WARNING: label transition to :merged failed for "
                        f"#{issue_number} after merging PR #{pr_number}; retrying",
                        flush=True,
                    )
                    if not _set_labels(issue_number, add=[LABEL_MERGED], remove=[LABEL_PR_OPEN, LABEL_MERGE_BLOCKED, LABEL_REVISING], log_prefix="cai merge"):
                        print(
                            f"[cai merge] WARNING: label transition to :merged failed twice for "
                            f"#{issue_number} — issue may be stuck without a lifecycle label",
                            file=sys.stderr, flush=True,
                        )
                        _pr_set_needs_human(pr_number, True)
                        held += 1
                        continue
                merged += 1
            else:
                print(
                    f"[cai merge] PR #{pr_number}: merge failed:\n{merge_result.stderr}",
                    file=sys.stderr,
                )
                # Merge failed → PR is still open and needs human attention.
                _pr_set_needs_human(pr_number, True)
                held += 1
        else:
            print(
                f"[cai merge] PR #{pr_number}: verdict={confidence} < threshold={_MERGE_THRESHOLD}; holding",
                flush=True,
            )
            # Set merge-blocked label on the issue, unless already merged.
            # Re-fetch to avoid race with a concurrent merge run.
            if not _issue_has_label(issue_number, LABEL_MERGED):
                if not _set_labels(issue_number, add=[LABEL_MERGE_BLOCKED], log_prefix="cai merge"):
                    print(
                        f"[cai merge] WARNING: failed to add :merge-blocked label to "
                        f"#{issue_number} for held PR #{pr_number}",
                        file=sys.stderr, flush=True,
                    )
            # Tag the PR itself so humans can filter `label:needs-human-review`.
            _pr_set_needs_human(pr_number, True)
            held += 1

    # Sweep `needs-human-review` across every open bot PR so that any
    # PR the merge agent did NOT touch this tick (idempotency-skipped,
    # in :revising, blocked on rebase, etc.) still ends up with a
    # correct label state. Refs #223.
    label_added, label_removed = _pr_label_sweep()
    if label_added or label_removed:
        print(
            f"[cai merge] needs-human-review sweep: "
            f"added={label_added} removed={label_removed}",
            flush=True,
        )

    dur = f"{int(time.monotonic() - t0)}s"
    print(
        f"[cai merge] prs_evaluated={evaluated} merged={merged} held={held} closed={closed}",
        flush=True,
    )
    log_run("merge", repo=REPO, prs_evaluated=evaluated, merged=merged,
            held=held, closed=closed,
            label_added=label_added, label_removed=label_removed,
            duration=dur, exit=0)
    return 0


# ---------------------------------------------------------------------------
# Refine — turn human-filed issues into structured plans
# ---------------------------------------------------------------------------


def cmd_refine(args) -> int:
    """Invoke the cai-refine agent on the oldest :raised or human:submitted issue."""
    print("[cai refine] looking for issues to refine", flush=True)
    t0 = time.monotonic()

    # 1. Find candidates.
    if getattr(args, "issue", None) is not None:
        # Direct targeting: look up the specified issue.
        try:
            issue = _gh_json([
                "issue", "view", str(args.issue),
                "--repo", REPO,
                "--json", "number,title,body,labels,createdAt,comments",
            ])
        except subprocess.CalledProcessError as e:
            print(f"[cai refine] gh issue view #{args.issue} failed:\n{e.stderr}", file=sys.stderr)
            log_run("refine", repo=REPO, result="issue_lookup_failed", exit=1)
            return 1
        issue_number = issue["number"]
        title = issue["title"]
        print(f"[cai refine] targeting #{issue_number}: {title}", flush=True)
    else:
        all_candidates = []
        for label in (LABEL_RAISED, LABEL_HUMAN_SUBMITTED):
            try:
                batch = _gh_json([
                    "issue", "list",
                    "--repo", REPO,
                    "--label", label,
                    "--state", "open",
                    "--json", "number,title,body,labels,createdAt,comments",
                    "--limit", "100",
                ]) or []
            except subprocess.CalledProcessError as e:
                print(
                    f"[cai refine] gh issue list failed:\n{e.stderr}",
                    file=sys.stderr,
                )
                log_run("refine", repo=REPO, result="list_failed", exit=1)
                return 1
            all_candidates.extend(batch)

        # Deduplicate by issue number (in case an issue has both labels).
        seen = set()
        issues = []
        for i in all_candidates:
            if i["number"] not in seen:
                seen.add(i["number"])
                issues.append(i)

        if not issues:
            print("[cai refine] no :raised or human:submitted issues; nothing to do", flush=True)
            log_run("refine", repo=REPO, result="no_eligible_issues", exit=0)
            return 0

        # 2. Pick the oldest.
        issue = min(issues, key=lambda i: i["createdAt"])
        issue_number = issue["number"]
        title = issue["title"]
        print(f"[cai refine] picked #{issue_number}: {title}", flush=True)

    # 3. Build user message and invoke cai-refine (read-only, no clone needed).
    user_message = _build_issue_block(issue)
    _write_active_job("refine", "issue", issue_number)
    try:
        result = _run_claude_p(
            ["claude", "-p", "--agent", "cai-refine",
             "--dangerously-skip-permissions"],
            category="refine",
            agent="cai-refine",
            input=user_message,
        )
    finally:
        _clear_active_job()
    print(result.stdout, flush=True)

    if result.returncode != 0:
        print(
            f"[cai refine] claude -p failed (exit {result.returncode}):\n"
            f"{result.stderr}",
            flush=True,
        )
        dur = f"{int(time.monotonic() - t0)}s"
        log_run("refine", repo=REPO, issue=issue_number,
                duration=dur, result="agent_failed", exit=result.returncode)
        return result.returncode

    stdout = result.stdout

    # 4. Check for early-exit (already structured).
    if "## No Refinement Needed" in stdout:
        print(
            f"[cai refine] #{issue_number} already structured; "
            f"transitioning to :refined",
            flush=True,
        )
        _set_labels(
            issue_number,
            add=[LABEL_REFINED],
            remove=[LABEL_RAISED, LABEL_HUMAN_SUBMITTED],
            log_prefix="cai refine",
        )
        dur = f"{int(time.monotonic() - t0)}s"
        log_run("refine", repo=REPO, issue=issue_number,
                duration=dur, result="already_structured", exit=0)
        return 0

    # 4b. Check for multi-step decomposition.
    if "## Multi-Step Decomposition" in stdout:
        steps = _parse_decomposition(stdout)
        if steps and len(steps) >= 2:
            print(
                f"[cai refine] #{issue_number} decomposed into "
                f"{len(steps)} steps",
                flush=True,
            )
            sub_nums = _create_sub_issues(steps, issue_number, title)
            if sub_nums:
                _update_parent_checklist(issue_number, sub_nums, steps)
            _set_labels(
                issue_number,
                add=[LABEL_PARENT],
                remove=[LABEL_RAISED, LABEL_HUMAN_SUBMITTED],
                log_prefix="cai refine",
            )
            dur = f"{int(time.monotonic() - t0)}s"
            log_run(
                "refine", repo=REPO, issue=issue_number,
                duration=dur, result="decomposed",
                sub_issues=len(sub_nums), steps=len(steps), exit=0,
            )
            return 0
        # Malformed decomposition (< 2 steps) — fall through to normal
        # refinement.
        print(
            f"[cai refine] #{issue_number} decomposition had "
            f"{len(steps)} step(s); falling through to normal refinement",
            flush=True,
        )

    # 5. Parse the refined issue block.
    marker = "## Refined Issue"
    marker_pos = stdout.find(marker)
    if marker_pos == -1:
        print(
            f"[cai refine] agent output missing '{marker}' marker; "
            f"leaving #{issue_number} as-is",
            flush=True,
        )
        dur = f"{int(time.monotonic() - t0)}s"
        log_run("refine", repo=REPO, issue=issue_number,
                duration=dur, result="no_marker", exit=0)
        return 0

    refined_body = stdout[marker_pos:].strip()

    # 6. Build the new issue body: refined content + original text quoted.
    original_body = issue.get("body") or "(no body)"
    quoted_original = "\n".join(f"> {line}" for line in original_body.splitlines())
    new_body = (
        f"{refined_body}\n\n"
        f"---\n\n"
        f"> **Original issue text:**\n>\n"
        f"{quoted_original}\n"
    )

    # 7. Update the issue body.
    update = _run(
        ["gh", "issue", "edit", str(issue_number),
         "--repo", REPO, "--body", new_body],
        capture_output=True,
    )
    if update.returncode != 0:
        print(
            f"[cai refine] gh issue edit failed:\n{update.stderr}",
            file=sys.stderr,
        )
        dur = f"{int(time.monotonic() - t0)}s"
        log_run("refine", repo=REPO, issue=issue_number,
                duration=dur, result="edit_failed", exit=1)
        return 1

    # 8. Transition labels: :raised / human:submitted → :refined.
    _set_labels(
        issue_number,
        add=[LABEL_REFINED],
        remove=[LABEL_RAISED, LABEL_HUMAN_SUBMITTED],
        log_prefix="cai refine",
    )

    dur = f"{int(time.monotonic() - t0)}s"
    print(
        f"[cai refine] #{issue_number} refined and transitioned to :refined "
        f"in {dur}",
        flush=True,
    )
    log_run("refine", repo=REPO, issue=issue_number,
            duration=dur, result="refined", exit=0)
    return 0


# ---------------------------------------------------------------------------
# Spike (research / verification)
# ---------------------------------------------------------------------------

def cmd_spike(args) -> int:
    """Run the cai-spike agent on the oldest :needs-spike issue."""
    print("[cai spike] looking for issues to spike", flush=True)
    t0 = time.monotonic()

    # 1. Find candidates.
    if getattr(args, "issue", None) is not None:
        # Direct targeting: look up the specified issue.
        try:
            issue = _gh_json([
                "issue", "view", str(args.issue),
                "--repo", REPO,
                "--json", "number,title,body,labels,createdAt,comments",
            ])
        except subprocess.CalledProcessError as e:
            print(f"[cai spike] gh issue view #{args.issue} failed:\n{e.stderr}", file=sys.stderr)
            log_run("spike", repo=REPO, result="issue_lookup_failed", exit=1)
            return 1
        if issue.get("state", "").upper() == "CLOSED":
            print(f"[cai spike] issue #{args.issue} is closed; nothing to do", flush=True)
            log_run("spike", repo=REPO, issue=args.issue, result="not_open", exit=0)
            return 0
        issue_number = issue["number"]
        title = issue["title"]
        print(f"[cai spike] targeting #{issue_number}: {title}", flush=True)
    else:
        try:
            issues = _gh_json([
                "issue", "list",
                "--repo", REPO,
                "--label", LABEL_NEEDS_SPIKE,
                "--state", "open",
                "--json", "number,title,body,labels,createdAt,comments",
                "--limit", "100",
            ]) or []
        except subprocess.CalledProcessError as e:
            print(f"[cai spike] gh issue list failed:\n{e.stderr}", file=sys.stderr)
            log_run("spike", repo=REPO, result="list_failed", exit=1)
            return 1

        if not issues:
            print("[cai spike] no :needs-spike issues; nothing to do", flush=True)
            log_run("spike", repo=REPO, result="no_eligible_issues", exit=0)
            return 0

        # 2. Pick the oldest.
        issue = min(issues, key=lambda i: i["createdAt"])
        issue_number = issue["number"]
        title = issue["title"]
        print(f"[cai spike] picked #{issue_number}: {title}", flush=True)

    # 3. Lock: :needs-spike → :in-progress.
    if not _set_labels(
        issue_number,
        add=[LABEL_IN_PROGRESS],
        remove=[LABEL_NEEDS_SPIKE],
        log_prefix="cai spike",
    ):
        print(f"[cai spike] could not lock #{issue_number}", file=sys.stderr)
        log_run("spike", repo=REPO, issue=issue_number, result="lock_failed", exit=1)
        return 1

    _write_active_job("spike", "issue", issue_number)
    _uid = uuid.uuid4().hex[:8]
    work_dir = Path(f"/tmp/cai-spike-{issue_number}-{_uid}")

    def rollback() -> None:
        _set_labels(
            issue_number,
            add=[LABEL_NEEDS_SPIKE],
            remove=[LABEL_IN_PROGRESS],
            log_prefix="cai spike",
        )

    try:
        if work_dir.exists():
            shutil.rmtree(work_dir)

        # 4. Clone (no branch needed — spikes don't commit).
        _run(["gh", "auth", "setup-git"], capture_output=True)
        clone = _run(
            ["git", "clone", "--depth", "1",
             f"https://github.com/{REPO}.git", str(work_dir)],
            capture_output=True,
        )
        if clone.returncode != 0:
            print(f"[cai spike] git clone failed:\n{clone.stderr}", file=sys.stderr)
            rollback()
            log_run("spike", repo=REPO, issue=issue_number, result="clone_failed", exit=1)
            return 1

        # 5. Build user message and invoke cai-spike.
        user_message = (
            _work_directory_block(work_dir)
            + "\n"
            + _build_issue_block(issue)
        )
        print(f"[cai spike] running cai-spike subagent for {work_dir}", flush=True)
        result = _run_claude_p(
            ["claude", "-p", "--agent", "cai-spike",
             "--dangerously-skip-permissions",
             "--add-dir", str(work_dir)],
            category="spike",
            agent="cai-spike",
            input=user_message,
            cwd="/app",
            timeout=900,  # 15 min cap
        )
        if result.stdout:
            print(result.stdout, flush=True)

        if result.returncode != 0:
            print(
                f"[cai spike] subagent failed (exit {result.returncode}):\n"
                f"{result.stderr}",
                file=sys.stderr,
            )
            rollback()
            dur = f"{int(time.monotonic() - t0)}s"
            log_run("spike", repo=REPO, issue=issue_number,
                    duration=dur, result="agent_failed", exit=result.returncode)
            return result.returncode

        stdout = result.stdout or ""

        # 6. Parse outcome markers (in priority order).

        # Outcome 1: Spike Findings
        findings_pos = stdout.find("## Spike Findings")
        if findings_pos != -1:
            findings_block = stdout[findings_pos:].strip()
            # Extract recommendation
            rec_match = re.search(
                r"###\s*Recommendation\s*\n+\s*(\S+)",
                findings_block,
            )
            recommendation = rec_match.group(1).strip() if rec_match else ""

            if recommendation in ("close_documented", "close_wont_do"):
                # Post findings as comment and close
                _run(
                    ["gh", "issue", "comment", str(issue_number),
                     "--repo", REPO,
                     "--body", f"## Spike findings\n\n{findings_block}\n\n---\n_Closed by `cai spike`._"],
                    capture_output=True,
                )
                _run(
                    ["gh", "issue", "close", str(issue_number),
                     "--repo", REPO],
                    capture_output=True,
                )
                _set_labels(issue_number, remove=[LABEL_IN_PROGRESS], log_prefix="cai spike")
                dur = f"{int(time.monotonic() - t0)}s"
                print(f"[cai spike] #{issue_number} closed ({recommendation}) in {dur}", flush=True)
                log_run("spike", repo=REPO, issue=issue_number,
                        duration=dur, result=recommendation, exit=0)
                return 0

            elif recommendation == "refine_and_retry":
                # Update body with findings + original, relabel to :raised
                original_body = issue.get("body") or "(no body)"
                quoted_original = "\n".join(f"> {line}" for line in original_body.splitlines())
                new_body = (
                    f"{findings_block}\n\n"
                    f"---\n\n"
                    f"> **Original issue text:**\n>\n"
                    f"{quoted_original}\n"
                )
                _run(
                    ["gh", "issue", "edit", str(issue_number),
                     "--repo", REPO, "--body", new_body],
                    capture_output=True,
                )
                _set_labels(
                    issue_number,
                    add=[LABEL_RAISED],
                    remove=[LABEL_IN_PROGRESS],
                    log_prefix="cai spike",
                )
                dur = f"{int(time.monotonic() - t0)}s"
                print(f"[cai spike] #{issue_number} refined-and-retried in {dur}", flush=True)
                log_run("spike", repo=REPO, issue=issue_number,
                        duration=dur, result="refine_and_retry", exit=0)
                return 0

            # Unrecognised recommendation — fall through to no_marker

        # Outcome 2: Refined Issue
        refined_pos = stdout.find("## Refined Issue")
        if refined_pos != -1:
            refined_body = stdout[refined_pos:].strip()
            original_body = issue.get("body") or "(no body)"
            quoted_original = "\n".join(f"> {line}" for line in original_body.splitlines())
            new_body = (
                f"{refined_body}\n\n"
                f"---\n\n"
                f"> **Original issue text:**\n>\n"
                f"{quoted_original}\n"
            )
            _run(
                ["gh", "issue", "edit", str(issue_number),
                 "--repo", REPO, "--body", new_body],
                capture_output=True,
            )
            _set_labels(
                issue_number,
                add=[LABEL_REFINED],
                remove=[LABEL_IN_PROGRESS],
                log_prefix="cai spike",
            )
            dur = f"{int(time.monotonic() - t0)}s"
            print(f"[cai spike] #{issue_number} refined and handed to fix in {dur}", flush=True)
            log_run("spike", repo=REPO, issue=issue_number,
                    duration=dur, result="refined", exit=0)
            return 0

        # Outcome 3: Spike Blocked
        blocked_pos = stdout.find("## Spike Blocked")
        if blocked_pos != -1:
            blocked_block = stdout[blocked_pos:].strip()
            _run(
                ["gh", "issue", "comment", str(issue_number),
                 "--repo", REPO,
                 "--body", f"{blocked_block}\n\n---\n_Escalated by `cai spike`._"],
                capture_output=True,
            )
            _set_labels(
                issue_number,
                add=[LABEL_PR_NEEDS_HUMAN],
                remove=[LABEL_IN_PROGRESS],
                log_prefix="cai spike",
            )
            dur = f"{int(time.monotonic() - t0)}s"
            print(f"[cai spike] #{issue_number} blocked/escalated in {dur}", flush=True)
            log_run("spike", repo=REPO, issue=issue_number,
                    duration=dur, result="blocked", exit=0)
            return 0

        # No recognised marker — rollback to :needs-spike.
        rollback()
        dur = f"{int(time.monotonic() - t0)}s"
        print(f"[cai spike] #{issue_number} no outcome marker; rolling back in {dur}", flush=True)
        log_run("spike", repo=REPO, issue=issue_number,
                duration=dur, result="no_marker", exit=0)
        return 0

    except Exception as exc:
        print(f"[cai spike] unexpected error: {exc}", file=sys.stderr)
        rollback()
        log_run("spike", repo=REPO, issue=issue_number, result="error", exit=1)
        return 1
    finally:
        _clear_active_job()
        if work_dir.exists():
            shutil.rmtree(work_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Explore (autonomous exploration / benchmarking)
# ---------------------------------------------------------------------------

def cmd_explore(args) -> int:
    """Run the cai-explore agent on the oldest :needs-exploration issue.

    Outcomes mirror cmd_spike:
    - ## Exploration Findings + ### Recommendation close_documented/close_wont_do → close
    - ## Exploration Findings + ### Recommendation refine_and_retry → :raised
    - ## Refined Issue → :refined (direct hand-off to fix)
    - ## Exploration Blocked → :needs-human-review
    - No marker → rollback to :needs-exploration
    """
    print("[cai explore] looking for issues to explore", flush=True)
    t0 = time.monotonic()

    if getattr(args, "issue", None) is not None:
        # Direct targeting: look up the specified issue.
        try:
            issue = _gh_json([
                "issue", "view", str(args.issue),
                "--repo", REPO,
                "--json", "number,title,body,labels,createdAt,comments",
            ])
        except subprocess.CalledProcessError as e:
            print(f"[cai explore] gh issue view #{args.issue} failed:\n{e.stderr}", file=sys.stderr)
            log_run("explore", repo=REPO, result="issue_lookup_failed", exit=1)
            return 1
        if issue.get("state", "").upper() == "CLOSED":
            print(f"[cai explore] issue #{args.issue} is closed; nothing to do", flush=True)
            log_run("explore", repo=REPO, issue=args.issue, result="not_open", exit=0)
            return 0
        issue_number = issue["number"]
        title = issue["title"]
        print(f"[cai explore] targeting #{issue_number}: {title}", flush=True)
    else:
        try:
            issues = _gh_json([
                "issue", "list",
                "--repo", REPO,
                "--label", LABEL_NEEDS_EXPLORATION,
                "--state", "open",
                "--json", "number,title,body,labels,createdAt,comments",
                "--limit", "100",
            ]) or []
        except subprocess.CalledProcessError as e:
            print(f"[cai explore] gh issue list failed:\n{e.stderr}", file=sys.stderr)
            log_run("explore", repo=REPO, result="list_failed", exit=1)
            return 1

        if not issues:
            print("[cai explore] no :needs-exploration issues; nothing to do", flush=True)
            log_run("explore", repo=REPO, result="no_eligible_issues", exit=0)
            return 0

        issue = min(issues, key=lambda i: i["createdAt"])
        issue_number = issue["number"]
        title = issue["title"]
        print(f"[cai explore] picked #{issue_number}: {title}", flush=True)

    # Lock: :needs-exploration → :in-progress.
    if not _set_labels(
        issue_number,
        add=[LABEL_IN_PROGRESS],
        remove=[LABEL_NEEDS_EXPLORATION],
        log_prefix="cai explore",
    ):
        print(f"[cai explore] could not lock #{issue_number}", file=sys.stderr)
        log_run("explore", repo=REPO, issue=issue_number, result="lock_failed", exit=1)
        return 1

    _write_active_job("explore", "issue", issue_number)
    _uid = uuid.uuid4().hex[:8]
    work_dir = Path(f"/tmp/cai-explore-{issue_number}-{_uid}")

    def rollback() -> None:
        _set_labels(
            issue_number,
            add=[LABEL_NEEDS_EXPLORATION],
            remove=[LABEL_IN_PROGRESS],
            log_prefix="cai explore",
        )

    try:
        if work_dir.exists():
            shutil.rmtree(work_dir)

        _run(["gh", "auth", "setup-git"], capture_output=True)
        clone = _run(
            ["git", "clone", "--depth", "1",
             f"https://github.com/{REPO}.git", str(work_dir)],
            capture_output=True,
        )
        if clone.returncode != 0:
            print(f"[cai explore] git clone failed:\n{clone.stderr}", file=sys.stderr)
            rollback()
            log_run("explore", repo=REPO, issue=issue_number, result="clone_failed", exit=1)
            return 1

        user_message = (
            _work_directory_block(work_dir)
            + "\n"
            + _build_issue_block(issue)
        )
        print(f"[cai explore] running cai-explore subagent for {work_dir}", flush=True)
        result = _run_claude_p(
            ["claude", "-p", "--agent", "cai-explore",
             "--dangerously-skip-permissions",
             "--add-dir", str(work_dir)],
            category="explore",
            agent="cai-explore",
            input=user_message,
            cwd="/app",
            timeout=1800,  # 30 min cap
        )
        if result.stdout:
            print(result.stdout, flush=True)

        if result.returncode != 0:
            print(
                f"[cai explore] subagent failed (exit {result.returncode}):\n"
                f"{result.stderr}",
                file=sys.stderr,
            )
            rollback()
            dur = f"{int(time.monotonic() - t0)}s"
            log_run("explore", repo=REPO, issue=issue_number,
                    duration=dur, result="agent_failed", exit=result.returncode)
            return result.returncode

        stdout = result.stdout or ""

        # Outcome 1: Exploration Findings
        findings_pos = stdout.find("## Exploration Findings")
        if findings_pos != -1:
            findings_block = stdout[findings_pos:].strip()
            rec_match = re.search(
                r"###\s*Recommendation\s*\n+\s*(\S+)",
                findings_block,
            )
            recommendation = rec_match.group(1).strip() if rec_match else ""

            if recommendation in ("close_documented", "close_wont_do"):
                _run(
                    ["gh", "issue", "comment", str(issue_number),
                     "--repo", REPO,
                     "--body", f"## Exploration findings\n\n{findings_block}\n\n---\n_Closed by `cai explore`._"],
                    capture_output=True,
                )
                _run(
                    ["gh", "issue", "close", str(issue_number),
                     "--repo", REPO],
                    capture_output=True,
                )
                _set_labels(issue_number, remove=[LABEL_IN_PROGRESS], log_prefix="cai explore")
                dur = f"{int(time.monotonic() - t0)}s"
                print(f"[cai explore] #{issue_number} closed ({recommendation}) in {dur}", flush=True)
                log_run("explore", repo=REPO, issue=issue_number,
                        duration=dur, result=recommendation, exit=0)
                return 0

            elif recommendation == "refine_and_retry":
                original_body = issue.get("body") or "(no body)"
                quoted_original = "\n".join(f"> {line}" for line in original_body.splitlines())
                new_body = (
                    f"{findings_block}\n\n"
                    f"---\n\n"
                    f"> **Original issue text:**\n>\n"
                    f"{quoted_original}\n"
                )
                _run(
                    ["gh", "issue", "edit", str(issue_number),
                     "--repo", REPO, "--body", new_body],
                    capture_output=True,
                )
                _set_labels(
                    issue_number,
                    add=[LABEL_RAISED],
                    remove=[LABEL_IN_PROGRESS],
                    log_prefix="cai explore",
                )
                dur = f"{int(time.monotonic() - t0)}s"
                print(f"[cai explore] #{issue_number} refined-and-retried in {dur}", flush=True)
                log_run("explore", repo=REPO, issue=issue_number,
                        duration=dur, result="refine_and_retry", exit=0)
                return 0

            # Unrecognised recommendation — fall through to no_marker

        # Outcome 2: Refined Issue
        refined_pos = stdout.find("## Refined Issue")
        if refined_pos != -1:
            refined_body = stdout[refined_pos:].strip()
            original_body = issue.get("body") or "(no body)"
            quoted_original = "\n".join(f"> {line}" for line in original_body.splitlines())
            new_body = (
                f"{refined_body}\n\n"
                f"---\n\n"
                f"> **Original issue text:**\n>\n"
                f"{quoted_original}\n"
            )
            _run(
                ["gh", "issue", "edit", str(issue_number),
                 "--repo", REPO, "--body", new_body],
                capture_output=True,
            )
            _set_labels(
                issue_number,
                add=[LABEL_REFINED],
                remove=[LABEL_IN_PROGRESS],
                log_prefix="cai explore",
            )
            dur = f"{int(time.monotonic() - t0)}s"
            print(f"[cai explore] #{issue_number} refined and handed to fix in {dur}", flush=True)
            log_run("explore", repo=REPO, issue=issue_number,
                    duration=dur, result="refined", exit=0)
            return 0

        # Outcome 3: Exploration Blocked
        blocked_pos = stdout.find("## Exploration Blocked")
        if blocked_pos != -1:
            blocked_block = stdout[blocked_pos:].strip()
            _run(
                ["gh", "issue", "comment", str(issue_number),
                 "--repo", REPO,
                 "--body", f"{blocked_block}\n\n---\n_Escalated by `cai explore`._"],
                capture_output=True,
            )
            _set_labels(
                issue_number,
                add=[LABEL_PR_NEEDS_HUMAN],
                remove=[LABEL_IN_PROGRESS],
                log_prefix="cai explore",
            )
            dur = f"{int(time.monotonic() - t0)}s"
            print(f"[cai explore] #{issue_number} blocked/escalated in {dur}", flush=True)
            log_run("explore", repo=REPO, issue=issue_number,
                    duration=dur, result="blocked", exit=0)
            return 0

        # No recognised marker — rollback to :needs-exploration.
        rollback()
        dur = f"{int(time.monotonic() - t0)}s"
        print(f"[cai explore] #{issue_number} no outcome marker; rolling back in {dur}", flush=True)
        log_run("explore", repo=REPO, issue=issue_number,
                duration=dur, result="no_marker", exit=0)
        return 0

    except Exception as exc:
        print(f"[cai explore] unexpected error: {exc}", file=sys.stderr)
        rollback()
        log_run("explore", repo=REPO, issue=issue_number, result="error", exit=1)
        return 1
    finally:
        _clear_active_job()
        if work_dir.exists():
            shutil.rmtree(work_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Cycle (full pipeline without analyze)
# ---------------------------------------------------------------------------

def _run_step(name: str, handler, args) -> int:
    """Run a single cycle step, catching exceptions."""
    print(f"\n[cai cycle] === {name} ===", flush=True)
    try:
        return handler(args)
    except Exception as exc:
        print(f"[cai cycle] {name} raised {exc!r}", file=sys.stderr, flush=True)
        return 1


def _drain_pending_prs(args) -> dict:
    """Revise → review-pr → review-docs → merge all pending PRs. Returns step results."""
    results: dict[str, int] = {}
    results["revise"] = _run_step("revise", cmd_revise, args)
    results["review-pr"] = _run_step("review-pr", cmd_review_pr, args)
    results["review-docs"] = _run_step("review-docs", cmd_review_docs, args)
    results["merge"] = _run_step("merge", cmd_merge, args)
    return results


_CYCLE_LOCK_PATH = "/tmp/cai-cycle.lock"


def cmd_cycle(args) -> int:
    """Continuously fix issues and merge PRs until nothing is left to do.

    Flow:
      1. verify + confirm  (sync label state)
      1.5. recover stale locks (:in-progress / :revising)
      2. drain pending PRs (revise → review-pr → review-docs → merge)
      2.5. refine one :raised issue
      2.6. plan one :refined issue (plan-select pipeline → store plan → :planned)
      3. loop: verify → fix/spike/explore → drain → refine → repeat
      4. final confirm

    A non-blocking flock on `_CYCLE_LOCK_PATH` ensures at most one
    cycle runs at a time — supercronic is happy to fire overlapping
    cycles if a previous one is still running, and we want serial
    issue processing (one full cycle per issue from refine through
    merge before starting the next).
    """
    lock_fd = os.open(_CYCLE_LOCK_PATH, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(lock_fd)
        print("[cai cycle] another cycle is already running; skipping this tick",
              flush=True)
        return 0

    try:
        return _cmd_cycle_inner(args)
    finally:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        finally:
            os.close(lock_fd)


def _cmd_cycle_inner(args) -> int:
    print("[cai cycle] starting continuous cycle", flush=True)
    t0 = time.monotonic()
    iteration = 0
    all_results: dict[str, int] = {}
    had_failure = False

    # --- Phase 1: verify + confirm (initial state sync) -----------------
    for step_name, handler in [("verify", cmd_verify), ("confirm", cmd_confirm)]:
        rc = _run_step(step_name, handler, args)
        all_results[step_name] = rc
        if rc != 0:
            had_failure = True

    # --- Phase 1.5: recover stale locks ----------------------------------
    rolled_back = _rollback_stale_in_progress(immediate=True)
    if rolled_back:
        nums = ", ".join(f"#{i['number']}" for i in rolled_back)
        print(f"[cai cycle] recovered {len(rolled_back)} stale lock(s): {nums}",
              flush=True)

    # --- Phase 1.6: ingest unlabeled issues --------------------------------
    ingested = _ingest_unlabeled_issues()
    if ingested:
        nums = ", ".join(f"#{i['number']}" for i in ingested)
        print(f"[cai cycle] ingested {len(ingested)} unlabeled issue(s): {nums}",
              flush=True)

    # --- Phase 2: drain any already-pending PRs -------------------------
    print("\n[cai cycle] draining pending PRs before starting fix loop",
          flush=True)
    pr_results = _drain_pending_prs(args)
    all_results.update(pr_results)
    if any(v != 0 for v in pr_results.values()):
        had_failure = True

    # --- Phase 2.5: refine one :raised issue ------------------------------
    rc = _run_step("refine", cmd_refine, args)
    all_results["refine"] = rc
    if rc != 0:
        had_failure = True

    # --- Phase 2.6: plan one :refined issue --------------------------------
    rc = _run_step("plan", cmd_plan, args)
    all_results["plan"] = rc
    if rc != 0:
        had_failure = True

    # --- Phase 3: fix loop — pick → fix → drain → refine → repeat ------
    # The loop also handles pr-open issues that need further
    # revise/review/merge passes, not just new fix targets.
    drain_only_passes = 0
    _MAX_DRAIN_ONLY_PASSES = 3  # cap drain-only iterations to avoid infinite loops

    while True:
        iteration += 1
        print(f"\n[cai cycle] ---- iteration {iteration} ----", flush=True)

        # Sync labels before each fix attempt so we see freshly-merged PRs.
        _run_step("verify", cmd_verify, args)

        has_fix_target = _select_fix_target() is not None

        # Check for pr-open issues that still need drain passes.
        has_pending_prs = False
        try:
            pending = _gh_json([
                "issue", "list",
                "--repo", REPO,
                "--label", LABEL_PR_OPEN,
                "--state", "open",
                "--json", "number",
                "--limit", "1",
            ]) or []
            has_pending_prs = len(pending) > 0
        except subprocess.CalledProcessError:
            pass

        # Check for :needs-spike issues.
        has_spike = False
        if not has_fix_target:
            try:
                spike_issues = _gh_json([
                    "issue", "list",
                    "--repo", REPO,
                    "--label", LABEL_NEEDS_SPIKE,
                    "--state", "open",
                    "--json", "number",
                    "--limit", "1",
                ]) or []
                has_spike = len(spike_issues) > 0
            except subprocess.CalledProcessError:
                pass

        # Check for :needs-exploration issues.
        has_exploration = False
        if not has_fix_target:
            try:
                exploration_issues = _gh_json([
                    "issue", "list",
                    "--repo", REPO,
                    "--label", LABEL_NEEDS_EXPLORATION,
                    "--state", "open",
                    "--json", "number",
                    "--limit", "1",
                ]) or []
                has_exploration = len(exploration_issues) > 0
            except subprocess.CalledProcessError:
                pass

        # Check for :raised or human:submitted issues that still need refining.
        has_raised = False
        if not has_fix_target and not has_pending_prs and not has_spike and not has_exploration:
            try:
                raised = _gh_json([
                    "issue", "list",
                    "--repo", REPO,
                    "--label", LABEL_RAISED,
                    "--state", "open",
                    "--json", "number",
                    "--limit", "1",
                ]) or []
                has_raised = len(raised) > 0
            except subprocess.CalledProcessError:
                pass
            if not has_raised:
                try:
                    human_submitted = _gh_json([
                        "issue", "list",
                        "--repo", REPO,
                        "--label", LABEL_HUMAN_SUBMITTED,
                        "--state", "open",
                        "--json", "number",
                        "--limit", "1",
                    ]) or []
                    has_raised = len(human_submitted) > 0
                except subprocess.CalledProcessError:
                    pass

        if not has_fix_target and not has_pending_prs and not has_spike and not has_exploration and not has_raised:
            print("[cai cycle] no eligible issues and no pending PRs; exiting loop",
                  flush=True)
            break

        if has_pending_prs:
            drain_only_passes += 1
            if drain_only_passes > _MAX_DRAIN_ONLY_PASSES:
                print(
                    f"[cai cycle] {drain_only_passes - 1} drain-only passes with PRs "
                    "still open; exiting (PRs likely need human attention)",
                    flush=True,
                )
                break
            print(
                f"[cai cycle] pending PR(s) still open; "
                f"draining (pass {drain_only_passes}/{_MAX_DRAIN_ONLY_PASSES})",
                flush=True,
            )
        else:
            drain_only_passes = 0  # reset when no pending PRs

        if has_fix_target and not has_pending_prs:
            rc = _run_step("fix", cmd_fix, args)
            key = f"fix.{iteration}"
            all_results[key] = rc

            if rc != 0:
                had_failure = True
                # fix failed (error) — stop looping.
                print("[cai cycle] fix step failed; stopping loop", flush=True)
                break
        elif has_fix_target and has_pending_prs:
            print(
                "[cai cycle] fix target available but skipping — draining pending PR(s) first",
                flush=True,
            )

        # Run spike if no fix target but :needs-spike issues exist.
        # Spike outcomes feed back: refine_and_retry → :raised,
        # refined → :refined, blocked → :needs-human, close → done.
        if not has_fix_target and has_spike:
            rc = _run_step("spike", cmd_spike, args)
            all_results[f"spike.{iteration}"] = rc
            if rc != 0:
                had_failure = True

        # Run explore if no fix target but :needs-exploration issues exist.
        # Explore outcomes feed back: refine_and_retry → :raised,
        # refined → :refined, blocked → :needs-human, close → done.
        if not has_fix_target and has_exploration:
            rc = _run_step("explore", cmd_explore, args)
            all_results[f"explore.{iteration}"] = rc
            if rc != 0:
                had_failure = True

        # Drain pending PRs (from fix or pre-existing).
        pr_results = _drain_pending_prs(args)
        for step, step_rc in pr_results.items():
            all_results[f"{step}.{iteration}"] = step_rc
            if step_rc != 0:
                had_failure = True

        # Refine one more :raised issue so the next iteration has
        # something to fix.
        rc = _run_step("refine", cmd_refine, args)
        all_results[f"refine.{iteration}"] = rc

    # --- Phase 4: final confirm -----------------------------------------
    rc = _run_step("confirm-final", cmd_confirm, args)
    all_results["confirm-final"] = rc
    if rc != 0:
        had_failure = True

    dur = f"{time.monotonic() - t0:.1f}s"
    summary = " ".join(f"{k}={v}" for k, v in all_results.items())
    print(f"\n[cai cycle] done in {dur} ({iteration} iterations) — {summary}",
          flush=True)
    log_run("cycle", repo=REPO, results=summary, iterations=iteration,
            duration=dur, exit=1 if had_failure else 0)
    return 1 if had_failure else 0


def cmd_health_report(args) -> int:
    """Automated pipeline health report with anomaly detection.

    Gathers cost trends, issue throughput, pipeline stalls, and fix
    quality metrics from existing data sources (_load_cost_log,
    _gh_json), formats them as GitHub-flavored markdown with
    traffic-light anomaly indicators, and optionally posts the report
    as a GitHub issue.

    With --dry-run the report is printed to stdout without posting.
    """
    t0 = time.monotonic()
    now_ts = datetime.now(timezone.utc).timestamp()
    sections: list[str] = []
    anomalies: list[str] = []

    # ------------------------------------------------------------------ #
    # 1. Cost Trends                                                       #
    # ------------------------------------------------------------------ #
    cost_status = "🟢"
    try:
        rows_14d = _load_cost_log(days=14)
        boundary = now_ts - 7 * 86400
        last_7d = [r for r in rows_14d if _row_ts(r) >= boundary]
        prior_7d = [r for r in rows_14d if _row_ts(r) < boundary]

        def _cost(r: dict) -> float:
            try:
                return float(r.get("cost_usd") or 0.0)
            except (TypeError, ValueError):
                return 0.0

        last_7d_total = sum(_cost(r) for r in last_7d)
        prior_7d_total = sum(_cost(r) for r in prior_7d)

        if not rows_14d:
            cost_section_body = "_No cost data available._"
        elif not prior_7d:
            cost_section_body = (
                f"- Last 7d total: **${last_7d_total:.4f}**\n"
                "- Prior 7d: _Insufficient history (need ≥2 weeks of cost data)_"
            )
        else:
            wow_pct = (
                ((last_7d_total - prior_7d_total) / prior_7d_total * 100)
                if prior_7d_total > 0 else 0.0
            )
            if prior_7d_total > 0 and last_7d_total > 1.5 * prior_7d_total:
                cost_status = "🔴"
                anomalies.append(
                    f"🔴 **Cost spike**: last-7d ${last_7d_total:.4f} is "
                    f"{wow_pct:+.1f}% vs prior-7d ${prior_7d_total:.4f}"
                )

            # Per-agent breakdown
            def _by_agent(rows: list[dict]) -> dict:
                agg: dict[str, float] = {}
                for r in rows:
                    agent = r.get("agent") or "(none)"
                    agg[agent] = agg.get(agent, 0.0) + _cost(r)
                return agg

            last_by_agent = _by_agent(last_7d)
            prior_by_agent = _by_agent(prior_7d)
            all_agents = sorted(
                set(last_by_agent) | set(prior_by_agent),
                key=lambda a: -last_by_agent.get(a, 0.0),
            )

            rows_md = []
            for agent in all_agents:
                l = last_by_agent.get(agent, 0.0)
                p = prior_by_agent.get(agent, 0.0)
                delta = ((l - p) / p * 100) if p > 0 else float("nan")
                delta_str = f"{delta:+.1f}%" if p > 0 else "n/a"
                rows_md.append(
                    f"| `{agent}` | ${l:.4f} | ${p:.4f} | {delta_str} |"
                )

            agent_table = (
                "| Agent | Last 7d | Prior 7d | WoW Δ |\n"
                "|-------|---------|----------|-------|\n"
                + "\n".join(rows_md)
            )
            wow_line = f"{wow_pct:+.1f}%"
            cost_section_body = (
                f"- **Last 7d total**: ${last_7d_total:.4f}\n"
                f"- **Prior 7d total**: ${prior_7d_total:.4f}\n"
                f"- **WoW Δ**: {wow_line}\n\n"
                f"{agent_table}"
            )
    except Exception as exc:
        cost_section_body = f"⚠️ Data unavailable ({exc})"
        cost_status = "🟡"

    sections.append(
        f"## {cost_status} Cost Trends\n\n{cost_section_body}"
    )

    # ------------------------------------------------------------------ #
    # 2. Issue Throughput                                                  #
    # ------------------------------------------------------------------ #
    throughput_status = "🟢"
    label_states = [
        ("raised", LABEL_RAISED),
        ("refined", LABEL_REFINED),
        ("planned", LABEL_PLANNED),
        ("plan-approved", LABEL_PLAN_APPROVED),
        ("in-progress", LABEL_IN_PROGRESS),
        ("pr-open", LABEL_PR_OPEN),
        ("merged", LABEL_MERGED),
        ("no-action", LABEL_NO_ACTION),
        ("revising", LABEL_REVISING),
    ]
    counts: dict[str, int] = {}
    try:
        for name, label in label_states:
            items = _gh_json(
                ["issue", "list", "--repo", REPO,
                 "--label", label, "--state", "open",
                 "--json", "number", "--limit", "200"]
            )
            counts[name] = len(items) if items else 0
    except Exception as exc:
        throughput_status = "🟡"
        counts = {name: -1 for name, _ in label_states}

    header_row = "| " + " | ".join(n for n, _ in label_states) + " |"
    sep_row = "|" + "|".join("---" for _ in label_states) + "|"
    val_row = "| " + " | ".join(
        str(counts.get(n, "?")) for n, _ in label_states
    ) + " |"
    throughput_table = "\n".join([header_row, sep_row, val_row])

    sections.append(
        f"## {throughput_status} Issue Queue\n\n{throughput_table}"
    )

    # ------------------------------------------------------------------ #
    # 3. Pipeline Stalls                                                   #
    # ------------------------------------------------------------------ #
    stall_status = "🟢"
    stall_lines: list[str] = []

    def _parse_gh_ts(ts_str: str) -> float:
        """Parse a GitHub updatedAt/createdAt timestamp to Unix time."""
        if not ts_str:
            return 0.0
        # Strip fractional seconds if present
        ts_str = ts_str.split(".")[0].rstrip("Z") + "Z"
        try:
            return datetime.strptime(
                ts_str, "%Y-%m-%dT%H:%M:%SZ",
            ).replace(tzinfo=timezone.utc).timestamp()
        except ValueError:
            return 0.0

    # In-progress stalls (>2h since update)
    try:
        ip_items = _gh_json(
            ["issue", "list", "--repo", REPO,
             "--label", LABEL_IN_PROGRESS, "--state", "open",
             "--json", "number,title,updatedAt", "--limit", "200"]
        ) or []
        stalled_ip = [
            i for i in ip_items
            if now_ts - _parse_gh_ts(i.get("updatedAt", "")) > 7200
        ]
        if stalled_ip:
            stall_status = "🟡"
            shown = stalled_ip[:10]
            for i in shown:
                stall_lines.append(
                    f"- `:in-progress` stall >2h: #{i['number']} {i.get('title', '')[:60]}"
                )
            remainder = len(stalled_ip) - len(shown)
            if remainder > 0:
                stall_lines.append(f"  _…and {remainder} more_")
    except Exception as exc:
        stall_lines.append(f"⚠️ `:in-progress` data unavailable ({exc})")

    # Refined stalls (>5 days since update)
    try:
        ref_items = _gh_json(
            ["issue", "list", "--repo", REPO,
             "--label", LABEL_REFINED, "--state", "open",
             "--json", "number,title,updatedAt", "--limit", "200"]
        ) or []
        stalled_ref = [
            i for i in ref_items
            if now_ts - _parse_gh_ts(i.get("updatedAt", "")) > 5 * 86400
        ]
        if stalled_ref:
            if stall_status == "🟢":
                stall_status = "🟡"
            shown = stalled_ref[:10]
            for i in shown:
                stall_lines.append(
                    f"- `:refined` stall >5d: #{i['number']} {i.get('title', '')[:60]}"
                )
            remainder = len(stalled_ref) - len(shown)
            if remainder > 0:
                stall_lines.append(f"  _…and {remainder} more_")
    except Exception as exc:
        stall_lines.append(f"⚠️ `:refined` data unavailable ({exc})")

    # Merged stalls (>14 days since update — confirm not run)
    try:
        merged_items = _gh_json(
            ["issue", "list", "--repo", REPO,
             "--label", LABEL_MERGED, "--state", "open",
             "--json", "number,title,updatedAt", "--limit", "200"]
        ) or []
        stalled_merged = [
            i for i in merged_items
            if now_ts - _parse_gh_ts(i.get("updatedAt", "")) > 14 * 86400
        ]
        if len(stalled_merged) > 5:
            stall_status = "🟡"
            anomalies.append(
                f"🟡 **Confirm backlog**: {len(stalled_merged)} issues in "
                "`:merged` state >14d without confirmation"
            )
        if stalled_merged:
            shown = stalled_merged[:10]
            for i in shown:
                stall_lines.append(
                    f"- `:merged` stall >14d: #{i['number']} {i.get('title', '')[:60]}"
                )
            remainder = len(stalled_merged) - len(shown)
            if remainder > 0:
                stall_lines.append(f"  _…and {remainder} more_")
    except Exception as exc:
        stall_lines.append(f"⚠️ `:merged` data unavailable ({exc})")

    # Complete pipeline stall detection — no PRs opened in >72h?
    try:
        all_prs = _gh_json(
            ["pr", "list", "--repo", REPO, "--state", "all",
             "--json", "number,createdAt", "--limit", "200"]
        ) or []
        recent_prs = [
            pr for pr in all_prs
            if now_ts - _parse_gh_ts(pr.get("createdAt", "")) < 72 * 3600
        ]
        raised_count = counts.get("raised", 0)
        refined_count = counts.get("refined", 0)
        if not recent_prs and (raised_count + refined_count) > 0:
            stall_status = "🔴"
            anomalies.append(
                "🔴 **Pipeline stall**: no PRs opened in the last 72h, "
                f"but {raised_count + refined_count} issues are queued "
                "(`:raised` + `:refined`)"
            )
            stall_lines.append(
                "⚠️ No PRs opened in the last 72h — pipeline may be stalled"
            )
    except Exception as exc:
        stall_lines.append(f"⚠️ PR stall check unavailable ({exc})")

    stall_body = "\n".join(stall_lines) if stall_lines else "_No stalls detected._"
    sections.append(
        f"## {stall_status} Pipeline Stalls\n\n{stall_body}"
    )

    # ------------------------------------------------------------------ #
    # 4. Fix Quality (last 7 days)                                        #
    # ------------------------------------------------------------------ #
    quality_status = "🟢"
    try:
        prs = _gh_json(
            ["pr", "list", "--repo", REPO, "--state", "all",
             "--json", "number,state,mergedAt,closedAt,createdAt",
             "--limit", "200"]
        ) or []
        # Filter to PRs created in the last 7 days
        recent = [
            pr for pr in prs
            if now_ts - _parse_gh_ts(pr.get("createdAt", "")) <= 7 * 86400
        ]
        merged_prs = [p for p in recent if p.get("mergedAt")]
        # gh CLI returns "CLOSED" or "OPEN" (uppercase) for state
        closed_no_merge = [
            p for p in recent
            if p.get("state", "").upper() == "CLOSED" and not p.get("mergedAt")
        ]
        open_prs = [
            p for p in recent
            if p.get("state", "").upper() == "OPEN"
        ]

        denom = len(merged_prs) + len(closed_no_merge)
        if denom > 0:
            rate = len(merged_prs) / denom * 100
            rate_str = f"{rate:.1f}%"
            if rate < 60:
                quality_status = "🟡"
                anomalies.append(
                    f"🟡 **Fix success rate drop**: {rate_str} merge rate "
                    f"({len(merged_prs)} merged / {denom} resolved)"
                )
        else:
            rate_str = "n/a (no resolved PRs)"

        quality_body = (
            f"| Merged | Closed w/o merge | Still open | Merge rate |\n"
            f"|--------|-----------------|------------|------------|\n"
            f"| {len(merged_prs)} | {len(closed_no_merge)} | {len(open_prs)} | {rate_str} |"
        )
    except Exception as exc:
        quality_body = f"⚠️ Data unavailable ({exc})"
        quality_status = "🟡"

    sections.append(
        f"## {quality_status} Fix Quality (last 7d)\n\n{quality_body}"
    )

    # ------------------------------------------------------------------ #
    # 5. Assemble the report                                               #
    # ------------------------------------------------------------------ #
    overall = "🟢 healthy"
    if any("🔴" in a for a in anomalies):
        overall = "🔴 critical"
    elif any("🟡" in a for a in anomalies):
        overall = "🟡 warning"

    run_date = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    if anomalies:
        anomaly_block = (
            "### Anomalies\n\n"
            + "\n".join(f"- {a}" for a in anomalies)
        )
    else:
        anomaly_block = "### Anomalies\n\n_None detected._"

    narrative = (
        f"Pipeline status as of **{run_date}**: **{overall}**. "
        f"{len(anomalies)} anomaly(ies) detected across cost, throughput, "
        f"stalls, and fix quality."
    )

    report = "\n\n".join(
        [
            f"# 🤖 Pipeline Health Report — {run_date}",
            narrative,
            anomaly_block,
        ]
        + sections
    )

    # ------------------------------------------------------------------ #
    # 6. Post or print                                                     #
    # ------------------------------------------------------------------ #
    if args.dry_run:
        print(report)
        dur = f"{int(time.monotonic() - t0)}s"
        log_run("health-report", repo=REPO, result="dry-run", duration=dur, exit=0)
        return 0

    result = _run(
        ["gh", "issue", "create",
         "--repo", REPO,
         "--title", f"Health Report — {datetime.now(timezone.utc).strftime('%Y-%m-%d')}",
         "--body", report,
         "--label", "health-report"],
        capture_output=True,
    )
    if result.returncode == 0:
        url = result.stdout.strip()
        print(f"[cai health-report] created report issue: {url}", flush=True)
    else:
        print(
            f"[cai health-report] failed to create issue: {result.stderr}",
            file=sys.stderr, flush=True,
        )

    dur = f"{int(time.monotonic() - t0)}s"
    log_run("health-report", repo=REPO, duration=dur, exit=result.returncode)
    return result.returncode


def cmd_check_workflows(args) -> int:
    """Check GitHub Actions for recent workflow failures and raise findings."""
    print("[cai check-workflows] running workflow check", flush=True)
    t0 = time.monotonic()

    # 1. Fetch recent failed runs from GitHub Actions.
    try:
        runs = _gh_json([
            "run", "list",
            "--repo", REPO,
            "--status", "failure",
            "--json", "databaseId,name,headBranch,conclusion,createdAt,url,event,headSha",
            "--limit", "20",
        ]) or []
    except subprocess.CalledProcessError as exc:
        print(
            f"[cai check-workflows] gh run list failed: {exc}",
            file=sys.stderr, flush=True,
        )
        dur = f"{int(time.monotonic() - t0)}s"
        log_run("check-workflows", repo=REPO, result="gh_failed", duration=dur, exit=1)
        return 1

    # 2. Filter: last 24 hours only, skip bot branches.
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    recent_runs = []
    for r in runs:
        try:
            created = datetime.fromisoformat(r["createdAt"].replace("Z", "+00:00"))
        except (KeyError, ValueError):
            continue
        if created < cutoff:
            continue
        if r.get("headBranch", "").startswith("auto-improve/"):
            continue
        recent_runs.append(r)

    if not recent_runs:
        dur = f"{int(time.monotonic() - t0)}s"
        print("[cai check-workflows] no recent failures found", flush=True)
        log_run("check-workflows", repo=REPO, failures=0, duration=dur, exit=0)
        return 0

    # 3. Fetch existing open check-workflows issues for dedup context.
    try:
        existing = _gh_json([
            "issue", "list",
            "--repo", REPO,
            "--label", "check-workflows",
            "--state", "open",
            "--json", "number,title,body",
            "--limit", "50",
        ]) or []
    except subprocess.CalledProcessError:
        existing = []

    # 4. Build user message.
    runs_section = "## Recent failed workflow runs\n\n"
    runs_section += json.dumps(recent_runs, indent=2) + "\n\n"

    existing_section = "## Existing open check-workflows issues\n\n"
    if existing:
        for iss in existing:
            existing_section += f"- #{iss['number']}: {iss['title']}\n"
            body_snippet = (iss.get("body") or "")[:300]
            existing_section += f"  Body: {body_snippet}\n"
    else:
        existing_section += "(none)\n"

    user_message = runs_section + existing_section

    # 5. Invoke the declared cai-check-workflows agent.
    print(
        f"[cai check-workflows] running agent on {len(recent_runs)} failure(s)",
        flush=True,
    )
    _write_active_job("check-workflows", "none", None)
    try:
        agent = _run_claude_p(
            ["claude", "-p", "--agent", "cai-check-workflows",
             "--max-turns", "3",
             "--permission-mode", "acceptEdits"],
            category="check-workflows",
            agent="cai-check-workflows",
            input=user_message,
            cwd="/app",
        )
    finally:
        _clear_active_job()
    if agent.stdout:
        print(agent.stdout, flush=True)
    if agent.returncode != 0:
        print(
            f"[cai check-workflows] agent failed (exit {agent.returncode}):\n"
            f"{agent.stderr}",
            file=sys.stderr, flush=True,
        )
        dur = f"{int(time.monotonic() - t0)}s"
        log_run("check-workflows", repo=REPO, result="agent_failed",
                duration=dur, exit=agent.returncode)
        return agent.returncode

    # 6. Publish findings via publish.py with check-workflows namespace.
    print("[cai check-workflows] publishing findings", flush=True)
    published = _run(
        ["python", str(PUBLISH_SCRIPT), "--namespace", "check-workflows"],
        input=agent.stdout,
    )

    dur = f"{int(time.monotonic() - t0)}s"
    log_run("check-workflows", repo=REPO, failures=len(recent_runs),
            duration=dur, exit=published.returncode)
    return published.returncode


def cmd_test(args) -> int:
    """Run the project test suite."""
    result = _run(
        [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"],
        cwd=str(Path(__file__).resolve().parent),
    )
    return result.returncode


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(prog="cai")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="Smoke test if no transcripts exist")
    sub.add_parser("analyze", help="Run the analyzer + publish findings")

    fix_parser = sub.add_parser("fix", help="Run the fix subagent")
    fix_parser.add_argument(
        "--issue", type=int, default=None,
        help="Target a specific issue number instead of using automatic scoring-based selection",
    )

    revise_parser = sub.add_parser("revise", help="Iterate on open PRs based on review comments")
    revise_parser.add_argument(
        "--pr", type=int, default=None,
        help="Target a specific PR number instead of using queue-based selection",
    )
    sub.add_parser("verify", help="Update labels based on PR merge state")
    sub.add_parser("audit", help="Run the queue/PR consistency audit")
    sub.add_parser(
        "audit-triage",
        help="Autonomously resolve audit:raised findings (no PRs)",
    )
    sub.add_parser("code-audit", help="Audit repo source code for inconsistencies")
    sub.add_parser("propose", help="Weekly creative improvement proposal")
    sub.add_parser("update-check", help="Check Claude Code releases for workspace improvements")
    confirm_parser = sub.add_parser("confirm", help="Verify merged issues are actually solved")
    confirm_parser.add_argument(
        "--issue", type=int, default=None,
        help="Target a specific issue number instead of using queue-based selection",
    )
    review_pr_parser = sub.add_parser("review-pr", help="Pre-merge consistency review of open PRs")
    review_pr_parser.add_argument(
        "--pr", type=int, default=None,
        help="Target a specific PR number instead of using queue-based selection",
    )
    review_docs_parser = sub.add_parser("review-docs", help="Pre-merge documentation review of open PRs")
    review_docs_parser.add_argument(
        "--pr", type=int, default=None,
        help="Target a specific PR number instead of using queue-based selection",
    )
    merge_parser = sub.add_parser("merge", help="Confidence-gated auto-merge for bot PRs")
    merge_parser.add_argument(
        "--pr", type=int, default=None,
        help="Target a specific PR number instead of using queue-based selection",
    )
    refine_parser = sub.add_parser("refine", help="Refine human-filed issues into structured plans")
    refine_parser.add_argument(
        "--issue", type=int, default=None,
        help="Target a specific issue number instead of using queue-based selection",
    )
    plan_parser = sub.add_parser("plan", help="Run plan-select pipeline on a :refined issue")
    plan_parser.add_argument(
        "--issue", type=int, default=None,
        help="Target a specific issue number instead of using queue-based selection",
    )
    spike_parser = sub.add_parser("spike", help="Run the spike agent on :needs-spike issues")
    spike_parser.add_argument(
        "--issue", type=int, default=None,
        help="Target a specific issue number instead of using queue-based selection",
    )
    explore_parser = sub.add_parser("explore", help="Autonomous exploration/benchmarking of :needs-exploration issues")
    explore_parser.add_argument(
        "--issue", type=int, default=None,
        help="Target a specific issue number instead of using queue-based selection",
    )
    sub.add_parser("cost-optimize", help="Weekly cost-reduction proposal or evaluation")
    sub.add_parser("check-workflows", help="Check GitHub Actions for recent workflow failures and raise findings")
    sub.add_parser("cycle", help="Full cycle: verify, fix, revise, review-pr, review-docs, merge, confirm")
    sub.add_parser("test", help="Run the project test suite")

    cost_parser = sub.add_parser(
        "cost-report",
        help="Print a human-readable cost report from the cost log",
    )
    cost_parser.add_argument(
        "--days", type=int, default=7,
        help="Window in days to include (default: 7)",
    )
    cost_parser.add_argument(
        "--top", type=int, default=10,
        help="Number of most-expensive invocations to list (default: 10)",
    )
    cost_parser.add_argument(
        "--by", choices=["category", "agent", "day"], default="category",
        help="Aggregation grouping (default: category)",
    )

    health_parser = sub.add_parser(
        "health-report",
        help="Automated pipeline health report with anomaly detection",
    )
    health_parser.add_argument(
        "--dry-run", action="store_true", default=False,
        help="Print report to stdout without posting a GitHub issue",
    )

    args = parser.parse_args()

    auth_rc = check_gh_auth()
    if auth_rc != 0:
        return auth_rc

    auth_rc = check_claude_auth()
    if auth_rc != 0:
        return auth_rc

    ensure_all_labels()

    handlers = {
        "init": cmd_init,
        "analyze": cmd_analyze,
        "fix": cmd_fix,
        "revise": cmd_revise,
        "verify": cmd_verify,
        "audit": cmd_audit,
        "audit-triage": cmd_audit_triage,
        "code-audit": cmd_code_audit,
        "propose": cmd_propose,
        "update-check": cmd_update_check,
        "confirm": cmd_confirm,
        "review-pr": cmd_review_pr,
        "review-docs": cmd_review_docs,
        "merge": cmd_merge,
        "refine": cmd_refine,
        "plan": cmd_plan,
        "spike": cmd_spike,
        "explore": cmd_explore,
        "cycle": cmd_cycle,
        "cost-report": cmd_cost_report,
        "health-report": cmd_health_report,
        "cost-optimize": cmd_cost_optimize,
        "check-workflows": cmd_check_workflows,
        "test": cmd_test,
    }
    return handlers[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
