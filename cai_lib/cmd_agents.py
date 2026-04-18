"""Agent-launch cmd_* functions extracted from cai.py.

Each function builds a prompt, invokes a claude -p sub-agent, and
publishes or handles the results.  This module contains the eight
periodic/creative agent commands:
  cmd_analyze, cmd_audit, cmd_propose, cmd_code_audit,
  cmd_agent_audit, cmd_update_check, cmd_cost_optimize, cmd_external_scout
"""

import json
import re
import shutil
import subprocess
import sys
import time
import uuid

from datetime import datetime, timedelta, timezone
from pathlib import Path

from cai_lib.config import *  # noqa: F403
from cai_lib.config import _STALE_MERGED_DAYS  # noqa: F401

from cai_lib.logging_utils import (
    log_run,
    _load_cost_log, _row_ts, _build_cost_summary, _load_outcome_counts,
)

from cai_lib.subprocess_utils import _run, _run_claude_p

from cai_lib.github import (
    _gh_json, _set_labels, close_issue_not_planned, _recover_stale_pr_open,
)
from cai_lib.watchdog import _rollback_stale_in_progress
from cai_lib.cmd_helpers import _work_directory_block
from cai_lib import transcript_sync


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
        # Detect whether this issue was already closed by any cai agent
        # (either _retroactive_no_action_sweep or _migrate_no_action_labels).
        has_retroactive_close = any(
            "Closing as **not planned**" in (c.get("body") or "")
            for c in comments
        )
        result.append({
            "number": issue["number"],
            "title": issue["title"],
            "labels": [lbl["name"] for lbl in issue.get("labels", [])],
            "closedAt": issue.get("closedAt", ""),
            "rationale": rationale,
            "rationale_author": rationale_author,
            "has_retroactive_close": has_retroactive_close,
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

    # When cross-host transcript sync is enabled, pull every machine's
    # bucket into the local aggregate mirror before parsing — this way
    # the analyzer sees tool-call activity from all machines that share
    # this repo, not only the host this container runs on. No-op when
    # sync is disabled.
    transcript_sync.pull()
    parse_dir = transcript_sync.parse_source()

    if not parse_dir.exists():
        print(
            f"[cai analyze] no transcript dir at {parse_dir}; nothing to analyze",
            flush=True,
        )
        log_run("analyze", repo=REPO, sessions=0, tool_calls=0,
                in_tokens=0, out_tokens=0, duration="0s", exit=0)
        return 0

    parsed = _run(
        ["python", str(PARSE_SCRIPT), str(parse_dir)],
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


def _retroactive_no_action_sweep() -> list[dict]:
    """Close recently-closed auto-improve issues that lack a terminal label.

    Issues closed without auto-improve:merged or auto-improve:solved
    (and not already closed as 'not planned') are re-closed with
    --reason 'not planned' to satisfy the terminal-state requirement.
    """
    closed_issues = _fetch_closed_auto_improve_issues(limit=50)
    terminal_labels = {LABEL_MERGED, LABEL_SOLVED}
    swept = []
    for ci in closed_issues:
        labels = set(ci.get("labels", []))
        if labels & terminal_labels:
            continue  # already has a terminal label
        # Skip if already retroactively closed (marker comment detected).
        if ci.get("has_retroactive_close"):
            continue
        ok = close_issue_not_planned(
            ci["number"],
            "Retroactively closing as **not planned** — issue was closed "
            "without a terminal lifecycle label.",
            log_prefix="cai audit",
        )
        if ok:
            swept.append({"number": ci["number"], "title": ci["title"]})
            log_run("audit", action="no_action_applied_retroactively",
                    issue=ci["number"])
            print(
                f"[audit] action=no_action_applied_retroactively "
                f"issue=#{ci['number']}",
                flush=True,
            )
    return swept


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

    # Step 1f: Retroactively close auto-improve issues closed without terminal labels.
    retroactive_no_action = _retroactive_no_action_sweep()

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
    if retroactive_no_action:
        deterministic_section += "## Closed issues with :no-action applied retroactively this run\n\n"
        for ra in retroactive_no_action:
            deterministic_section += f"- #{ra['number']}: {ra['title']}\n"
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
                retroactive_no_action=len(retroactive_no_action),
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
                retroactive_no_action=len(retroactive_no_action),
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
            retroactive_no_action=len(retroactive_no_action),
            duration=dur, exit=published.returncode)
    return published.returncode


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

    # 2. Detect current installed version of claude-code.
    ver_result = _run(
        ["claude", "--version"], capture_output=True,
    )
    current_version = ver_result.stdout.strip() if ver_result.returncode == 0 else "unknown"

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
