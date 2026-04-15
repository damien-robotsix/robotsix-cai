"""cai_lib.actions.plan — handlers for the planning phase of the FSM.

Two handlers:

- :func:`handle_plan` covers :class:`IssueState.REFINED` (entry) and
  :class:`IssueState.PLANNING` (resume). It runs the serial 2-plan →
  select pipeline and stores the chosen plan on the issue body, then
  transitions to :class:`IssueState.PLANNED`.
- :func:`handle_plan_gate` covers :class:`IssueState.PLANNED` and
  auto-advances the issue to ``PLAN_APPROVED`` via the confidence gate
  (below-threshold diverts to ``:human-needed`` with a pending marker).

Derived from ``cmd_plan`` in ``cai.py`` — behaviour is preserved as
closely as possible. The dispatcher is responsible for fetching the
issue; direct-invoke ``args.issue`` handling and pickup queries are
intentionally dropped here.
"""
from __future__ import annotations

import shutil
import sys
import time
import uuid

from pathlib import Path

from cai_lib.config import (
    REPO,
    LABEL_IN_PROGRESS,
    LABEL_PR_OPEN,
    LABEL_REFINED,
    LABEL_PLANNING,
    LABEL_PLANNED,
)
from cai_lib.github import _gh_json, _build_issue_block
from cai_lib.subprocess_utils import _run, _run_claude_p
from cai_lib.logging_utils import log_run
from cai_lib.cmd_helpers import (
    _work_directory_block,
    _strip_stored_plan_block,
    _fetch_previous_fix_attempts,
    _build_attempt_history_block,
)
from cai_lib.fsm import (
    apply_transition,
    apply_transition_with_confidence,
    render_pending_marker,
    strip_pending_marker,
    IssueState,
    get_issue_state,
)


# ---------------------------------------------------------------------------
# Helpers (moved from cai.py — only used by the plan phase).
# ---------------------------------------------------------------------------

def _select_plan_target(issue_number: int | None = None):
    """Return the oldest open :refined issue eligible for planning, or None.

    If *issue_number* is given, fetch that issue directly (validating it is
    open and not locked).  Otherwise query for the oldest :refined issue
    that is not :in-progress or :pr-open.
    """
    import subprocess  # local import — keeps module-level deps tight

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
        category="plan.plan",
        agent="cai-plan",
        input=user_message,
        cwd="/app",
    )
    if result.returncode != 0:
        return f"(Plan {plan_index} failed: exit {result.returncode})"
    return result.stdout or ""


_SELECT_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "plan": {
            "type": "string",
            "description": "Full text of the chosen plan.",
        },
        "confidence": {
            "type": "string",
            "enum": ["HIGH", "MEDIUM", "LOW"],
            "description": "Confidence level for the selected plan.",
        },
        "note": {
            "type": "string",
            "description": "Optional note flagging critical weaknesses for the fix agent.",
        },
    },
    "required": ["plan", "confidence"],
}


def _run_select_agent(
    issue: dict, plans: list[str], work_dir: Path,
) -> "tuple[str, Confidence] | None":
    """Run the cai-select agent and return ``(plan_text, confidence)``.

    Invokes the Claude Code subagent with ``--json-schema`` so the final
    output is a structured JSON object ({plan, confidence, note?}) rather
    than free-form text. Removes the regex-based confidence extraction that
    previously diverted sound plans to ``:human-needed`` when the model
    drifted from the ``Confidence: …`` trailer format.

    Returns ``None`` on subprocess or parse failure; otherwise returns
    the cleaned plan text (with optional ``> **Note:** …`` blockquote
    prepended) and the :class:`~cai_lib.fsm.Confidence` enum member.
    """
    import json

    from cai_lib.fsm import Confidence

    user_message = _work_directory_block(work_dir) + "\n"
    user_message += _build_issue_block(issue)
    user_message += "\n---\n\n# Candidate Plans\n\n"
    for i, plan in enumerate(plans, 1):
        user_message += f"## Plan {i}\n\n{plan}\n\n---\n\n"

    result = _run_claude_p(
        ["claude", "-p", "--agent", "cai-select",
         "--dangerously-skip-permissions",
         "--json-schema", json.dumps(_SELECT_JSON_SCHEMA),
         "--add-dir", str(work_dir)],
        category="plan.select",
        agent="cai-select",
        input=user_message,
        cwd="/app",
    )
    if result.returncode != 0 or not (result.stdout or "").strip():
        print("[cai plan] cai-select produced no output", file=sys.stderr)
        return None

    try:
        payload = json.loads(result.stdout)
    except (json.JSONDecodeError, ValueError) as exc:
        print(
            f"[cai plan] cai-select output was not valid JSON: {exc}; "
            f"stdout starts with: {(result.stdout or '')[:120]!r}",
            file=sys.stderr,
        )
        return None

    plan_text = payload.get("plan", "") or ""
    confidence_str = (payload.get("confidence") or "").upper()
    note = payload.get("note", "") or ""

    if note:
        plan_text = f"> **Note:** {note}\n\n{plan_text}"

    try:
        confidence = Confidence[confidence_str]
    except KeyError:
        print(
            f"[cai plan] cai-select returned invalid confidence: {confidence_str!r}",
            file=sys.stderr,
        )
        return None

    return plan_text.rstrip() + "\n", confidence


def _run_plan_select_pipeline(
    issue: dict, work_dir: Path, attempt_history_block: str = "",
) -> "tuple[str, Confidence | None] | None":
    """Run the serial 2-plan → select pipeline.

    Plan 1 runs first; Plan 2 receives Plan 1's output and is asked
    to find an alternative approach. The select agent then picks the
    best and emits a trailing ``Confidence: HIGH|MEDIUM|LOW`` line
    indicating how sure it is that the chosen plan will succeed.

    Returns ``(plan_text, confidence)`` — plan text and confidence arrive
    as separate structured fields from the select agent's forced tool-use
    call. ``confidence`` is a :class:`~cai_lib.fsm.Confidence` enum member,
    or ``None`` when the select agent fails (treated as below-threshold by
    the caller).
    Returns ``None`` if the pipeline fails to produce any output.
    """
    issue_number = issue["number"]

    # Step 1: Run Plan 1.
    print(f"[cai plan] running plan agent 1/2 for #{issue_number}", flush=True)
    plan1 = _run_plan_agent(issue, 1, work_dir, attempt_history_block)
    print(f"[cai plan] plan 1: {len(plan1)} chars", flush=True)

    # Step 2: Run Plan 2 with knowledge of Plan 1, asking for an alternative.
    print(f"[cai plan] running plan agent 2/2 for #{issue_number}", flush=True)
    plan2 = _run_plan_agent(issue, 2, work_dir, attempt_history_block, first_plan=plan1)
    print(f"[cai plan] plan 2: {len(plan2)} chars", flush=True)

    plans = [plan1, plan2]

    # Step 3: Run the select agent to pick the best plan.
    print(f"[cai plan] running select agent for #{issue_number}", flush=True)
    select_result = _run_select_agent(issue, plans, work_dir)
    if select_result is None:
        print("[cai plan] select agent produced no output; skipping pipeline", flush=True)
        return None

    plan_text, confidence = select_result
    conf_name = confidence.name if confidence else "MISSING"
    print(
        f"[cai plan] select agent produced {len(plan_text)} chars "
        f"(confidence={conf_name})",
        flush=True,
    )
    return plan_text, confidence


# ---------------------------------------------------------------------------
# Handlers.
# ---------------------------------------------------------------------------

def handle_plan(issue: dict) -> int:
    """Drive a :refined or :planning issue through the plan-select pipeline.

    The dispatcher supplies an issue whose state is either
    :class:`IssueState.REFINED` (fresh entry — we apply
    ``refined_to_planning`` first) or :class:`IssueState.PLANNING`
    (resume — we skip the entry transition). On success the issue is
    transitioned to :class:`IssueState.PLANNED`; on pipeline/edit
    failure we divert to :human-needed via ``planning_to_human`` so the
    issue does not stay stuck mid-planning.

    ``handle_plan_gate`` performs the subsequent confidence-gated
    auto-advance to ``PLAN_APPROVED``.
    """
    t0 = time.monotonic()

    issue_number = issue["number"]
    title = issue["title"]
    label_names = [l["name"] for l in issue.get("labels", [])]  # noqa: E741
    state = get_issue_state(label_names)

    print(f"[cai plan] picked #{issue_number}: {title}", flush=True)

    # 1. Entry transition :refined → :planning (only on fresh entry).
    if state == IssueState.REFINED:
        apply_transition(
            issue_number, "refined_to_planning",
            current_labels=label_names,
            log_prefix="cai plan",
        )
    elif state == IssueState.PLANNING:
        print(
            f"[cai plan] resuming #{issue_number} already at :planning",
            flush=True,
        )
    else:
        print(
            f"[cai plan] #{issue_number} unexpected state {state!r} "
            f"— aborting to prevent label corruption",
            file=sys.stderr, flush=True,
        )
        log_run("plan", repo=REPO, issue=issue_number,
                result="unexpected_state", exit=1)
        return 1

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
            apply_transition(
                issue_number, "planning_to_human",
                current_labels=[LABEL_PLANNING],
                log_prefix="cai plan",
            )
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
        pipeline_result = _run_plan_select_pipeline(
            issue, work_dir, attempt_history_block,
        )
        if pipeline_result is None:
            print(f"[cai plan] plan pipeline failed for #{issue_number}",
                  file=sys.stderr)
            apply_transition(
                issue_number, "planning_to_human",
                current_labels=[LABEL_PLANNING],
                log_prefix="cai plan",
            )
            dur = f"{int(time.monotonic() - t0)}s"
            log_run("plan", repo=REPO, issue=issue_number,
                    duration=dur, result="pipeline_failed", exit=1)
            return 1
        selected_plan, plan_confidence = pipeline_result

        # 5. Store plan in issue body (strip any old plan block first).
        current_body = _strip_stored_plan_block(issue.get("body", "") or "")
        # Also strip any stale pending marker left from a prior run — the
        # upcoming confidence gate (handle_plan_gate) will re-add one if
        # it diverts.
        current_body = strip_pending_marker(current_body)
        conf_name = plan_confidence.name if plan_confidence else "MISSING"
        plan_block = (
            "<!-- cai-plan-start -->\n"
            "## Selected Implementation Plan\n\n"
            f"{selected_plan}\n"
            f"Confidence: {conf_name}\n"
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
            apply_transition(
                issue_number, "planning_to_human",
                current_labels=[LABEL_PLANNING],
                log_prefix="cai plan",
            )
            dur = f"{int(time.monotonic() - t0)}s"
            log_run("plan", repo=REPO, issue=issue_number,
                    duration=dur, result="edit_failed", exit=1)
            return 1

        # Stash the confidence on the issue dict so the gate handler
        # (run as a separate dispatcher step) can read it. This is
        # belt-and-braces — the gate also reparses from the body if we
        # ever split the two calls across processes.
        issue["_cai_plan_confidence"] = plan_confidence

        # 6. Transition labels: :planning → :planned (waypoint).
        ok = apply_transition(
            issue_number, "planning_to_planned",
            current_labels=[LABEL_PLANNING],
            log_prefix="cai plan",
        )
        if not ok:
            dur = f"{int(time.monotonic() - t0)}s"
            log_run("plan", repo=REPO, issue=issue_number,
                    duration=dur, result="label_update_failed", exit=1)
            return 1

        dur = f"{int(time.monotonic() - t0)}s"
        conf_name = plan_confidence.name if plan_confidence else "MISSING"
        print(
            f"[cai plan] #{issue_number} planned :planning → :planned in {dur} "
            f"(confidence={conf_name}); running confidence gate inline",
            flush=True,
        )
        # Run the confidence gate inline so it is atomic with planning.
        # (The dispatcher also has PLANNED → handle_plan_gate as a safety net
        # for issues already stuck at :planned.)
        return handle_plan_gate(issue)

    finally:
        if work_dir.exists():
            shutil.rmtree(work_dir, ignore_errors=True)


def handle_plan_gate(issue: dict) -> int:
    """Confidence-gated auto-advance :planned → :plan-approved.

    Expects an issue at :class:`IssueState.PLANNED` with a stored plan
    block and a confidence marker (either stashed under
    ``_cai_plan_confidence`` by :func:`handle_plan` within the same
    process, or re-parsed from the issue body). HIGH-confidence plans
    auto-promote via ``planned_to_plan_approved``; anything below
    diverts to :human-needed (`planned_to_human`) with a pending marker
    so an admin can review.
    """
    from cai_lib.fsm import parse_confidence

    t0 = time.monotonic()
    issue_number = issue["number"]

    # Recover the confidence marker. Prefer the in-process stash from
    # handle_plan; otherwise parse from the stored plan block in the
    # issue body (for dispatchers that run the two handlers across
    # separate invocations).
    plan_confidence = issue.get("_cai_plan_confidence")
    if plan_confidence is None:
        body = issue.get("body", "") or ""
        plan_confidence = parse_confidence(body)

    # Apply the gate. HIGH → :plan-approved; below HIGH (or MISSING) →
    # :human-needed via the configured divert target.
    ok, diverted = apply_transition_with_confidence(
        issue_number, "planned_to_plan_approved", plan_confidence,
        current_labels=[LABEL_PLANNED],
        log_prefix="cai plan",
    )
    if not ok:
        # Transition or divert refused (e.g. state drift, label-edit failure).
        # Returning 0 here would leave the issue at :planned and cause the
        # dispatcher to re-pick the same target every cycle (#657). Report
        # failure so the cycle's worst_rc reflects the stall.
        dur = f"{int(time.monotonic() - t0)}s"
        conf_name = plan_confidence.name if plan_confidence else "MISSING"
        print(
            f"[cai plan] #{issue_number} gate refused — state did not advance",
            file=sys.stderr,
            flush=True,
        )
        log_run("plan", repo=REPO, issue=issue_number,
                duration=dur, result="gate_refused",
                confidence=conf_name, diverted=int(diverted),
                exit=1)
        return 1
    if diverted:
        # Append a pending marker so cai-unblock knows what we were
        # trying to do when the admin comments. Re-read the body so the
        # marker lands on the freshest content.
        try:
            fresh = _gh_json([
                "issue", "view", str(issue_number),
                "--repo", REPO,
                "--json", "body",
            ]) or {}
            current_body = fresh.get("body", "") or ""
        except Exception:  # pragma: no cover — defensive
            current_body = issue.get("body", "") or ""
        marker = render_pending_marker(
            transition_name="planned_to_plan_approved",
            from_state=IssueState.PLANNED,
            intended_state=IssueState.PLAN_APPROVED,
            confidence=plan_confidence,
        )
        marker_body = f"{current_body}\n\n{marker}\n"
        _run(
            ["gh", "issue", "edit", str(issue_number),
             "--repo", REPO, "--body", marker_body],
            capture_output=True,
        )

    dur = f"{int(time.monotonic() - t0)}s"
    conf_name = plan_confidence.name if plan_confidence else "MISSING"
    final_state = "human-needed" if diverted else "plan-approved"
    print(
        f"[cai plan] #{issue_number} gate → :{final_state} in {dur} "
        f"(confidence={conf_name})",
        flush=True,
    )
    log_run("plan", repo=REPO, issue=issue_number,
            duration=dur, result="gate_ok",
            confidence=conf_name, diverted=int(diverted),
            exit=0)
    return 0
