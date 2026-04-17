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
                            the highest scorer labelled
                            `auto-improve:plan-approved` (reached
                            either automatically on a HIGH-confidence
                            plan or via admin resume from
                            `:human-needed`), run a cheap Haiku
                            pre-screen to classify the issue;
                            ambiguous issues are returned to their origin
                            label without cloning, while spike-shaped
                            issues are routed to :human-needed; if
                            actionable, lock it
                            via the `:in-progress` label, clone the repo
                            into /tmp, load the stored implementation
                            plan from the issue body (written by `cai
                            plan` between `<!-- cai-plan-start/end -->`
                            markers) if present, then run the fix
                            subagent (full tool permissions) with that
                            plan, and open a PR if the agent produced a
                            diff. Does NOT re-plan — planning is a
                            separate `cai plan` step. Rolls back the
                            label on empty diff or any failure.

    python cai.py verify    Mechanical, no-LLM. Walk issues with
                            `:pr-open`, find their linked PR by `Refs`
                            search, and transition the label:
                            merged → `:merged`,
                            closed-unmerged → `:refined`,
                            no-linked-PR → `:raised`.

    python cai.py audit     Periodic queue/PR consistency audit.
                            Deterministically rolls back stale
                            `:in-progress` (>6h), `:revising` (>1h),
                            and `:applying` (>2h) locks; unsticks stale
                            `:no-action` issues; flags stale `:merged`
                            issues; recovers `:pr-open` issues with closed
                            PRs; cleans up orphaned branches; applies
                            `:no-action` to closed issues lacking terminal
                            labels; then runs an Opus-driven semantic
                            check for duplicates, stuck loops, label
                            corruption, and human-needed issues
                            (pipeline jams, abandoned tasks, repeated
                            diversions, missing reasons). Findings are
                            pre-screened for duplicates/resolved via
                            cai-dup-check; survivors are published as
                            `auto-improve:raised` + `audit` issues in
                            the unified label scheme.

    python cai.py audit-triage  Autonomously resolve `auto-improve:raised`
                            + `audit` findings without opening a PR. Calls
                            `cai-audit-triage` which classifies each finding
                            as `close_duplicate`, `close_resolved`,
                            `passthrough`, or `escalate`.

    python cai.py revise    Watch `:pr-open` PRs for new comments and
                            let the implement subagent iterate on the same
                            branch. Force-pushes revisions with
                            `--force-with-lease`.

    python cai.py confirm   Re-analyze the recent transcript window and
                            verify whether `:merged` issues are actually
                            solved. Patterns that disappeared trigger a
                            SOLVED transition, which closes the issue as
                            GitHub "completed"; patterns that persist are
                            re-queued to `:refined` (up to 3 attempts),
                            then escalated to `:needs-human-review`.

    python cai.py review-pr Walk open PRs against main, run a
                            consistency review for ripple effects. Post
                            findings as PR comments; out-of-scope findings
                            become separate GitHub issues. Skips PRs
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

    python cai.py code-audit  Weekly source-code consistency audit.
                            Clones the repo read-only, runs a Sonnet
                            agent that checks for cross-file
                            inconsistencies, dead code, missing
                            references, and similar concrete problems.
                            Findings are published as issues via
                            publish.py with the `code-audit` namespace.

    python cai.py agent-audit  Weekly audit of .claude/agents/*.md for
                            Claude Code best-practice violations, unused
                            agents (not invoked via `--agent` anywhere), and
                            near-duplicate agents. Runs on Opus. Findings are
                            published via publish.py with the `agent-audit`
                            namespace.

    python cai.py propose     Weekly creative improvement proposal.
                            Clones the repo read-only, runs a creative
                            agent to propose an ambitious improvement,
                            then a review agent to evaluate feasibility.
                            Approved proposals are filed as issues with
                            `auto-improve:raised` so they flow through
                            the refine → fix pipeline.

    python cai.py update-check  Weekly Claude Code release check.
                            Clones the repo, fetches the latest Claude
                            Code releases from GitHub, and runs a Sonnet
                            agent that compares the current pinned
                            version against the latest releases. Findings
                            (new versions, deprecated flags, best
                            practices) are published via publish.py with
                            the `update-check` namespace.

    python cai.py external-scout  Weekly scout for open-source libraries
                            that could replace in-house plumbing.
                            Clones the repo, runs an Opus agent that
                            walks the codebase, picks one category of
                            in-house utility, searches the open-source
                            ecosystem for mature alternatives, and emits
                            a single adoption proposal (or No findings.).
                            Findings are published via publish.py with
                            the `external-scout` namespace. Uses the
                            built-in `memory: project` pool to avoid
                            re-proposing the same category or library.

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
                            by default (configurable via CAI_CHECK_WORKFLOWS_SCHEDULE).

    python cai.py unblock   Scan open issues/PRs parked at
                            `auto-improve:human-needed` (or
                            `auto-improve:pr-human-needed`) that an admin
                            has marked ready for resume by applying the
                            `human:solved` label. For each such item, invokes
                            the `cai-unblock` Haiku agent to classify the
                            admin's comment, fires the matching state
                            transition, strips the label, and returns the
                            issue/PR to the FSM. Requires CAI_ADMIN_LOGINS
                            to be set; without it, `human:solved` is silently
                            ignored.

Default schedules (all configurable via environment variables):

    Subcommand        Default cron       Frequency               Env var
    ─────────────     ──────────────     ─────────────────────   ──────────────────────────
    cycle             0 * * * *          Hourly at :00           CAI_CYCLE_SCHEDULE
    verify            15 * * * *         Hourly at :15           CAI_VERIFY_SCHEDULE
    analyze           0 0 * * *          Daily at midnight       CAI_ANALYZER_SCHEDULE
    audit             0 */6 * * *        Every 6 hours           CAI_AUDIT_SCHEDULE
    check-workflows   0 */6 * * *        Every 6 hours           CAI_CHECK_WORKFLOWS_SCHEDULE
    code-audit        0 3 * * 0          Weekly, Sundays 03:00   CAI_CODE_AUDIT_SCHEDULE
    propose           0 4 * * 0          Weekly, Sundays 04:00   CAI_PROPOSE_SCHEDULE
    cost-optimize     0 5 * * 0          Weekly, Sundays 05:00   CAI_COST_OPTIMIZE_SCHEDULE
    agent-audit       0 6 * * 0          Weekly, Sundays 06:00   CAI_AGENT_AUDIT_SCHEDULE
    update-check      0 4 * * 1          Weekly, Mondays 04:00   CAI_UPDATE_CHECK_SCHEDULE
    external-scout    0 6 * * 1          Weekly, Mondays 06:00   CAI_EXTERNAL_SCOUT_SCHEDULE
    health-report     0 7 * * 1          Weekly, Mondays 07:00   CAI_HEALTH_REPORT_SCHEDULE

The container runs `entrypoint.sh`, which executes `cai.py cycle` once
synchronously at startup (driving the full issue-solving pipeline:
verify → confirm → drain PRs → refine → plan → implement loop), then hands
off to supercronic. Each cron tick is a fresh process. The pipeline is
driven by a single `CAI_CYCLE_SCHEDULE` cron line; a flock in
`cmd_cycle` serializes overlapping runs so issues are processed one
at a time. Orthogonal tasks (analyze, audit, propose, update-check,
health-report, cost-optimize, check-workflows, code-audit, agent-audit,
external-scout) keep their own schedules and are not run at startup.

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

from publish import (  # noqa: E402
    ensure_all_labels, AUDIT_CATEGORIES,
    create_issue, issue_exists, ensure_labels,
)
from cai_lib.dup_check import check_duplicate_or_resolved  # noqa: E402


from cai_lib.config import *  # noqa: E402,F403
from cai_lib.config import (  # noqa: E402
    _STALE_MERGED_DAYS,
)

# ---------------------------------------------------------------------------
# Canonical set of valid cai-managed labels on issues.  Any cai-owned label
# found on an open issue that is NOT in this set is considered stale and will
# be removed by _issue_label_sweep().
# ---------------------------------------------------------------------------
_ALL_MANAGED_ISSUE_LABELS: frozenset[str] = frozenset({
    LABEL_RAISED, LABEL_IN_PROGRESS, LABEL_PR_OPEN,
    LABEL_MERGED, LABEL_SOLVED,
    LABEL_NEEDS_EXPLORATION, LABEL_REFINED, LABEL_REVISING,
    LABEL_PARENT, LABEL_PLANNED, LABEL_PLAN_APPROVED,
    LABEL_REFINING, LABEL_PLANNING, LABEL_APPLYING, LABEL_APPLIED,
    LABEL_HUMAN_NEEDED, LABEL_PR_HUMAN_NEEDED,
    LABEL_TRIAGING, LABEL_MERGE_BLOCKED,
    LABEL_HUMAN_SOLVED, LABEL_KIND_CODE, LABEL_KIND_MAINTENANCE,
    "auto-improve", "audit", "check-workflows", "check-workflows:raised",
})

# Prefixes that identify a label as cai-owned on an issue.
# NO trailing colons — matching uses `lbl == p or lbl.startswith(p + ":")`.
_MANAGED_ISSUE_PREFIXES: tuple[str, ...] = (
    "auto-improve",
    "audit",
    "check-workflows",
    "merge-blocked",
    "human",          # matches human:solved (and any future human:* labels)
    "kind",           # matches kind:code, kind:maintenance
    "pr",             # pr:* labels are PR-only; stale if found on an issue
    "needs-human-review",
)


from cai_lib.logging_utils import (  # noqa: E402
    log_run, log_cost,  # noqa: F401
    _get_issue_category, _log_outcome, _load_outcome_counts,
    _load_outcome_stats, _load_cost_log, _row_ts, _build_cost_summary,
)


# ---------------------------------------------------------------------------
# Common helpers
# ---------------------------------------------------------------------------

from cai_lib.subprocess_utils import _run, _run_claude_p  # noqa: E402


from cai_lib.github import (  # noqa: E402
    _gh_json, check_gh_auth, check_claude_auth, _transcript_dir_is_empty,
    _set_labels, _set_pr_labels, _issue_has_label, _build_issue_block,
    _build_implement_user_message, _fetch_linked_issue_block,
    close_issue_not_planned,
)
from cai_lib.watchdog import _rollback_stale_in_progress  # noqa: E402
from cai_lib.cmd_helpers import _work_directory_block  # noqa: E402
from cai_lib.cmd_unblock import cmd_unblock  # noqa: E402
from cai_lib.actions.confirm import (  # noqa: E402
    _parse_verdicts,
    _update_parent_checklist_item,
)
from cai_lib.dispatcher import dispatch_drain  # noqa: E402


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


def _fetch_human_needed_issues() -> list[dict]:
    """Return open issues/PRs parked at HUMAN_NEEDED / PR_HUMAN_NEEDED.

    For each, parse the most-recent agent-posted divert comment
    (rendered by `_render_human_divert_reason` in cai_lib/fsm.py) to
    extract the failing transition, required/reported confidence, and
    count how many divert comments have been posted (used to detect
    `human_needed_loop` recurrence). Returns a flat list; each entry
    carries a ``parked_as`` field set to LABEL_HUMAN_NEEDED or
    LABEL_PR_HUMAN_NEEDED for the audit agent.
    """
    import re as _re
    MARKER = "🙋 Human attention needed"
    out: list[dict] = []
    for label in (LABEL_HUMAN_NEEDED, LABEL_PR_HUMAN_NEEDED):
        try:
            issues = _gh_json([
                "issue", "list",
                "--repo", REPO,
                "--label", label,
                "--state", "open",
                "--json",
                "number,title,labels,createdAt,updatedAt,comments",
                "--limit", "100",
            ]) or []
        except subprocess.CalledProcessError:
            issues = []
        for it in issues:
            comments = it.get("comments") or []
            latest_body = None
            latest_created = None
            divert_count = 0
            for c in comments:
                body = (c.get("body") or "")
                if MARKER not in body:
                    continue
                divert_count += 1
                created = c.get("createdAt") or ""
                if latest_created is None or created > latest_created:
                    latest_body = body
                    latest_created = created
            transition = required_c = reported_c = None
            if latest_body:
                m = _re.search(r"Automation paused `([^`]+)`", latest_body)
                if m:
                    transition = m.group(1)
                m = _re.search(r"Required confidence:\s*`([^`]+)`", latest_body)
                if m:
                    required_c = m.group(1)
                m = _re.search(r"Reported confidence:\s*`([^`]+)`", latest_body)
                if m:
                    reported_c = m.group(1)
            label_names = [lbl["name"] for lbl in it.get("labels", [])]
            out.append({
                "number": it["number"],
                "title": it["title"],
                "parked_as": label,
                "labels": label_names,
                "createdAt": it.get("createdAt", ""),
                "updatedAt": it.get("updatedAt", ""),
                "divert_count": divert_count,
                "reason_found": latest_body is not None,
                "latest_divert_at": latest_created or "",
                "transition": transition,
                "required_confidence": required_c,
                "reported_confidence": reported_c,
                "has_human_solved": LABEL_HUMAN_SOLVED in label_names,
            })
    return out


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
        LABEL_PLAN_APPROVED: 3,
        LABEL_REFINED: 3,
        LABEL_PLANNED: 3,
        LABEL_RAISED: 4,
        LABEL_MERGED: 5,
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
    _uid = uuid.uuid4().hex[:8]
    work_dir = Path(f"/tmp/cai-analyze-{_uid}")
    work_dir.mkdir(parents=True, exist_ok=True)
    findings_file = work_dir / "findings.json"
    user_message = (
        "## Parsed signals\n\n"
        "```json\n"
        f"{parsed_signals}\n"
        "```\n"
        f"{issues_block}"
        f"{review_pr_block}"
        f"\n\n## Findings file\n\nWrite your findings to: `{findings_file}`\n"
    )

    analyzer = _run_claude_p(
        ["claude", "-p", "--agent", "cai-analyze",
         "--permission-mode", "acceptEdits",
         "--allowedTools", "Read,Grep,Glob,Skill,Write",
         "--add-dir", str(work_dir)],
        category="analyze",
        agent="cai-analyze",
        input=user_message,
        cwd="/app",
    )
    print(analyzer.stdout, flush=True)
    if analyzer.returncode != 0:
        print(
            f"[cai analyze] claude -p failed (exit {analyzer.returncode}):\n"
            f"{analyzer.stderr}",
            flush=True,
        )
        shutil.rmtree(work_dir, ignore_errors=True)
        dur = f"{int(time.monotonic() - t0)}s"
        log_run("analyze", repo=REPO, sessions=session_count,
                tool_calls=tool_calls, in_tokens=in_tokens,
                out_tokens=out_tokens, duration=dur, exit=analyzer.returncode)
        return analyzer.returncode

    if not findings_file.exists():
        print(
            f"[cai analyze] agent did not write {findings_file} — "
            f"expected findings.json output",
            file=sys.stderr, flush=True,
        )
        shutil.rmtree(work_dir, ignore_errors=True)
        dur = f"{int(time.monotonic() - t0)}s"
        log_run("analyze", repo=REPO, sessions=session_count,
                tool_calls=tool_calls, in_tokens=in_tokens,
                out_tokens=out_tokens, result="no_findings_file",
                duration=dur, exit=1)
        return 1

    print("[cai analyze] publishing findings", flush=True)
    published = _run(
        ["python", str(PUBLISH_SCRIPT),
         "--findings-file", str(findings_file)],
    )
    shutil.rmtree(work_dir, ignore_errors=True)
    dur = f"{int(time.monotonic() - t0)}s"
    log_run("analyze", repo=REPO, sessions=session_count,
            tool_calls=tool_calls, in_tokens=in_tokens,
            out_tokens=out_tokens, duration=dur, exit=published.returncode)
    return published.returncode


# ---------------------------------------------------------------------------
# fix
# ---------------------------------------------------------------------------

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
        remove_labels = [LABEL_PR_OPEN, LABEL_MERGE_BLOCKED, LABEL_REVISING]
        if pr is None:
            if _set_labels(issue["number"], add=[LABEL_RAISED], remove=remove_labels, log_prefix=log_prefix):
                comment = (
                    "## Auto-improve: rolling back to :raised\n\n"
                    "No linked PR found for this `:pr-open` issue. "
                    "Resetting to `:raised` so the refine subagent can re-structure it "
                    "and the implement subagent can then attempt a fresh fix.\n\n"
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
            if _set_labels(issue["number"], add=[LABEL_REFINED], remove=remove_labels, log_prefix=log_prefix):
                comment = (
                    "## Auto-improve: rolling back to :refined\n\n"
                    f"Linked PR #{pr['number']} was closed without merging. "
                    "Resetting this issue to `:refined` so it can flow through "
                    "the refinement and planning cycle again before a human "
                    "can re-approve it for the implement subagent.\n\n"
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


def _issue_label_sweep() -> tuple[int, int]:
    """Remove stale/deprecated cai-managed labels from open issues.

    Fetches open issues bearing 'auto-improve', 'audit', or 'check-workflows'
    labels, identifies any cai-owned label not in _ALL_MANAGED_ISSUE_LABELS,
    and removes those stale labels via _set_labels().

    Returns (issues_scanned, labels_removed).
    """
    seen: dict[int, list[str]] = {}
    for base_label in ("auto-improve", "audit", "check-workflows"):
        try:
            batch = _gh_json([
                "issue", "list",
                "--repo", REPO,
                "--label", base_label,
                "--state", "open",
                "--json", "number,labels",
                "--limit", "200",
            ]) or []
        except subprocess.CalledProcessError as e:
            print(f"[cai sweep] gh issue list failed for {base_label!r}:\n{e.stderr}",
                  file=sys.stderr)
            continue
        for issue in batch:
            num = issue["number"]
            if num not in seen:
                seen[num] = [lbl["name"] for lbl in issue.get("labels", [])]

    issues_scanned = len(seen)
    labels_removed = 0
    for num, label_names in seen.items():
        stale = [
            lbl for lbl in label_names
            if any(lbl == p or lbl.startswith(p + ":") for p in _MANAGED_ISSUE_PREFIXES)
            and lbl not in _ALL_MANAGED_ISSUE_LABELS
        ]
        if stale:
            _set_labels(num, remove=stale, log_prefix="cai sweep")
            labels_removed += len(stale)
            print(f"[cai sweep] #{num}: removed stale label(s) {stale}", flush=True)

    print(f"[cai sweep] scanned {issues_scanned} issue(s), removed {labels_removed} stale label(s)",
          flush=True)
    return issues_scanned, labels_removed


def cmd_verify(args) -> int:
    """Walk :pr-open issues and transition labels based on PR state."""
    print("[cai verify] checking pr-open issues", flush=True)
    _issue_label_sweep()
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
    # in cmd_implement step 10 failed silently.
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
        iss_labels = {l["name"] for l in iss.get("labels", [])}  # noqa: E741
        if LABEL_PR_OPEN in iss_labels:
            continue
        # Issue is open, has an open PR, but missing :pr-open — recover.
        remove = [l for l in (LABEL_IN_PROGRESS, LABEL_REFINED, LABEL_PLANNED, LABEL_PLAN_APPROVED, LABEL_APPLYING, LABEL_APPLIED, LABEL_RAISED) if l in iss_labels]  # noqa: E741
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


def _migrate_no_action_labels() -> list[int]:
    """One-time migration: close any open :no-action issues as 'not planned'.

    Idempotent — once the label is deleted from GitHub, gh issue list
    returns nothing and subsequent calls are no-ops.  If the label is
    completely unknown to GitHub, the CalledProcessError is caught and
    an empty list is returned.
    """
    try:
        issues = _gh_json([
            "issue", "list",
            "--repo", REPO,
            "--label", "auto-improve:no-action",
            "--state", "open",
            "--json", "number,title",
            "--limit", "100",
        ]) or []
    except subprocess.CalledProcessError as exc:
        print(
            f"[cai audit] _migrate_no_action_labels: gh issue list failed "
            f"(label likely already deleted): {exc.stderr}",
            file=sys.stderr,
        )
        return []

    closed = []
    for issue in issues:
        num = issue["number"]
        ok = close_issue_not_planned(
            num,
            "Closing as **not planned** — `auto-improve:no-action` is retired; "
            "the disposition is now recorded via GitHub's native close-with-reason.",
            log_prefix="cai audit",
        )
        if ok:
            closed.append(num)
            log_run("audit", action="no_action_migrated_closed", issue=num)
            print(
                f"[cai audit] migrated #{num}: closed as not planned",
                flush=True,
            )
    return closed


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

    # Step 1c: One-time migration — close any surviving open :no-action issues.
    _migrate_no_action_labels()

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

    # 2e. Issues/PRs currently parked at human-needed — include the
    #     parsed divert reason so the LLM can classify root cause.
    human_needed = _fetch_human_needed_issues()
    human_section = "## Open issues/PRs parked at human-needed\n\n"
    if human_needed:
        human_section += (
            "For each entry the most-recent divert comment (rendered by "
            "`_render_human_divert_reason`) has been parsed. A missing "
            "`Transition`/`Required`/`Reported` field means the divert "
            "comment is absent or malformed (→ `human_needed_reason_missing`).\n\n"
        )
        for hn in human_needed:
            human_section += (
                f"- #{hn['number']} ({hn['parked_as']}): {hn['title']}\n"
                f"  - Created: {hn['createdAt']}; Updated: {hn['updatedAt']}\n"
                f"  - Divert comments on issue: {hn['divert_count']}; "
                f"latest at: {hn['latest_divert_at'] or '(none)'}\n"
                f"  - Transition: {hn['transition'] or '(missing)'}\n"
                f"  - Required confidence: {hn['required_confidence'] or '(missing)'}; "
                f"Reported confidence: {hn['reported_confidence'] or '(missing)'}\n"
                f"  - human:solved applied: {hn['has_human_solved']}\n"
            )
    else:
        human_section += "(none)\n"

    log_section = "## Log tail (last ~200 lines)\n\n```\n" + (log_tail or "(empty)") + "\n```\n"

    deterministic_section = ""
    if rolled_back:
        deterministic_section += "## Stale lock rollbacks performed this run\n\n"
        for rb in rolled_back:
            deterministic_section += f"- #{rb['number']}: {rb['title']}\n"
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

    # Outcome statistics for the audit agent to spot fix_loop_efficiency issues.
    outcome_counts = _load_outcome_counts(days=90)
    if outcome_counts:
        outcome_lines = ["## Outcome statistics (last 90 days)\n",
                         "| Category | Total | Solved | Rate |",
                         "|---|---|---|---|"]
        for cat, bucket in sorted(outcome_counts.items()):
            total = bucket["total"]
            solved = bucket["solved"]
            rate = solved / total if total else 0.0
            flag = " ⚠" if rate < 0.4 and total >= 3 else ""
            outcome_lines.append(f"| {cat} | {total} | {solved} | {rate:.0%}{flag} |")
        outcome_section = "\n".join(outcome_lines) + "\n"
    else:
        outcome_section = "## Outcome statistics (last 90 days)\n\n(no outcome-log entries yet)\n"

    _uid = uuid.uuid4().hex[:8]
    work_dir = Path(f"/tmp/cai-audit-{_uid}")
    work_dir.mkdir(parents=True, exist_ok=True)
    findings_file = work_dir / "findings.json"
    user_message = (
        f"{issues_section}\n"
        f"{prs_section}\n"
        f"{log_section}\n"
        f"{cost_section}\n"
        f"{outcome_section}\n"
        f"{closed_section}\n"
        f"{human_section}\n"
        f"{deterministic_section}"
        f"\n## Findings file\n\nWrite your findings to: `{findings_file}`\n"
    )

    # Step 3: Invoke the declared cai-audit subagent.
    audit = _run_claude_p(
        ["claude", "-p", "--agent", "cai-audit",
         "--permission-mode", "acceptEdits",
         "--allowedTools", "Read,Grep,Glob,Write",
         "--add-dir", str(work_dir)],
        category="audit",
        agent="cai-audit",
        input=user_message,
        cwd="/app",
    )
    print(audit.stdout, flush=True)
    if audit.returncode != 0:
        print(
            f"[cai audit] claude -p failed (exit {audit.returncode}):\n"
            f"{audit.stderr}",
            flush=True,
        )
        shutil.rmtree(work_dir, ignore_errors=True)
        dur = f"{int(time.monotonic() - t0)}s"
        log_run("audit", repo=REPO, duration=dur,
                pr_open_recovered=len(recovered_pr_open),
                branches_cleaned=len(deleted_orphaned),
                merged_flagged=len(flagged_merged),
                exit=audit.returncode)
        return audit.returncode

    # Step 4: Publish findings via publish.py with audit namespace.
    if not findings_file.exists():
        print(
            f"[cai audit] agent did not write {findings_file} — "
            f"expected findings.json output",
            file=sys.stderr, flush=True,
        )
        shutil.rmtree(work_dir, ignore_errors=True)
        dur = f"{int(time.monotonic() - t0)}s"
        log_run("audit", repo=REPO, rollbacks=len(rolled_back),
                pr_open_recovered=len(recovered_pr_open),
                branches_cleaned=len(deleted_orphaned),
                merged_flagged=len(flagged_merged),
                result="no_findings_file", duration=dur, exit=1)
        return 1

    print("[cai audit] publishing audit findings", flush=True)
    published = _run(
        ["python", str(PUBLISH_SCRIPT), "--namespace", "audit",
         "--findings-file", str(findings_file)],
    )
    shutil.rmtree(work_dir, ignore_errors=True)
    dur = f"{int(time.monotonic() - t0)}s"
    log_run("audit", repo=REPO, rollbacks=len(rolled_back),
            pr_open_recovered=len(recovered_pr_open),
            branches_cleaned=len(deleted_orphaned),
            merged_flagged=len(flagged_merged),
            duration=dur, exit=published.returncode)
    return published.returncode


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
# agent-audit — weekly audit of .claude/agents/ for consistency and usage
# ---------------------------------------------------------------------------


def _read_agent_audit_memory() -> str:
    """Return the contents of the agent-audit memory file, or empty string."""
    if not AGENT_AUDIT_MEMORY.exists():
        return ""
    try:
        return AGENT_AUDIT_MEMORY.read_text().strip()
    except OSError:
        return ""


def _save_agent_audit_memory(agent_output: str) -> None:
    """Extract the ## Memory Update block from agent output and persist it."""
    match = re.search(
        r"^## Memory Update\s*\n(.*)",
        agent_output,
        flags=re.MULTILINE | re.DOTALL,
    )
    if not match:
        return
    try:
        AGENT_AUDIT_MEMORY.parent.mkdir(parents=True, exist_ok=True)
        AGENT_AUDIT_MEMORY.write_text(match.group(0).strip() + "\n")
    except OSError as exc:
        print(f"[cai agent-audit] could not write memory: {exc}", flush=True)


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
    result = _run_claude_p(
        ["claude", "-p", "--agent", "cai-cost-optimize",
         "--permission-mode", "acceptEdits",
         "--allowedTools", "Read,Grep,Glob"],
        category="cost-optimize",
        agent="cai-cost-optimize",
        input=user_message,
        cwd="/app",
    )
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
    creative = _run_claude_p(
        ["claude", "-p", "--agent", "cai-propose",
         "--permission-mode", "acceptEdits",
         "--allowedTools", "Read,Grep,Glob",
         "--add-dir", str(work_dir)],
        category="propose",
        agent="cai-propose",
        input=user_message,
        cwd="/app",
    )
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
    review = _run_claude_p(
        ["claude", "-p", "--agent", "cai-propose-review",
         "--permission-mode", "acceptEdits",
         "--allowedTools", "Read,Grep,Glob",
         "--add-dir", str(work_dir)],
        category="propose",
        agent="cai-propose-review",
        input=review_message,
        cwd="/app",
    )
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

    findings_file = work_dir / "findings.json"

    # 2. Build the user message with the runtime memory from the
    #    named-volume log directory (cai_logs). System prompt, tool allowlist
    #    (Read/Grep/Glob/Write), and model (sonnet) all live in
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
    agent = _run_claude_p(
        ["claude", "-p", "--agent", "cai-code-audit",
         "--permission-mode", "acceptEdits",
         "--allowedTools", "Read,Grep,Glob,Write",
         "--add-dir", str(work_dir)],
        category="code-audit",
        agent="cai-code-audit",
        input=user_message,
        cwd="/app",
    )
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

    if not findings_file.exists():
        print(
            f"[cai code-audit] agent did not write {findings_file} — "
            f"expected findings.json output",
            file=sys.stderr, flush=True,
        )
        shutil.rmtree(work_dir, ignore_errors=True)
        dur = f"{int(time.monotonic() - t0)}s"
        log_run("code-audit", repo=REPO, result="no_findings_file",
                duration=dur, exit=1)
        return 1

    # 5. Publish findings via publish.py with code-audit namespace.
    print("[cai code-audit] publishing findings", flush=True)
    published = _run(
        ["python", str(PUBLISH_SCRIPT), "--namespace", "code-audit",
         "--findings-file", str(findings_file)],
    )

    # 6. Clean up.
    shutil.rmtree(work_dir, ignore_errors=True)

    dur = f"{int(time.monotonic() - t0)}s"
    log_run("code-audit", repo=REPO, duration=dur, exit=published.returncode)
    return published.returncode


def cmd_agent_audit(args) -> int:
    """Clone the repo and run the agent-audit agent to audit .claude/agents/."""
    print("[cai agent-audit] running agent inventory audit", flush=True)
    t0 = time.monotonic()

    _uid = uuid.uuid4().hex[:8]
    work_dir = Path(f"/tmp/cai-agent-audit-{_uid}")

    if work_dir.exists():
        shutil.rmtree(work_dir)

    clone = _run(
        ["git", "clone", "--depth", "1",
         f"https://github.com/{REPO}.git", str(work_dir)],
        capture_output=True,
    )
    if clone.returncode != 0:
        print(
            f"[cai agent-audit] git clone failed:\n{clone.stderr}",
            file=sys.stderr, flush=True,
        )
        dur = f"{int(time.monotonic() - t0)}s"
        log_run("agent-audit", repo=REPO, result="clone_failed",
                duration=dur, exit=1)
        return 1

    memory = _read_agent_audit_memory()
    memory_section = "## Memory from previous runs\n\n"
    if memory:
        memory_section += memory + "\n"
    else:
        memory_section += "(first run — no prior memory)\n"

    user_message = _work_directory_block(work_dir) + "\n" + memory_section

    findings_file = work_dir / "findings.json"

    print(f"[cai agent-audit] running agent for {work_dir}", flush=True)
    agent = _run_claude_p(
        ["claude", "-p", "--agent", "cai-agent-audit",
         "--permission-mode", "acceptEdits",
         "--allowedTools", "Read,Grep,Glob,Write",
         "--add-dir", str(work_dir)],
        category="agent-audit",
        agent="cai-agent-audit",
        input=user_message,
        cwd="/app",
    )
    if agent.stdout:
        print(agent.stdout, flush=True)
    if agent.returncode != 0:
        print(
            f"[cai agent-audit] claude -p failed (exit {agent.returncode}):\n"
            f"{agent.stderr}",
            file=sys.stderr, flush=True,
        )
        shutil.rmtree(work_dir, ignore_errors=True)
        dur = f"{int(time.monotonic() - t0)}s"
        log_run("agent-audit", repo=REPO, result="agent_failed",
                duration=dur, exit=agent.returncode)
        return agent.returncode

    _save_agent_audit_memory(agent.stdout)

    if not findings_file.exists():
        print(
            f"[cai agent-audit] agent did not write {findings_file} — "
            f"expected findings.json output",
            file=sys.stderr, flush=True,
        )
        shutil.rmtree(work_dir, ignore_errors=True)
        dur = f"{int(time.monotonic() - t0)}s"
        log_run("agent-audit", repo=REPO, result="no_findings_file",
                duration=dur, exit=1)
        return 1

    print("[cai agent-audit] publishing findings", flush=True)
    published = _run(
        ["python", str(PUBLISH_SCRIPT), "--namespace", "agent-audit",
         "--findings-file", str(findings_file)],
    )

    shutil.rmtree(work_dir, ignore_errors=True)

    dur = f"{int(time.monotonic() - t0)}s"
    log_run("agent-audit", repo=REPO, duration=dur, exit=published.returncode)
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

    findings_file = work_dir / "findings.json"

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
    agent = _run_claude_p(
        ["claude", "-p", "--agent", "cai-update-check",
         "--permission-mode", "acceptEdits",
         "--allowedTools", "Read,Grep,Glob,Write",
         "--add-dir", str(work_dir)],
        category="update-check",
        agent="cai-update-check",
        input=user_message,
        cwd="/app",
    )
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

    if not findings_file.exists():
        print(
            f"[cai update-check] agent did not write {findings_file} — "
            f"expected findings.json output",
            file=sys.stderr, flush=True,
        )
        shutil.rmtree(work_dir, ignore_errors=True)
        dur = f"{int(time.monotonic() - t0)}s"
        log_run("update-check", repo=REPO, result="no_findings_file",
                duration=dur, exit=1)
        return 1

    # 8. Publish findings via publish.py with update-check namespace.
    print("[cai update-check] publishing findings", flush=True)
    published = _run(
        ["python", str(PUBLISH_SCRIPT), "--namespace", "update-check",
         "--findings-file", str(findings_file)],
    )

    # 9. Clean up.
    shutil.rmtree(work_dir, ignore_errors=True)

    dur = f"{int(time.monotonic() - t0)}s"
    log_run("update-check", repo=REPO, duration=dur, exit=published.returncode)
    return published.returncode


# ---------------------------------------------------------------------------
# external-scout — weekly scout for open-source library replacements
# ---------------------------------------------------------------------------


def cmd_external_scout(args) -> int:
    """Clone the repo and scout open-source libraries that could replace in-house plumbing."""
    print("[cai external-scout] scouting for external solutions", flush=True)
    t0 = time.monotonic()

    # 1. Clone repo into a temporary directory.
    _uid = uuid.uuid4().hex[:8]
    work_dir = Path(f"/tmp/cai-external-scout-{_uid}")

    if work_dir.exists():
        shutil.rmtree(work_dir)

    clone = _run(
        ["git", "clone", "--depth", "1",
         f"https://github.com/{REPO}.git", str(work_dir)],
        capture_output=True,
    )
    if clone.returncode != 0:
        print(
            f"[cai external-scout] git clone failed:\n{clone.stderr}",
            file=sys.stderr, flush=True,
        )
        dur = f"{int(time.monotonic() - t0)}s"
        log_run("external-scout", repo=REPO, result="clone_failed",
                duration=dur, exit=1)
        return 1

    findings_file = work_dir / "findings.json"

    # 2. Build the user message. System prompt, tool allowlist, and model all
    #    live in `.claude/agents/cai-external-scout.md`. Durable per-agent
    #    learnings live in its `memory: project` pool — auto-loaded by
    #    claude-code, no external memory file needed.
    user_message = (
        _work_directory_block(work_dir)
        + f"\n\n## Findings file\n\nWrite your findings to: `{findings_file}`\n"
    )

    # 3. Invoke the declared cai-external-scout subagent.
    #    Runs with `cwd=/app` and `--add-dir <work_dir>` so the agent
    #    reads its definition + memory from the canonical /app paths
    #    while examining the clone via absolute paths.
    print(f"[cai external-scout] running agent for {work_dir}", flush=True)
    agent = _run_claude_p(
        ["claude", "-p", "--agent", "cai-external-scout",
         "--permission-mode", "acceptEdits",
         "--allowedTools", "Read,Grep,Glob,WebSearch,WebFetch,Write",
         "--add-dir", str(work_dir)],
        category="external-scout",
        agent="cai-external-scout",
        input=user_message,
        cwd="/app",
    )
    if agent.stdout:
        print(agent.stdout, flush=True)
    if agent.returncode != 0:
        print(
            f"[cai external-scout] claude -p failed (exit {agent.returncode}):\n"
            f"{agent.stderr}",
            file=sys.stderr, flush=True,
        )
        shutil.rmtree(work_dir, ignore_errors=True)
        dur = f"{int(time.monotonic() - t0)}s"
        log_run("external-scout", repo=REPO, result="agent_failed",
                duration=dur, exit=agent.returncode)
        return agent.returncode

    if not findings_file.exists():
        print(
            f"[cai external-scout] agent did not write {findings_file} — "
            f"expected findings.json output",
            file=sys.stderr, flush=True,
        )
        shutil.rmtree(work_dir, ignore_errors=True)
        dur = f"{int(time.monotonic() - t0)}s"
        log_run("external-scout", repo=REPO, result="no_findings_file",
                duration=dur, exit=1)
        return 1

    # 4. Publish findings via publish.py with external-scout namespace.
    print("[cai external-scout] publishing findings", flush=True)
    published = _run(
        ["python", str(PUBLISH_SCRIPT), "--namespace", "external-scout",
         "--findings-file", str(findings_file)],
    )

    # 5. Clean up.
    shutil.rmtree(work_dir, ignore_errors=True)

    dur = f"{int(time.monotonic() - t0)}s"
    log_run("external-scout", repo=REPO, duration=dur, exit=published.returncode)
    return published.returncode


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


_CYCLE_LOCK_PATH = f"/tmp/cai-cycle-{REPO.replace('/', '-')}.lock"


def cmd_cycle(args) -> int:
    """One cycle tick under a non-blocking flock.

    Delegates to :func:`_cmd_cycle_inner`, which reconciles labels,
    runs audit, and dispatches a single actionable issue/PR via the
    FSM dispatcher. The flock on ``_CYCLE_LOCK_PATH`` (per-repo) ensures
    overlapping supercronic fires don't step on each other.
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
    """One cycle tick: restart-recovery + dispatch one actionable issue/PR.

    Verify and audit run on their own cron cadences (CAI_VERIFY_SCHEDULE,
    CAI_AUDIT_SCHEDULE) — the cycle is purely restart-recovery + dispatch.
    """
    print("[cai cycle] starting cycle tick", flush=True)
    t0 = time.monotonic()
    all_results: dict[str, int] = {}
    had_failure = False

    # Phase 0: self-heal parent label. Dispatcher lists open issues via
    # `--label auto-improve`, so any issue carrying an FSM state label
    # (e.g. auto-improve:raised) but missing the parent `auto-improve`
    # label is invisible to the cycle. Add the parent where missing.
    _fsm_state_labels = (
        LABEL_RAISED, LABEL_REFINING, LABEL_REFINED,
        LABEL_PLANNING, LABEL_PLANNED, LABEL_PLAN_APPROVED,
        LABEL_APPLYING, LABEL_APPLIED, LABEL_IN_PROGRESS,
        LABEL_PR_OPEN, LABEL_REVISING, LABEL_MERGED,
        LABEL_HUMAN_NEEDED, LABEL_TRIAGING,
    )
    _healed: set[int] = set()
    for _lbl in _fsm_state_labels:
        try:
            _issues = _gh_json([
                "issue", "list",
                "--repo", REPO,
                "--label", _lbl,
                "--state", "open",
                "--json", "number,labels",
                "--limit", "100",
            ]) or []
        except Exception:
            continue
        for _iss in _issues:
            _num = _iss["number"]
            if _num in _healed:
                continue
            _names = [lb["name"] for lb in _iss.get("labels", [])]
            if "auto-improve" not in _names:
                if _set_labels(_num, add=["auto-improve"], log_prefix="cai cycle"):
                    print(
                        f"[cai cycle] self-heal: added parent "
                        f"`auto-improve` to #{_num}",
                        flush=True,
                    )
                _healed.add(_num)

    # Phase 1: restart recovery — force-rollback any stuck locks left
    # behind by a previous run that crashed mid-handler.
    rolled_back = _rollback_stale_in_progress(immediate=True)
    if rolled_back:
        nums = ", ".join(f"#{i['number']}" for i in rolled_back)
        print(f"[cai cycle] recovered {len(rolled_back)} stale lock(s): {nums}",
              flush=True)

    # Phase 2: dispatch a single actionable issue/PR via the FSM dispatcher.
    # Note: :applied → :solved bookkeeping is handled by handle_applied in
    # the dispatcher (IssueState.APPLIED), so no separate Phase 1.5 is needed.
    rc = _run_step("dispatch", lambda _a: dispatch_drain(), args)
    all_results["dispatch"] = rc
    if rc != 0:
        had_failure = True

    dur = f"{time.monotonic() - t0:.1f}s"
    summary = " ".join(f"{k}={v}" for k, v in all_results.items())
    print(f"\n[cai cycle] done in {dur} — {summary}", flush=True)
    log_run("cycle", repo=REPO, results=summary,
            duration=dur, exit=1 if had_failure else 0)
    return 1 if had_failure else 0


def cmd_dispatch(args) -> int:
    """Dispatch one or more FSM actions.

    With no args, drains the actionable queue: repeatedly picks the
    oldest actionable issue/PR and dispatches it until the queue is
    empty (or a loop-guard / max-iter cap fires). With --issue N,
    fetches issue N, derives its FSM state, and runs the matching
    handler exactly once. With --pr N, same for a PR.
    """
    from cai_lib.dispatcher import (
        dispatch_issue, dispatch_pr, dispatch_drain,
    )
    if getattr(args, "issue", None) is not None:
        return dispatch_issue(args.issue)
    if getattr(args, "pr", None) is not None:
        return dispatch_pr(args.pr)
    return dispatch_drain()




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
                l = last_by_agent.get(agent, 0.0)  # noqa: E741
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
    except Exception:
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
    _uid = uuid.uuid4().hex[:8]
    work_dir = Path(f"/tmp/cai-check-workflows-{_uid}")
    work_dir.mkdir(parents=True, exist_ok=True)
    findings_file = work_dir / "findings.json"

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

    user_message = (
        runs_section
        + existing_section
        + f"\n## Findings file\n\nWrite your findings to: `{findings_file}`\n"
    )

    # 5. Invoke the declared cai-check-workflows agent.
    print(
        f"[cai check-workflows] running agent on {len(recent_runs)} failure(s)",
        flush=True,
    )
    agent = _run_claude_p(
        ["claude", "-p", "--agent", "cai-check-workflows",
         "--max-turns", "3",
         "--permission-mode", "acceptEdits",
         "--allowedTools", "Read,Grep,Glob,Write",
         "--add-dir", str(work_dir)],
        category="check-workflows",
        agent="cai-check-workflows",
        input=user_message,
        cwd="/app",
    )
    if agent.stdout:
        print(agent.stdout, flush=True)
    if agent.returncode != 0:
        print(
            f"[cai check-workflows] agent failed (exit {agent.returncode}):\n"
            f"{agent.stderr}",
            file=sys.stderr, flush=True,
        )
        shutil.rmtree(work_dir, ignore_errors=True)
        dur = f"{int(time.monotonic() - t0)}s"
        log_run("check-workflows", repo=REPO, result="agent_failed",
                duration=dur, exit=agent.returncode)
        return agent.returncode

    if not findings_file.exists():
        print(
            f"[cai check-workflows] agent did not write {findings_file} — "
            f"expected findings.json output",
            file=sys.stderr, flush=True,
        )
        shutil.rmtree(work_dir, ignore_errors=True)
        dur = f"{int(time.monotonic() - t0)}s"
        log_run("check-workflows", repo=REPO, result="no_findings_file",
                duration=dur, exit=1)
        return 1

    # 6. Publish findings via publish.py with check-workflows namespace.
    print("[cai check-workflows] publishing findings", flush=True)
    published = _run(
        ["python", str(PUBLISH_SCRIPT), "--namespace", "check-workflows",
         "--findings-file", str(findings_file)],
    )
    shutil.rmtree(work_dir, ignore_errors=True)

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

    dispatch_parser = sub.add_parser("dispatch", help="Dispatch FSM action (oldest actionable by default)")
    dispatch_parser.add_argument("--issue", type=int, default=None, help="Dispatch a specific issue by number")
    dispatch_parser.add_argument("--pr", type=int, default=None, help="Dispatch a specific PR by number")

    sub.add_parser("verify", help="Update labels based on PR merge state")
    sub.add_parser("audit", help="Run the queue/PR consistency audit (includes human-needed checks)")
    sub.add_parser("code-audit", help="Audit repo source code for inconsistencies")
    sub.add_parser("agent-audit", help="Weekly audit of .claude/agents/ for consistency and usage")
    sub.add_parser("propose", help="Weekly creative improvement proposal")
    sub.add_parser("update-check", help="Check Claude Code releases for workspace improvements")
    sub.add_parser("external-scout", help="Scout open-source libraries to replace in-house plumbing")
    sub.add_parser(
        "unblock",
        help="Resume :human-needed issues when an admin has commented",
    )
    sub.add_parser("cost-optimize", help="Weekly cost-reduction proposal or evaluation")
    sub.add_parser("check-workflows", help="Check GitHub Actions for recent workflow failures and raise findings")
    sub.add_parser("cycle", help="One cycle tick: verify, audit, dispatch one actionable issue/PR")
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
        "dispatch": cmd_dispatch,
        "verify": cmd_verify,
        "audit": cmd_audit,
        "code-audit": cmd_code_audit,
        "agent-audit": cmd_agent_audit,
        "propose": cmd_propose,
        "update-check": cmd_update_check,
        "external-scout": cmd_external_scout,
        "unblock": cmd_unblock,
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
