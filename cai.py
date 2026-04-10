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

    python cai.py fix       Pick the oldest issue labelled
                            `auto-improve:raised` or `auto-improve:
                            requested` (audit issues reach fix via
                            triage relabelling), lock it via the `:in-progress`
                            label, clone the repo into /tmp, run the
                            fix subagent (full tool permissions), and
                            open a PR if the agent produced a diff.
                            Rolls back the label on empty diff or any
                            failure.

    python cai.py verify    Mechanical, no-LLM. Walk issues with
                            `:pr-open`, find their linked PR by `Refs`
                            search, and transition the label:
                            merged → `:merged`,
                            closed-unmerged → `:raised`.

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
                            with `:solved`; patterns that persist stay
                            as `:merged`.

    python cai.py review-pr Walk open PRs against main, run a
                            consistency review for ripple effects, and
                            post findings as PR comments. Skips PRs
                            already reviewed at their current HEAD SHA.

    python cai.py merge     Confidence-gated auto-merge for bot PRs.
                            Evaluates each :pr-open PR against its
                            linked issue, posts a verdict comment, and
                            merges when confidence meets the threshold.

    python cai.py code-audit  Weekly source-code consistency audit.
                            Clones the repo read-only, runs a Sonnet
                            agent that checks for cross-file
                            inconsistencies, dead code, missing
                            references, and similar concrete problems.
                            Findings are published as issues via
                            publish.py with the `code-audit` namespace.

The container runs `entrypoint.sh`, which executes `init`, `analyze`,
`fix`, `revise`, `verify`, `audit`, `code-audit`, `confirm`, `review-pr`, and `merge` once synchronously at
startup, then hands off to supercronic. Each cron tick is a fresh process.

The gh auth check is done once per subcommand invocation. We want a
clear error message in docker logs if credentials ever disappear from
the cai_gh_config volume.

No third-party Python dependencies — only stdlib.
"""

import argparse
import concurrent.futures
import json
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path


REPO = "damien-robotsix/robotsix-cai"
SMOKE_PROMPT = "Say hello in one short sentence."

# Root of claude-code's per-cwd transcript dirs. claude-code writes
# `/root/.claude/projects/<sanitized-cwd>/<session-id>.jsonl` for every
# session, so this directory contains one subdir per cwd:
#   * `-app/`            — sessions started by cai.py inside /app
#   * `-tmp-cai-fix-<N>/` — sessions started by the fix subagent in
#                          its per-issue clone under /tmp
# The analyzer parses *all* of them so the fix subagent's tool-rich
# sessions feed back into the next analyzer cycle.
TRANSCRIPT_DIR = Path("/root/.claude/projects")

# Files baked into the image alongside cai.py.
PARSE_SCRIPT = Path("/app/parse.py")
PUBLISH_SCRIPT = Path("/app/publish.py")
# Persistent memory file for the code-audit agent. Stored in the
# bind-mounted log directory so it survives container restarts.
CODE_AUDIT_MEMORY = Path("/var/log/cai/code-audit-memory.md")

# Issue lifecycle labels.
LABEL_RAISED = "auto-improve:raised"
LABEL_REQUESTED = "auto-improve:requested"
LABEL_IN_PROGRESS = "auto-improve:in-progress"
LABEL_PR_OPEN = "auto-improve:pr-open"
LABEL_MERGED = "auto-improve:merged"
LABEL_SOLVED = "auto-improve:solved"
LABEL_NO_ACTION = "auto-improve:no-action"
LABEL_REVISING = "auto-improve:revising"
LABEL_MERGE_BLOCKED = "merge-blocked"
LABEL_AUDIT_RAISED = "audit:raised"
LABEL_AUDIT_NEEDS_HUMAN = "audit:needs-human"

# PR-level label applied by `cai merge` when the verdict is below the
# auto-merge threshold. Lets a human filter open PRs that are waiting
# on their decision (`label:needs-human-review`). Issue #216.
LABEL_PR_NEEDS_HUMAN = "needs-human-review"


# ---------------------------------------------------------------------------
# Run log
# ---------------------------------------------------------------------------

LOG_PATH = Path("/var/log/cai/cai.log")


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


# ---------------------------------------------------------------------------
# Common helpers
# ---------------------------------------------------------------------------

def _run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    """Thin wrapper around subprocess.run with text mode and check=False."""
    return subprocess.run(cmd, text=True, check=False, **kwargs)


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
        print("       Credentials are expected in the cai_gh_config volume.", file=sys.stderr)
        print("       Run the installer's login step, or do it manually:", file=sys.stderr)
        print("         docker compose run --rm cai gh auth login", file=sys.stderr)
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
    result = _run(["claude", "-p", SMOKE_PROMPT])
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


def _closed_issues_block(closed: list[dict]) -> str:
    """Format closed issues + their rationales as a prompt section."""
    if not closed:
        return ""
    lines = [
        "\n\n## Previously closed auto-improve issues",
        "",
        "These auto-improve issues have already been considered and "
        "closed. Before raising any new finding, check the rationale "
        "for each closed issue below. If your proposed finding "
        "overlaps with one of these by topic, do NOT re-raise it — "
        "the supervisor's reasoning still applies. The only exception: "
        "you have concrete new evidence that the prior reasoning is "
        "wrong, in which case raise a finding that explicitly "
        "references the prior issue number and explains what changed.",
        "",
    ]
    for ci in closed:
        labels_str = ", ".join(ci["labels"]) if ci["labels"] else "(none)"
        lines.append(f"### #{ci['number']} — {ci['title']}")
        lines.append(f"- **Closed:** {ci['closedAt']}")
        lines.append(f"- **Labels:** {labels_str}")
        if ci["rationale"]:
            lines.append(
                f"- **Closing rationale (@{ci['rationale_author']}):** "
                f"{ci['rationale']}"
            )
        else:
            lines.append("- **Closing rationale:** (none recorded)")
        lines.append("")
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
        LABEL_RAISED: 2,
        LABEL_MERGED: 3,
        LABEL_REQUESTED: 4,
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

    # Closed-issue rationales — so the analyzer doesn't re-raise
    # findings the supervisor has already reasoned through and
    # rejected. See #260.
    closed_issues = _fetch_closed_auto_improve_issues(limit=50)
    closed_block = _closed_issues_block(closed_issues)

    # The system prompt, tool allowlist, and model choice all live
    # in `.claude/agents/cai-analyze.md`. Durable per-agent learnings
    # live in its `memory: project` pool. The wrapper only passes
    # dynamic per-run context (parsed signals, open issues,
    # closed-issue rationales) via stdin as the user message.
    user_message = (
        "## Parsed signals\n\n"
        "```json\n"
        f"{parsed_signals}\n"
        "```\n"
        f"{issues_block}"
        f"{closed_block}"
    )

    analyzer = _run(
        ["claude", "-p", "--agent", "cai-analyze"],
        input=user_message,
        capture_output=True,
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
    """Transition :pr-open issues whose linked PR was closed (unmerged) back to :raised.

    Returns the list of issues that were successfully recovered.
    """
    recovered: list[dict] = []
    for issue in issues:
        if LABEL_IN_PROGRESS in {lbl["name"] for lbl in issue.get("labels", [])}:
            continue
        pr = _find_linked_pr(issue["number"])
        if pr is None:
            continue
        state = (pr.get("state") or "").upper()
        if state == "CLOSED":
            issue_labels = {lbl["name"] for lbl in issue.get("labels", [])}
            raised_label = LABEL_AUDIT_RAISED if LABEL_AUDIT_RAISED in issue_labels else LABEL_RAISED
            if _set_labels(issue["number"], add=[raised_label], remove=[LABEL_PR_OPEN, LABEL_MERGE_BLOCKED]):
                print(
                    f"[{log_prefix}] recovered stale :pr-open on #{issue['number']} "
                    f"(PR #{pr['number']} closed unmerged)",
                    flush=True,
                )
                recovered.append(issue)
    return recovered


def _select_fix_target():
    """Return the oldest open issue eligible for the fix subagent.

    Eligible = labelled `:raised` or `:requested`, NOT labelled
    `:in-progress` or `:pr-open`.  `audit:raised` issues are handled
    exclusively by the audit-triage agent — only issues that triage
    re-labels to `auto-improve:raised` enter the fix pipeline.
    If no candidates are found, attempts to recover stale `:pr-open`
    issues whose linked PR was closed unmerged.
    """
    candidates: dict[int, dict] = {}
    for label in (LABEL_RAISED, LABEL_REQUESTED):
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
        # Recover stale :pr-open issues whose linked PR was closed (unmerged).
        # This handles cases where the verify step failed to transition them
        # back to :raised (e.g. due to GitHub search indexing delays).
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

    return min(candidates.values(), key=lambda i: i["createdAt"])


def _set_labels(issue_number: int, *, add: list[str] = (), remove: list[str] = ()) -> bool:
    """Add and/or remove labels on an issue. Returns True on success."""
    # Auto-add the base label for any state-prefixed label being added.
    # This is defensive: create_issue already applies base labels, but
    # auto-adding here self-heals issues that lost theirs.
    _BASE_NAMESPACES = {"auto-improve", "audit"}
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
            f"[cai fix] failed to update labels on #{issue_number}:\n{result.stderr}",
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


def _build_fix_user_message(issue: dict) -> str:
    """Build the dynamic per-run user message for the cai-fix agent.

    The system prompt, tool allowlist, and hard rules live in
    `.claude/agents/cai-fix.md`; durable per-agent learnings live
    in its `memory: project` pool. This function returns the issue
    body + any reviewer comments. The caller (cmd_fix) may prepend
    a ``## Selected Implementation Plan`` block produced by the
    plan → select pipeline before passing it to the agent.
    """
    issue_block = (
        f"## Issue\n\n"
        f"### #{issue['number']} — {issue['title']}\n\n"
        f"{issue.get('body') or '(no body)'}\n"
    )
    comments = issue.get("comments") or []
    if comments:
        issue_block += "\n### Comments\n\n"
        for c in comments:
            author = c.get("author", {}).get("login", "unknown")
            body = c.get("body", "")
            issue_block += f"**{author}:**\n{body}\n\n"
    return issue_block


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
    """Create GitHub issues raised by the fix pipeline. Returns count created."""
    created = 0
    for s in suggested:
        issue_body = (
            f"{s['body']}\n\n"
            f"---\n"
            f"_Raised by the fix pipeline while working on "
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


def _git(work_dir: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    cmd = ["git", "-C", str(work_dir)] + list(args)
    return subprocess.run(cmd, text=True, check=check, capture_output=True)


# ---------------------------------------------------------------------------
# Fix pipeline: plan → select → implement
# ---------------------------------------------------------------------------

_PLAN_SYSTEM_PROMPT = """\
You are a planning subagent for robotsix-cai. Your job is to analyze a \
GitHub issue and the codebase, then produce a detailed implementation plan \
describing exactly how to fix the issue.

You have read-only access to the codebase via Read, Grep, and Glob.

What you must do:
1. Read the issue carefully — understand the problem, evidence, and remediation.
2. Explore the codebase — use Grep and Read to find the relevant files, \
functions, and code paths.
3. Produce a plan — be specific: name files, functions, line ranges, and \
describe exactly what should change.

End your response with a fenced block in exactly this format:

## Plan

### Analysis
<brief analysis of the issue and relevant code>

### Files to change
<bullet list of files that need to be modified or created>

### Steps
<numbered list of specific implementation steps>

### Risks and considerations
<edge cases, risks, or things the implementer should watch out for>

Rules:
- Do NOT edit any files. You are read-only.
- Do NOT suggest changes outside the scope of the issue.
- Be concrete and specific — vague plans are useless.
- If the issue is unclear, say so in Analysis with empty Steps.
"""

_SELECT_SYSTEM_PROMPT = """\
You are a plan selection subagent for robotsix-cai. You receive multiple \
implementation plans produced by independent planning agents for the same \
issue. Your job is to evaluate each plan and select the best one.

You have read-only access to the codebase via Read, Grep, and Glob to \
verify claims made in the plans.

Evaluation criteria (in order of importance):
1. Correctness — Does the plan actually fix the issue?
2. Minimality — Does it make the smallest change necessary?
3. Specificity — Is it concrete enough to follow without guessing?
4. Safety — Does it avoid risky side effects?

What you must do:
1. Read each plan carefully.
2. If needed, use Read/Grep/Glob to verify claims in the plans.
3. Select the best plan or synthesize the best parts of multiple plans.

End your response with a fenced block in exactly this format:

## Selected Plan

### Rationale
<why this plan was selected over the others>

### Analysis
<analysis from the selected plan, refined if needed>

### Files to change
<files to change, refined if needed>

### Steps
<implementation steps, refined if needed>

### Risks and considerations
<risks, refined if needed>

Rules:
- Do NOT edit any files. You are read-only.
- You may combine the best elements of multiple plans.
- If no plan is adequate, provide your own following the same format.
- If the issue should not be fixed, say so in Rationale with empty Steps.
"""

_NUM_PLAN_AGENTS = 3


def _run_single_plan_agent(
    idx: int, user_message: str, work_dir: Path,
) -> subprocess.CompletedProcess:
    """Run one planning agent. Designed to be called in a thread pool."""
    print(f"[cai fix] starting plan agent {idx + 1}/{_NUM_PLAN_AGENTS}", flush=True)
    result = _run(
        [
            "claude", "-p",
            "--model", "claude-opus-4-6",
            "--system-prompt", _PLAN_SYSTEM_PROMPT,
            "--tools", "Read,Grep,Glob",
            "--permission-mode", "acceptEdits",
        ],
        input=user_message,
        cwd=str(work_dir),
        capture_output=True,
    )
    print(
        f"[cai fix] plan agent {idx + 1}/{_NUM_PLAN_AGENTS} finished "
        f"(exit {result.returncode})",
        flush=True,
    )
    return result


def _run_plan_phase(
    user_message: str, work_dir: Path,
) -> list[str] | None:
    """Run N planning agents in parallel. Return list of plan texts, or None on failure."""
    with concurrent.futures.ThreadPoolExecutor(max_workers=_NUM_PLAN_AGENTS) as pool:
        futures = [
            pool.submit(_run_single_plan_agent, i, user_message, work_dir)
            for i in range(_NUM_PLAN_AGENTS)
        ]
        results = [f.result() for f in futures]

    plans: list[str] = []
    for i, r in enumerate(results):
        if r.returncode != 0:
            print(
                f"[cai fix] plan agent {i + 1} failed (exit {r.returncode}): "
                f"{r.stderr[:500]}",
                file=sys.stderr,
            )
            continue
        output = (r.stdout or "").strip()
        if output:
            plans.append(output)

    if not plans:
        print("[cai fix] all plan agents failed; aborting", file=sys.stderr)
        return None

    print(f"[cai fix] collected {len(plans)}/{_NUM_PLAN_AGENTS} plans", flush=True)
    return plans


def _run_select_phase(
    user_message: str, plans: list[str], work_dir: Path,
) -> str | None:
    """Run the selection agent. Return the selected plan text, or None on failure."""
    # Build the selection prompt with all plans + the original issue.
    parts = [user_message, "\n---\n"]
    for i, plan in enumerate(plans):
        parts.append(f"\n## Plan {i + 1}\n\n{plan}\n")

    select_input = "".join(parts)
    print("[cai fix] running plan selection agent", flush=True)
    result = _run(
        [
            "claude", "-p",
            "--model", "claude-opus-4-6",
            "--system-prompt", _SELECT_SYSTEM_PROMPT,
            "--tools", "Read,Grep,Glob",
            "--permission-mode", "acceptEdits",
        ],
        input=select_input,
        cwd=str(work_dir),
        capture_output=True,
    )
    if result.returncode != 0:
        print(
            f"[cai fix] selection agent failed (exit {result.returncode}): "
            f"{result.stderr[:500]}",
            file=sys.stderr,
        )
        return None

    output = (result.stdout or "").strip()
    if not output:
        print("[cai fix] selection agent returned empty output", file=sys.stderr)
        return None

    print("[cai fix] plan selection complete", flush=True)
    return output


def cmd_fix(args) -> int:
    """Run the fix pipeline against one eligible issue."""
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
    origin_raised_label = LABEL_RAISED
    print(f"[cai fix] picked #{issue_number}: {title}", flush=True)

    # 1. Lock — set :in-progress, drop :raised and :requested.
    if not _set_labels(
        issue_number,
        add=[LABEL_IN_PROGRESS],
        remove=[LABEL_RAISED, LABEL_REQUESTED],
    ):
        print(f"[cai fix] could not lock #{issue_number}", file=sys.stderr)
        log_run("fix", repo=REPO, issue=issue_number, result="lock_failed", exit=1)
        return 1
    print(f"[cai fix] locked #{issue_number} (label {LABEL_IN_PROGRESS})", flush=True)

    # Make sure git can authenticate over HTTPS via the gh token. This
    # is also done in entrypoint.sh, but redoing it here is cheap and
    # idempotent and lets ad-hoc `docker run` invocations work too.
    _run(["gh", "auth", "setup-git"], capture_output=True)

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

        # 5. Three-phase fix pipeline: plan → select → implement.
        user_message = _build_fix_user_message(issue)

        # 5a. Planning phase — run N plan agents in parallel.
        print(f"[cai fix] starting planning phase ({_NUM_PLAN_AGENTS} agents)", flush=True)
        plans = _run_plan_phase(user_message, work_dir)
        if plans is None:
            rollback()
            log_run("fix", repo=REPO, issue=issue_number,
                    result="plan_phase_failed", exit=1)
            return 1

        # 5b. Selection phase — pick the best plan.
        selected_plan = _run_select_phase(user_message, plans, work_dir)
        if selected_plan is None:
            rollback()
            log_run("fix", repo=REPO, issue=issue_number,
                    result="select_phase_failed", exit=1)
            return 1

        # 5c. Implementation phase — run the cai-fix agent with the
        #     selected plan prepended to the user message.
        impl_message = (
            f"## Selected Implementation Plan\n\n"
            f"The following plan was selected by the planning pipeline. "
            f"Follow it to implement the fix.\n\n"
            f"{selected_plan}\n\n"
            f"---\n\n"
            f"{user_message}"
        )
        print(f"[cai fix] running implementation agent in {work_dir}", flush=True)
        # `acceptEdits` auto-accepts file edits (Read/Edit/Write/Grep/Glob)
        # without prompting. We don't use `bypassPermissions` because
        # claude-code refuses it when running as root inside the container,
        # and `acceptEdits` is sufficient for code-editing fixes.
        agent = _run(
            ["claude", "-p", "--agent", "cai-fix",
             "--model", "claude-opus-4-6",
             "--permission-mode", "acceptEdits"],
            input=impl_message,
            cwd=str(work_dir),
            capture_output=True,
        )
        if agent.stdout:
            print(agent.stdout, flush=True)
        if agent.returncode != 0:
            print(
                f"[cai fix] implementation agent failed (exit {agent.returncode}):\n"
                f"{agent.stderr}",
                file=sys.stderr,
            )
            rollback()
            log_run("fix", repo=REPO, issue=issue_number,
                    result="subagent_failed", exit=agent.returncode)
            return agent.returncode

        # 5d. Create any suggested issues the subagent raised.
        agent_text = agent.stdout or ""
        suggested = _parse_suggested_issues(agent_text)
        if suggested:
            n = _create_suggested_issues(suggested, issue_number)
            print(f"[cai fix] created {n}/{len(suggested)} suggested issue(s)", flush=True)

        # 6. Inspect the working tree. Empty diff = deliberate no-action.
        status = _git(work_dir, "status", "--porcelain", check=False)
        if not status.stdout.strip():
            reasoning = (agent.stdout or "").strip()[:2000]
            print(
                f"[cai fix] subagent produced no changes for #{issue_number}; "
                "marking auto-improve:no-action",
                flush=True,
            )
            # Post the agent's reasoning as a comment on the issue
            comment_body = (
                f"## Fix subagent: no action needed\n\n"
                f"{reasoning}\n\n"
                f"---\n"
                f"_Set by `cai fix` after the subagent reviewed and decided "
                f"no code change was needed. Re-label to "
                f"`{origin_raised_label}` to retry, or close if you agree._"
            )
            _run(
                ["gh", "issue", "comment", str(issue_number),
                 "--repo", REPO,
                 "--body", comment_body],
                capture_output=True,
            )
            # Transition: in-progress -> no-action (NOT back to :raised)
            if not _set_labels(
                issue_number,
                add=[LABEL_NO_ACTION],
                remove=[LABEL_IN_PROGRESS],
            ):
                print(
                    f"[cai fix] WARNING: label transition to :no-action failed for "
                    f"#{issue_number}; retrying",
                    flush=True,
                )
                if not _set_labels(
                    issue_number,
                    add=[LABEL_NO_ACTION],
                    remove=[LABEL_IN_PROGRESS],
                ):
                    print(
                        f"[cai fix] WARNING: label transition to :no-action failed twice for "
                        f"#{issue_number} — issue may be stuck without a lifecycle label",
                        file=sys.stderr, flush=True,
                    )
                    rollback()
                    log_run("fix", repo=REPO, issue=issue_number,
                            result="label_transition_failed", exit=1)
                    return 1
            locked = False
            log_run("fix", repo=REPO, issue=issue_number,
                    result="no_action_needed", exit=0)
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
            f"_Auto-generated by `cai fix`. The fix pipeline runs autonomously "
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
    label auto-improve:pr-open. Returns a list of dicts with keys:
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
            "--json", "number,headRefName,comments",
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

        # Find the most recent commit date via `gh pr view`.
        try:
            pr_detail = _gh_json([
                "pr", "view", str(pr["number"]),
                "--repo", REPO,
                "--json", "commits,mergeable,mergeStateStatus",
            ])
            commits = pr_detail.get("commits", [])
            if commits:
                last_commit_date = commits[-1].get("committedDate", "")
            else:
                last_commit_date = ""
        except Exception:
            last_commit_date = ""

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
    to `:raised` lets the fix subagent open a fresh PR against the
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
            f"#{issue_number} to :raised so fix can retry",
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
            "linked issue has been reset to `auto-improve:raised` and "
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
        _set_labels(
            issue_number,
            add=[LABEL_RAISED],
            remove=[LABEL_PR_OPEN, LABEL_MERGE_BLOCKED, LABEL_REVISING],
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
        if not _set_labels(issue_number, add=[LABEL_REVISING]):
            print(
                f"[cai revise] could not lock #{issue_number}",
                file=sys.stderr,
            )
            log_run("revise", repo=REPO, pr=pr_number,
                    result="lock_failed", exit=1)
            had_failure = True
            continue

        _run(["gh", "auth", "setup-git"], capture_output=True)

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
                _set_labels(issue_number, remove=[LABEL_REVISING])
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
            #     no-op. The unified cai-revise subagent handles both
            #     conflict resolution AND review-comment addressing
            #     in one session, so the wrapper doesn't need to
            #     branch on `needs_rebase` anymore.
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
                _set_labels(issue_number, remove=[LABEL_REVISING])
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

            # 4. Fetch original issue body + current PR diff.
            try:
                issue_data = _gh_json([
                    "issue", "view", str(issue_number),
                    "--repo", REPO,
                    "--json", "number,title,body",
                ])
            except subprocess.CalledProcessError:
                issue_data = {"number": issue_number, "title": "(unknown)", "body": ""}

            diff_result = _run(
                ["gh", "pr", "diff", str(pr_number), "--repo", REPO],
                capture_output=True,
            )
            pr_diff = diff_result.stdout if diff_result.returncode == 0 else "(could not fetch diff)"

            # 5. Build the user message. The system prompt, tool
            #    allowlist (Bash + edit tools), and hard rules all
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
                f"{rebase_state_block}\n"
                f"## Original issue\n\n"
                f"### #{issue_data['number']} — {issue_data.get('title', '')}\n\n"
                f"{issue_data.get('body') or '(no body)'}\n\n"
                f"## Current PR diff\n\n"
                f"```diff\n{pr_diff}\n```\n\n"
                f"{comments_section}"
            )

            # 6. Invoke the declared cai-revise subagent.
            print(
                f"[cai revise] running cai-revise subagent in {work_dir}",
                flush=True,
            )
            agent = _run(
                ["claude", "-p", "--agent", "cai-revise",
                 "--permission-mode", "acceptEdits"],
                input=user_message,
                cwd=str(work_dir),
                capture_output=True,
            )
            if agent.stdout:
                print(agent.stdout, flush=True)

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
                _set_labels(issue_number, remove=[LABEL_REVISING])
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
                _set_labels(issue_number, remove=[LABEL_REVISING])
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
                _set_labels(issue_number, remove=[LABEL_REVISING])
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
            _set_labels(issue_number, remove=[LABEL_REVISING])
            log_run("revise", repo=REPO, pr=pr_number,
                    comments_addressed=len(comments), exit=0)

        except Exception as e:
            print(f"[cai revise] unexpected failure: {e!r}", file=sys.stderr)
            _set_labels(issue_number, remove=[LABEL_REVISING])
            log_run("revise", repo=REPO, pr=pr_number,
                    result="unexpected_error", exit=1)
            had_failure = True
        finally:
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

    # Handle MERGED transitions inline; CLOSED recovery uses the shared helper.
    remaining = []
    for issue in issues:
        num = issue["number"]
        pr = _find_linked_pr(num)
        if pr is None:
            print(f"[cai verify] #{num}: no linked PR found, leaving as-is", flush=True)
            continue
        state = (pr.get("state") or "").upper()
        if state == "MERGED":
            _set_labels(num, add=[LABEL_MERGED], remove=[LABEL_PR_OPEN, LABEL_MERGE_BLOCKED])
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
        remove = [l for l in (LABEL_IN_PROGRESS, LABEL_RAISED, LABEL_AUDIT_RAISED) if l in iss_labels]
        if _set_labels(issue_num, add=[LABEL_PR_OPEN], remove=remove):
            print(
                f"[cai verify] recovered #{issue_num}: added :pr-open "
                f"(open PR #{opr['number']} on branch {branch})",
                flush=True,
            )
            transitioned += 1

    print(f"[cai verify] done ({transitioned} transitioned)", flush=True)
    log_run("verify", repo=REPO, checked=len(issues), transitioned=transitioned, exit=0)
    return 0


# ---------------------------------------------------------------------------
# audit
# ---------------------------------------------------------------------------

_STALE_IN_PROGRESS_HOURS = 6
_STALE_NO_ACTION_DAYS = 7
_STALE_MERGED_DAYS = 14


def _cleanup_merged_branches() -> list[str]:
    """Delete remote branches for merged/closed PRs. Returns list of deleted branch names."""
    deleted: list[str] = []
    try:
        prs = _gh_json([
            "pr", "list",
            "--repo", REPO,
            "--state", "closed",
            "--json", "headRefName,state",
            "--limit", "100",
        ]) or []
    except subprocess.CalledProcessError:
        return deleted

    # Also fetch the list of remote branches to confirm they still exist.
    try:
        branches_data = _gh_json([
            "api", f"repos/{REPO}/branches",
            "--paginate",
        ]) or []
        remote_branches = {b["name"] for b in branches_data if isinstance(b, dict)}
    except (subprocess.CalledProcessError, Exception):
        remote_branches = None

    for pr in prs:
        branch = pr.get("headRefName", "")
        if not branch.startswith("auto-improve/"):
            continue
        # Skip if we know the branch no longer exists on the remote.
        if remote_branches is not None and branch not in remote_branches:
            continue
        result = _run([
            "gh", "api",
            "--method", "DELETE",
            f"repos/{REPO}/git/refs/heads/{branch}",
        ], capture_output=True)
        if result.returncode == 0:
            deleted.append(branch)
            print(f"[cai audit] deleted merged branch: {branch}", flush=True)

    return deleted


def _rollback_stale_in_progress() -> list[dict]:
    """Deterministic rollback: :in-progress or :revising issues with no recent activity.

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
            if "[fix]" not in line and "[revise]" not in line:
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
    threshold = _STALE_IN_PROGRESS_HOURS * 3600
    rolled_back = []

    for issue in issues:
        issue_num = issue["number"]
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
            lock_label = issue.get("_lock_label", LABEL_IN_PROGRESS)
            if lock_label == LABEL_REVISING:
                # Revising lock: just remove the lock, leave :pr-open.
                ok = _set_labels(issue_num, remove=[LABEL_REVISING])
            else:
                # In-progress lock: roll back to :raised.
                issue_labels = {lbl["name"] for lbl in issue.get("labels", [])}
                raised_label = LABEL_AUDIT_RAISED if LABEL_AUDIT_RAISED in issue_labels else LABEL_RAISED
                ok = _set_labels(
                    issue_num,
                    add=[raised_label],
                    remove=[LABEL_IN_PROGRESS],
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
    """Roll stale :no-action issues back to :raised so fix can retry with new context."""
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
        ok = _set_labels(issue_num, add=[LABEL_PR_NEEDS_HUMAN])
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

    # Step 1b: Delete remote branches for already-merged/closed PRs.
    deleted_branches = _cleanup_merged_branches()
    if deleted_branches:
        print(
            f"[cai audit] cleaned up {len(deleted_branches)} merged branch(es)",
            flush=True,
        )

    # Step 1c: Unstuck stale :no-action issues (roll back to :raised).
    unstuck_no_action = _unstuck_stale_no_action()

    # Step 1d: Flag stale :merged issues for human review.
    flagged_merged = _flag_stale_merged()

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
            "--json", "number,title,state,mergedAt,createdAt,headRefName,body",
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
            prs_section += (
                f"- PR #{pr['number']}: {pr['title']} "
                f"[{pr.get('state', 'unknown')}] "
                f"(created {pr['createdAt']}"
                f"{', merged ' + pr['mergedAt'] if pr.get('mergedAt') else ''})\n"
            )
    else:
        prs_section += "(none)\n"

    log_section = "## Log tail (last ~200 lines)\n\n```\n" + (log_tail or "(empty)") + "\n```\n"

    deterministic_section = ""
    if rolled_back:
        deterministic_section += "## Stale in-progress rollbacks performed this run\n\n"
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

    user_message = (
        f"{issues_section}\n"
        f"{prs_section}\n"
        f"{log_section}\n"
        f"{deterministic_section}"
    )

    # Step 3: Invoke the declared cai-audit subagent.
    audit = _run(
        ["claude", "-p", "--agent", "cai-audit"],
        input=user_message,
        capture_output=True,
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
                branches_cleaned=len(deleted_branches),
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
            branches_cleaned=len(deleted_branches),
            no_action_unstuck=len(unstuck_no_action),
            merged_flagged=len(flagged_merged),
            duration=dur, exit=published.returncode)
    return published.returncode


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
    triage = _run(
        ["claude", "-p", "--agent", "cai-audit-triage"],
        input=user_message,
        capture_output=True,
    )
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
            )
            print(
                f"[cai audit-triage] #{n}: escalated to audit:needs-human",
                flush=True,
            )
            escalated += 1

        else:
            # passthrough — relabel to auto-improve:raised so the fix
            # subagent picks it up (fix no longer selects audit:raised
            # directly, ensuring all audit issues go through triage first).
            _set_labels(
                n,
                add=[LABEL_RAISED],
                remove=[LABEL_AUDIT_RAISED],
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
    #    bind-mounted log directory. System prompt, tool allowlist
    #    (Read/Grep/Glob), and model (sonnet) all live in
    #    `.claude/agents/cai-code-audit.md`. Durable per-agent
    #    learnings live in its `memory: project` pool.
    memory = _read_code_audit_memory()

    memory_section = "## Memory from previous runs\n\n"
    if memory:
        memory_section += memory + "\n"
    else:
        memory_section += "(first run — no prior memory)\n"

    user_message = memory_section

    # 3. Invoke the declared cai-code-audit subagent.
    print(f"[cai code-audit] running agent in {work_dir}", flush=True)
    agent = _run(
        ["claude", "-p", "--agent", "cai-code-audit",
         "--permission-mode", "acceptEdits"],
        input=user_message,
        cwd=str(work_dir),
        capture_output=True,
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

    # 4. Save the memory update for next run.
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
    """Re-analyze the recent window to verify :merged issues are solved."""
    print("[cai confirm] checking merged issues against recent signals", flush=True)
    t0 = time.monotonic()

    # 1. Query open :merged issues.
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
                "--search", f"Refs #{num}",
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
    confirm = _run(
        ["claude", "-p", "--agent", "cai-confirm"],
        input=user_message,
        capture_output=True,
    )
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

    solved = 0
    unsolved = 0
    inconclusive = 0

    for issue_num, status, reasoning in verdicts:
        if issue_num not in merged_nums:
            continue
        if status == "solved":
            _set_labels(issue_num, add=[LABEL_SOLVED], remove=[LABEL_MERGED])
            _run(
                ["gh", "issue", "close", str(issue_num),
                 "--repo", REPO,
                 "--comment",
                 f"Confirmed solved: {reasoning}"],
                capture_output=True,
            )
            print(f"[cai confirm] #{issue_num}: solved — closed", flush=True)
            solved += 1
        elif status == "unsolved":
            _run(
                ["gh", "issue", "comment", str(issue_num),
                 "--repo", REPO,
                 "--body",
                 "Confirm check: fix did not eliminate the pattern in the recent window."],
                capture_output=True,
            )
            print(f"[cai confirm] #{issue_num}: unsolved — left as :merged", flush=True)
            unsolved += 1
        elif status == "inconclusive":
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
            # allowlist (Read/Grep/Glob/Agent), and hard rules all
            # live in `.claude/agents/cai-review-pr.md`. The wrapper
            # only passes dynamic per-run context via stdin.
            author_login = pr.get("author", {}).get("login", "unknown")
            user_message = (
                f"## PR metadata\n\n"
                f"- **Number:** #{pr_number}\n"
                f"- **Title:** {title}\n"
                f"- **Author:** @{author_login}\n"
                f"- **Base:** main\n"
                f"- **HEAD SHA:** {head_sha}\n\n"
                f"## PR diff\n\n"
                f"```diff\n{pr_diff}\n```\n"
            )

            # Invoke the declared cai-review-pr subagent.
            agent = _run(
                ["claude", "-p", "--agent", "cai-review-pr",
                 "--permission-mode", "acceptEdits"],
                input=user_message,
                cwd=str(work_dir),
                capture_output=True,
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

    threshold_rank = _CONFIDENCE_RANKS.get(_MERGE_THRESHOLD, 99)
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
        # re-evaluate. That way, when revise pushes a new commit (new
        # SHA), the bot naturally re-evaluates without requiring a
        # human to manually clear the label.

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
        agent = _run(
            ["claude", "-p", "--agent", "cai-merge"],
            input=user_message,
            capture_output=True,
        )
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
                if not _set_labels(issue_number, add=[LABEL_NO_ACTION], remove=[LABEL_PR_OPEN, LABEL_MERGE_BLOCKED]):
                    print(
                        f"[cai merge] WARNING: label transition to :no-action failed for "
                        f"#{issue_number} after closing PR #{pr_number}; retrying",
                        flush=True,
                    )
                    if not _set_labels(issue_number, add=[LABEL_NO_ACTION], remove=[LABEL_PR_OPEN, LABEL_MERGE_BLOCKED]):
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
                    if not _set_labels(issue_number, add=[LABEL_MERGE_BLOCKED]):
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
                if not _set_labels(issue_number, add=[LABEL_MERGED], remove=[LABEL_PR_OPEN, LABEL_MERGE_BLOCKED]):
                    print(
                        f"[cai merge] WARNING: label transition to :merged failed for "
                        f"#{issue_number} after merging PR #{pr_number}; retrying",
                        flush=True,
                    )
                    if not _set_labels(issue_number, add=[LABEL_MERGED], remove=[LABEL_PR_OPEN, LABEL_MERGE_BLOCKED]):
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
                if not _set_labels(issue_number, add=[LABEL_MERGE_BLOCKED]):
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
# Cycle (full pipeline without analyze)
# ---------------------------------------------------------------------------

def cmd_cycle(args) -> int:
    """Run verify → fix → revise → review-pr → merge → confirm in order."""
    print("[cai cycle] starting full cycle (no analyze)", flush=True)
    t0 = time.monotonic()

    steps = [
        ("verify", cmd_verify),
        ("fix", cmd_fix),
        ("revise", cmd_revise),
        ("review-pr", cmd_review_pr),
        ("merge", cmd_merge),
        ("confirm", cmd_confirm),
    ]

    results = {}
    failed = False
    for name, handler in steps:
        print(f"\n[cai cycle] === running step: {name} ===", flush=True)
        try:
            rc = handler(args)
        except Exception as exc:
            print(f"[cai cycle] step {name} raised {exc!r}", file=sys.stderr, flush=True)
            rc = 1
        results[name] = rc
        if rc != 0:
            print(f"[cai cycle] step {name} returned {rc}; continuing", flush=True)
            failed = True

    dur = f"{time.monotonic() - t0:.1f}s"
    summary = " ".join(f"{k}={v}" for k, v in results.items())
    print(f"\n[cai cycle] done in {dur} — {summary}", flush=True)
    log_run("cycle", repo=REPO, results=summary, duration=dur, exit=1 if failed else 0)
    return 1 if failed else 0


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(prog="cai")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="Smoke test if no transcripts exist")
    sub.add_parser("analyze", help="Run the analyzer + publish findings")

    fix_parser = sub.add_parser("fix", help="Run the fix pipeline")
    fix_parser.add_argument(
        "--issue", type=int, default=None,
        help="Target a specific issue number instead of picking the oldest",
    )

    sub.add_parser("revise", help="Iterate on open PRs based on review comments")
    sub.add_parser("verify", help="Update labels based on PR merge state")
    sub.add_parser("audit", help="Run the queue/PR consistency audit")
    sub.add_parser(
        "audit-triage",
        help="Autonomously resolve audit:raised findings (no PRs)",
    )
    sub.add_parser("code-audit", help="Audit repo source code for inconsistencies")
    sub.add_parser("confirm", help="Verify merged issues are actually solved")
    sub.add_parser("review-pr", help="Pre-merge consistency review of open PRs")
    sub.add_parser("merge", help="Confidence-gated auto-merge for bot PRs")
    sub.add_parser("cycle", help="Full cycle: verify, fix, revise, review-pr, merge, confirm")

    args = parser.parse_args()

    auth_rc = check_gh_auth()
    if auth_rc != 0:
        return auth_rc

    handlers = {
        "init": cmd_init,
        "analyze": cmd_analyze,
        "fix": cmd_fix,
        "revise": cmd_revise,
        "verify": cmd_verify,
        "audit": cmd_audit,
        "audit-triage": cmd_audit_triage,
        "code-audit": cmd_code_audit,
        "confirm": cmd_confirm,
        "review-pr": cmd_review_pr,
        "merge": cmd_merge,
        "cycle": cmd_cycle,
    }
    return handlers[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
