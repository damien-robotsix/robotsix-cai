"""Handler for the :raised / :triaging issue states.

Lifted from ``cmd_triage`` in ``cai.py`` as part of the FSM-dispatcher
refactor. The dispatcher picks the issue and guarantees that it is
currently at ``IssueState.RAISED`` or ``IssueState.TRIAGING`` before
calling :func:`handle_triage`.
"""

from __future__ import annotations

import re
import subprocess
import sys
import time

from cai_lib.cmd_helpers import _strip_stored_plan_block
from cai_lib.config import (
    LABEL_KIND_CODE,
    LABEL_KIND_MAINTENANCE,
    LABEL_TRIAGING,
    REPO,
)
from cai_lib.fsm import (
    Confidence,
    IssueState,
    apply_transition,
    get_issue_state,
)
from cai_lib.github import _gh_json, _set_labels
from cai_lib.logging_utils import log_run
from cai_lib.subprocess_utils import _run, _run_claude_p


# ---------------------------------------------------------------------------
# Handler-local verdict parsers (moved from cai.py)
# ---------------------------------------------------------------------------


def _parse_issue_triage_verdict(text: str) -> dict:
    """Parse the structured output from the cai-triage agent.

    Expected format (one field per line):
        RoutingDecision: DISMISS_DUPLICATE | DISMISS_RESOLVED | REFINE | HUMAN
        RoutingConfidence: LOW | MEDIUM | HIGH
        Kind: code | maintenance          (required for REFINE verdict)
        DuplicateOf: #N                   (required for DISMISS_DUPLICATE)
        Reasoning: <1-3 sentences>

    Returns a dict with lowercase keys: decision, confidence, kind,
    duplicate_of (int or None), reasoning. Returns an empty dict if the
    required fields cannot be parsed.
    """
    result: dict = {}
    for line in text.splitlines():
        m = re.match(r"^RoutingDecision:\s*(\w+)", line, re.IGNORECASE)
        if m:
            result["decision"] = m.group(1).upper()
            continue
        m = re.match(r"^RoutingConfidence:\s*(\w+)", line, re.IGNORECASE)
        if m:
            result["confidence"] = m.group(1).upper()
            continue
        m = re.match(r"^Kind:\s*(\w+)", line, re.IGNORECASE)
        if m:
            result["kind"] = m.group(1).lower()
            continue
        m = re.match(r"^DuplicateOf:\s*#?(\d+)", line, re.IGNORECASE)
        if m:
            result["duplicate_of"] = int(m.group(1))
            continue
        m = re.match(r"^Reasoning:\s*(.+)$", line, re.IGNORECASE)
        if m:
            result["reasoning"] = m.group(1).strip()
            continue
    return result


_TRIAGE_SKIP_CONFIDENCE_RE = re.compile(
    r"^\s*SkipConfidence\s*[:=]\s*(LOW|MEDIUM|HIGH)\s*$",
    re.IGNORECASE | re.MULTILINE,
)

_TRIAGE_PLAN_BLOCK_RE = re.compile(
    r"^\s*Plan\s*[:=]\s*(.+?)(?=^\s*\w+\s*[:=]|\Z)",
    re.IGNORECASE | re.MULTILINE | re.DOTALL,
)

_TRIAGE_OPS_BLOCK_RE = re.compile(
    r"^\s*Ops\s*[:=]\s*(.+?)(?=^\s*\w+\s*[:=]|\Z)",
    re.IGNORECASE | re.MULTILINE | re.DOTALL,
)


def _parse_triage_skip_confidence(text: str) -> "Confidence | None":
    """Extract ``SkipConfidence: LOW|MEDIUM|HIGH`` from cai-triage output."""
    if not text:
        return None
    m = _TRIAGE_SKIP_CONFIDENCE_RE.search(text)
    if not m:
        return None
    return Confidence[m.group(1).upper()]


def _parse_triage_plan(text: str) -> "str | None":
    """Extract ``Plan: <body>`` block from cai-triage output."""
    if not text:
        return None
    m = _TRIAGE_PLAN_BLOCK_RE.search(text)
    return m.group(1).strip() if m else None


def _parse_triage_ops(text: str) -> "str | None":
    """Extract ``Ops: <list>`` block from cai-triage output."""
    if not text:
        return None
    m = _TRIAGE_OPS_BLOCK_RE.search(text)
    return m.group(1).strip() if m else None


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------


def handle_triage(issue: dict) -> int:
    """Triage an issue at ``:raised`` or ``:triaging`` and route it via the FSM.

    Moves RAISED → TRIAGING (if not already there), runs the cai-triage agent
    inline, then executes the verdict:
    - DISMISS_DUPLICATE / DISMISS_RESOLVED at HIGH confidence → close issue.
    - PLAN_APPROVE with HIGH skip-confidence + code kind → TRIAGING → PLAN_APPROVED
      (embedded plan in issue body).
    - APPLY with HIGH skip-confidence + maintenance kind → TRIAGING → APPLYING.
    - REFINE (or DISMISS/PLAN_APPROVE/APPLY at non-HIGH confidence) →
      TRIAGING → REFINING + kind label.
    - HUMAN → TRIAGING → HUMAN_NEEDED.
    """
    t0 = time.monotonic()

    issue_number = issue["number"]
    title = issue["title"]
    issue_labels = [lb["name"] for lb in issue.get("labels", [])]
    current_state = get_issue_state(issue_labels)

    print(f"[cai triage] picked #{issue_number}: {title}", flush=True)

    # 1. RAISED → TRIAGING (skip if already at :triaging — same handler resumes).
    if current_state == IssueState.RAISED:
        apply_transition(
            issue_number, "raise_to_triaging",
            current_labels=issue_labels,
            log_prefix="cai triage",
        )

    # 2. Gather context: other open auto-improve* issues + recent PRs.
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
        recent_prs = _gh_json([
            "pr", "list", "--repo", REPO,
            "--state", "all",
            "--json", "number,title,state,mergedAt",
            "--limit", "30",
        ]) or []
    except subprocess.CalledProcessError:
        recent_prs = []

    # Exclude the issue being triaged from context.
    context_issues = [ci for ci in context_issues if ci["number"] != issue_number]

    # 3. Build user message.
    ci_lines = "\n".join(
        f"  #{ci['number']} [{', '.join(lb['name'] for lb in ci.get('labels', []))}] {ci['title']}"
        for ci in context_issues[:50]
    )
    pr_lines = "\n".join(
        f"  #{pr['number']} [{pr['state']}] {pr['title']}"
        + (f" (merged {pr['mergedAt'][:10]})" if pr.get("mergedAt") else "")
        for pr in recent_prs
    )
    user_message = (
        f"## Issue to triage: #{issue_number}\n\n"
        f"**Title:** {title}\n\n"
        f"**Body:**\n{issue.get('body', '')}\n\n"
        f"## Other open auto-improve issues\n{ci_lines or '(none)'}\n\n"
        f"## Recent PRs\n{pr_lines or '(none)'}\n"
    )

    # 4. Run cai-triage agent.
    result = _run_claude_p(
        ["claude", "-p", "--agent", "cai-triage",
         "--dangerously-skip-permissions"],
        category="triage",
        agent="cai-triage",
        input=user_message,
    )
    print(result.stdout, flush=True)

    if result.returncode != 0:
        print(
            f"[cai triage] claude -p failed (exit {result.returncode}):\n"
            f"{result.stderr}",
            flush=True,
        )
        dur = f"{int(time.monotonic() - t0)}s"
        log_run("triage", repo=REPO, issue=issue_number,
                duration=dur, result="agent_failed", exit=result.returncode)
        return result.returncode

    # 5. Parse verdict.
    verdict = _parse_issue_triage_verdict(result.stdout)
    decision   = verdict.get("decision", "")
    confidence = verdict.get("confidence", "")
    kind       = verdict.get("kind", "code")
    dup_of     = verdict.get("duplicate_of")
    reasoning  = verdict.get("reasoning", "(no reasoning)")

    print(
        f"[cai triage] verdict: decision={decision} confidence={confidence} "
        f"kind={kind} reasoning={reasoning}",
        flush=True,
    )

    dur = f"{int(time.monotonic() - t0)}s"

    # 6. Execute verdict.
    if decision in ("DISMISS_DUPLICATE", "DISMISS_RESOLVED") and confidence == "HIGH":
        reason_flag = "not-planned"
        if decision == "DISMISS_DUPLICATE" and dup_of:
            comment = f"Closed as duplicate of #{dup_of} by cai-triage. Reasoning: {reasoning}"
        else:
            comment = f"Closed as resolved by cai-triage. Reasoning: {reasoning}"
        close_res = _run(
            ["gh", "issue", "close", str(issue_number),
             "--repo", REPO,
             "--reason", reason_flag,
             "--comment", comment],
            capture_output=True,
        )
        if close_res.returncode != 0:
            print(f"[cai triage] gh issue close failed:\n{close_res.stderr}", file=sys.stderr)
            log_run("triage", repo=REPO, issue=issue_number,
                    duration=dur, result="close_failed", exit=1)
            return 1
        # Remove the triaging label (issue is closed but label cleanup is tidy).
        _set_labels(issue_number, remove=[LABEL_TRIAGING], log_prefix="cai triage")
        action_taken = decision.lower()
    elif decision == "HUMAN":
        apply_transition(
            issue_number, "triaging_to_human",
            current_labels=[LABEL_TRIAGING],
            log_prefix="cai triage",
        )
        action_taken = "human"
    elif decision in ("PLAN_APPROVE", "APPLY"):
        # Dual-gate skip-ahead logic: both RoutingDecision and SkipConfidence
        # must be HIGH for the fast path to fire; otherwise fall through to REFINE.
        skip_conf = _parse_triage_skip_confidence(result.stdout)
        kind_label = LABEL_KIND_MAINTENANCE if kind == "maintenance" else LABEL_KIND_CODE
        if skip_conf is None or skip_conf < Confidence.HIGH:
            print(
                f"[cai triage] #{issue_number}: {decision} but "
                f"SkipConfidence={skip_conf} < HIGH — falling through to REFINE",
                flush=True,
            )
            apply_transition(
                issue_number, "triaging_to_refining",
                current_labels=[LABEL_TRIAGING],
                log_prefix="cai triage",
            )
            _set_labels(issue_number, add=[kind_label], log_prefix="cai triage")
            action_taken = "refine"
        elif decision == "PLAN_APPROVE":
            plan_body = _parse_triage_plan(result.stdout)
            if plan_body:
                existing_body = issue.get("body") or ""
                stripped_body = _strip_stored_plan_block(existing_body)
                plan_section = (
                    f"<!-- cai-plan-start -->\n{plan_body}\n<!-- cai-plan-end -->"
                )
                new_body = f"{stripped_body}\n\n{plan_section}"
                _run(
                    ["gh", "issue", "edit", str(issue_number),
                     "--repo", REPO, "--body", new_body],
                    capture_output=True, check=True,
                )
            apply_transition(
                issue_number, "triaging_to_plan_approved",
                current_labels=[LABEL_TRIAGING],
                log_prefix="cai triage",
            )
            _set_labels(issue_number, add=[kind_label], log_prefix="cai triage")
            print(
                f"[cai triage] #{issue_number}: PLAN_APPROVE with HIGH SkipConfidence "
                f"— advancing to plan-approved",
                flush=True,
            )
            action_taken = "plan_approve"
        else:  # decision == "APPLY"
            ops_body = _parse_triage_ops(result.stdout)
            if ops_body:
                existing_body = issue.get("body") or ""
                stripped_body = _strip_stored_plan_block(existing_body)
                ops_section = (
                    f"<!-- cai-plan-start -->\n{ops_body}\n<!-- cai-plan-end -->"
                )
                new_body = f"{stripped_body}\n\n{ops_section}"
                _run(
                    ["gh", "issue", "edit", str(issue_number),
                     "--repo", REPO, "--body", new_body],
                    capture_output=True, check=True,
                )
            apply_transition(
                issue_number, "triaging_to_applying",
                current_labels=[LABEL_TRIAGING],
                log_prefix="cai triage",
            )
            _set_labels(issue_number, add=[kind_label], log_prefix="cai triage")
            print(
                f"[cai triage] #{issue_number}: APPLY with HIGH SkipConfidence "
                f"— advancing to applying",
                flush=True,
            )
            action_taken = "applying"
    else:
        # REFINE, or DISMISS at sub-HIGH confidence → fall through to REFINE.
        if decision in ("DISMISS_DUPLICATE", "DISMISS_RESOLVED"):
            print(
                f"[cai triage] #{issue_number}: dismiss at {confidence} confidence "
                f"— downgrading to REFINE",
                flush=True,
            )
        apply_transition(
            issue_number, "triaging_to_refining",
            current_labels=[LABEL_TRIAGING],
            log_prefix="cai triage",
        )
        kind_label = LABEL_KIND_MAINTENANCE if kind == "maintenance" else LABEL_KIND_CODE
        _set_labels(issue_number, add=[kind_label], log_prefix="cai triage")
        action_taken = "refine"

    log_run("triage", repo=REPO, issue=issue_number,
            duration=dur, result=action_taken, exit=0)
    return 0
