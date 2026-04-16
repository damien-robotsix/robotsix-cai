"""Handler for the :raised / :triaging issue states.

Lifted from ``cmd_triage`` in ``cai.py`` as part of the FSM-dispatcher
refactor. The dispatcher picks the issue and guarantees that it is
currently at ``IssueState.RAISED`` or ``IssueState.TRIAGING`` before
calling :func:`handle_triage`.
"""

from __future__ import annotations

import json
import sys
import time

from cai_lib.cmd_helpers import _strip_stored_plan_block
from cai_lib.config import (
    LABEL_KIND_CODE,
    LABEL_KIND_MAINTENANCE,
    LABEL_TRIAGING,
    REPO,
)
from cai_lib.dup_check import check_duplicate_or_resolved
from cai_lib.fsm import (
    Confidence,
    IssueState,
    apply_transition,
    get_issue_state,
)
from cai_lib.github import _post_issue_comment, _set_labels
from cai_lib.logging_utils import log_run
from cai_lib.subprocess_utils import _run, _run_claude_p


# ---------------------------------------------------------------------------
# JSON schema for structured triage verdict (forced tool-use via --json-schema)
# ---------------------------------------------------------------------------

_TRIAGE_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "routing_decision": {
            "type": "string",
            "enum": ["REFINE", "DISMISS_DUPLICATE", "DISMISS_RESOLVED", "PLAN_APPROVE", "APPLY", "HUMAN"],
            "description": "Verdict routing decision",
        },
        "routing_confidence": {
            "type": "string",
            "enum": ["LOW", "MEDIUM", "HIGH"],
        },
        "kind": {
            "type": "string",
            "enum": ["code", "maintenance"],
        },
        "duplicate_of": {
            "type": "integer",
            "description": "Issue number; required only for DISMISS_DUPLICATE",
        },
        "reasoning": {
            "type": "string",
            "description": "1-3 sentences explaining the routing decision",
        },
        "skip_confidence": {
            "type": "string",
            "enum": ["LOW", "MEDIUM", "HIGH"],
            "description": "Required when routing_decision is PLAN_APPROVE or APPLY",
        },
        "plan": {
            "type": "string",
            "description": "Full markdown plan body; required when skip_confidence is HIGH and routing_decision is PLAN_APPROVE",
        },
        "ops": {
            "type": "string",
            "description": "Ordered markdown list of operations; required when skip_confidence is HIGH and routing_decision is APPLY",
        },
    },
    "required": ["routing_decision", "routing_confidence", "kind", "reasoning"],
}


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------


def handle_triage(issue: dict) -> int:
    """Triage an issue at ``:raised`` or ``:triaging`` and route it via the FSM.

    Moves RAISED → TRIAGING (if not already there), runs the cai-triage agent
    inline, then executes the verdict:
    - PLAN_APPROVE with HIGH skip-confidence + code kind → TRIAGING → PLAN_APPROVED
      (embedded plan in issue body).
    - APPLY with HIGH skip-confidence + maintenance kind → TRIAGING → APPLYING.
    - REFINE (or PLAN_APPROVE/APPLY at non-HIGH confidence) →
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

    # 1b. Cheap pre-check: cai-dup-check (haiku) decides whether the
    # issue is an obvious duplicate of another open issue or has
    # already been resolved by a recent merged PR. At HIGH
    # confidence we close directly and skip the heavier triage
    # agent. Any other outcome (NONE, MEDIUM/LOW confidence, parse
    # failure, agent failure) falls through.
    dup_verdict = check_duplicate_or_resolved(issue)
    if dup_verdict is not None and dup_verdict.should_close:
        if dup_verdict.verdict == "DUPLICATE":
            comment = (
                f"Closed as duplicate of #{dup_verdict.target} by "
                f"cai-dup-check. Reasoning: {dup_verdict.reasoning}"
            )
        else:
            comment = (
                f"Closed as resolved by {dup_verdict.commit_sha} "
                f"(cai-dup-check). Reasoning: {dup_verdict.reasoning}"
            )
        close_res = _run(
            ["gh", "issue", "close", str(issue_number),
             "--repo", REPO,
             "--reason", "not-planned",
             "--comment", comment],
            capture_output=True,
        )
        if close_res.returncode == 0:
            _set_labels(issue_number, remove=[LABEL_TRIAGING], log_prefix="cai triage")
            dur = f"{int(time.monotonic() - t0)}s"
            action = (
                "dup_check_duplicate" if dup_verdict.verdict == "DUPLICATE"
                else "dup_check_resolved"
            )
            log_run("triage", repo=REPO, issue=issue_number,
                    duration=dur, result=action, exit=0)
            print(
                f"[cai triage] #{issue_number}: closed by cai-dup-check "
                f"({dup_verdict.verdict}, reasoning={dup_verdict.reasoning})",
                flush=True,
            )
            return 0
        print(
            f"[cai triage] gh issue close failed in dup-check path; "
            f"falling through to triage agent:\n{close_res.stderr}",
            file=sys.stderr,
        )

    # 2. Build user message.
    user_message = (
        f"## Issue to triage: #{issue_number}\n\n"
        f"**Title:** {title}\n\n"
        f"**Body:**\n{issue.get('body', '')}\n"
    )

    # 3. Run cai-triage agent with structured JSON output.
    result = _run_claude_p(
        ["claude", "-p", "--agent", "cai-triage",
         "--dangerously-skip-permissions",
         "--json-schema", json.dumps(_TRIAGE_JSON_SCHEMA)],
        category="triage",
        agent="cai-triage",
        input=user_message,
    )

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

    # 5. Parse verdict from structured JSON.
    try:
        tool_input = json.loads(result.stdout)
    except (json.JSONDecodeError, ValueError) as exc:
        print(
            f"[cai triage] failed to parse JSON verdict: {exc}; "
            f"stdout starts with: {(result.stdout or '')[:120]!r}",
            file=sys.stderr,
            flush=True,
        )
        tool_input = {}

    decision   = tool_input.get("routing_decision", "")
    confidence = tool_input.get("routing_confidence", "")
    kind       = tool_input.get("kind", "code")
    reasoning  = tool_input.get("reasoning", "(no reasoning)")

    print(
        f"[cai triage] verdict: decision={decision} confidence={confidence} "
        f"kind={kind} reasoning={reasoning}",
        flush=True,
    )

    dur = f"{int(time.monotonic() - t0)}s"

    # 6. Execute verdict.
    if decision == "HUMAN":
        _post_issue_comment(
            issue_number,
            f"cai-triage routed this issue to `:human-needed` "
            f"(confidence={confidence or 'MISSING'}).\n\n"
            f"**Reasoning:** {reasoning}",
            log_prefix="cai triage",
        )
        apply_transition(
            issue_number, "triaging_to_human",
            current_labels=[LABEL_TRIAGING],
            log_prefix="cai triage",
        )
        action_taken = "human"
    elif decision in ("PLAN_APPROVE", "APPLY"):
        # Dual-gate skip-ahead logic: both RoutingDecision and SkipConfidence
        # must be HIGH for the fast path to fire; otherwise fall through to REFINE.
        skip_conf_str = tool_input.get("skip_confidence")
        skip_conf = Confidence[skip_conf_str] if skip_conf_str else None
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
            plan_body = tool_input.get("plan")
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
            ops_body = tool_input.get("ops")
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
        # REFINE or any unrecognised decision → fall through to REFINE.
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
