"""cai_lib.actions.refine — handler for IssueState.REFINING.

Invoked by the FSM dispatcher after it has fetched an open issue and
verified its state is ``IssueState.REFINING``. Runs the ``cai-refine``
agent, parses its output, and fires the appropriate transition
(``refining_to_refined``, ``refining_to_exploration``, or a
decomposition path that labels the parent ``:parent``).
"""
from __future__ import annotations

import re
import sys
import time

from cai_lib.config import (
    REPO,
    LABEL_RAISED,
    LABEL_REFINING,
)
from cai_lib.fsm import fire_trigger
from cai_lib.github import _build_issue_block
from cai_lib.subagent import _run_claude_p
from cai_lib.subprocess_utils import _run
from cai_lib.logging_utils import log_run
from cai_lib.cmd_helpers import _strip_stored_plan_block
from cai_lib.issues import create_issue, get_parent_issue, link_sub_issue, list_sub_issues


_REFINE_NEXT_STEP_RE = re.compile(
    r"^\s*NextStep\s*[:=]\s*(PLAN|EXPLORE)\s*$",
    re.IGNORECASE | re.MULTILINE,
)


def _parse_refine_next_step(text: str) -> "str | None":
    """Extract ``NextStep: PLAN | EXPLORE`` from cai-refine output.

    Returns ``"PLAN"``, ``"EXPLORE"``, or ``None`` when missing. A missing
    decision is treated as PLAN by the caller — staying on :refined is
    the safe default that preserves today's behaviour.
    """
    if not text:
        return None
    m = _REFINE_NEXT_STEP_RE.search(text)
    if not m:
        return None
    return m.group(1).upper()


def _issue_depth(issue_number: int) -> int:
    """Compute decomposition depth by walking the native GitHub sub-issue parent chain.

    Depth 0 means the issue has no parent (top-level).
    Depth N means the issue's parent has depth N-1.
    Treats any API failure as "no parent" so the loop terminates safely.
    """
    depth = 0
    current = issue_number
    while True:
        parent = get_parent_issue(current)
        if parent is None:
            return depth
        depth += 1
        current = parent["number"]


def _find_sub_issue(parent_number: int, step: int) -> "int | None":
    """Return the issue number of an existing sub-issue for *parent_number*
    / *step* (open or closed), or None if none exists.

    Uses GitHub's native sub-issues API and matches by step number
    in the title.
    """
    subs = list_sub_issues(parent_number)
    pattern = re.compile(rf"\[#{parent_number}\s+Step\s+{step}/\d+\]")
    issues = [s for s in subs if pattern.search(s.get("title", ""))]
    if not issues:
        return None
    # Return the lowest (earliest-created) matching number for stability.
    return min(int(i["number"]) for i in issues)


def _create_sub_issues(
    steps: list[dict], parent_number: int, parent_title: str,
) -> list[int]:
    """Create GitHub sub-issues for a multi-step decomposition.

    Each sub-issue gets:
    - A title formatted as `[#{parent_number} Step {step}/{total}] {title}`
      (e.g. `[#123 Step 1/3] Add schema migration`)
    - A body with a back-reference to the parent issue
    - A native sub-issue link to the parent via the GitHub sub-issues API

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
            f"{s['body']}\n\n"
            f"---\n"
            f"_Sub-issue of #{parent_number} ({parent_title}). "
            f"Step {s['step']} of {total}._\n"
        )
        title = f"[#{parent_number} Step {s['step']}/{total}] {s['title']}"
        labels = ["auto-improve", LABEL_RAISED]
        # Use create_issue() (REST API) instead of gh issue create so we
        # get back the internal `id` needed for link_sub_issue().
        meta = create_issue(title, body, labels)
        if meta:
            num = meta.get("number", 0)
            url = meta.get("html_url", "")
            if num:
                created.append(num)
                # Establish native GitHub sub-issue relationship so
                # all_sub_issues_closed() in the verify sweep can detect
                # parent completion without falling back to checklist regex.
                link_sub_issue(parent_number, meta["id"])
            print(f"[cai refine] created sub-issue: {url}", flush=True)
        else:
            print(
                f"[cai refine] failed to create sub-issue "
                f"'Step {s['step']}'",
                file=sys.stderr,
            )
    return created


def handle_refine(issue: dict) -> int:
    """Invoke the cai-refine agent on *issue* (already at :refining)."""
    t0 = time.monotonic()

    issue_number = issue["number"]
    title = issue["title"]
    print(f"[cai refine] targeting #{issue_number}: {title}", flush=True)

    # :raised → :refining entry is now fired by ``drive_issue`` before this
    # handler runs (see ``cai_lib/dispatcher.py``). :refining issues picked
    # up from the resume pool are already in the working state.

    # Build user message. Every invocation is treated as a fresh pass —
    # the agent may rewrite the body to incorporate exploration findings
    # and re-decide NextStep.
    user_message = _build_issue_block(issue)
    # Decomposition depth awareness is handled by cai-split (the
    # downstream scope evaluator). Refine now always refines a
    # single unit of work regardless of depth.
    result = _run_claude_p(
        ["claude", "-p", "--agent", "cai-refine",
         "--dangerously-skip-permissions"],
        category="refine",
        agent="cai-refine",
        input=user_message,
        target_kind="issue",
        target_number=issue_number,
    )
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

    # Check for early-exit (already structured).
    if "## No Refinement Needed" in stdout:
        print(
            f"[cai refine] #{issue_number} already structured; "
            f"advancing :refining → :refined",
            flush=True,
        )
        fire_trigger(
            issue_number, "refining_to_refined",
            current_labels=[LABEL_REFINING],
            log_prefix="cai refine",
        )
        dur = f"{int(time.monotonic() - t0)}s"
        log_run("refine", repo=REPO, issue=issue_number,
                duration=dur, result="already_structured", exit=0)
        return 0

    # Scope decomposition (Multi-Step Decomposition) is now the
    # responsibility of the downstream cai-split agent. If refine
    # emits a decomposition block anyway we silently ignore it here
    # — split re-evaluates from the refined body on the next cycle.

    # Parse the refined issue block.
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

    # Build the new issue body: refined content + original text quoted.
    next_step = _parse_refine_next_step(stdout)
    original_body = _strip_stored_plan_block(issue.get("body") or "(no body)")
    quoted_original = "\n".join(f"> {line}" for line in original_body.splitlines())
    new_body = (
        f"{refined_body}\n\n"
        f"---\n\n"
        f"> **Original issue text:**\n>\n"
        f"{quoted_original}\n"
    )

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

    # Transition out of :refining. NextStep: EXPLORE routes the
    # issue to :needs-exploration; anything else advances to :refined
    # for cmd_plan to pick up.
    dur = f"{int(time.monotonic() - t0)}s"
    if next_step == "EXPLORE":
        fire_trigger(
            issue_number, "refining_to_exploration",
            current_labels=[LABEL_REFINING],
            log_prefix="cai refine",
        )
        print(
            f"[cai refine] #{issue_number} refined and routed to "
            f":needs-exploration in {dur}",
            flush=True,
        )
        log_run("refine", repo=REPO, issue=issue_number,
                duration=dur, result="refined_explore", exit=0)
        return 0

    fire_trigger(
        issue_number, "refining_to_refined",
        current_labels=[LABEL_REFINING],
        log_prefix="cai refine",
    )

    print(
        f"[cai refine] #{issue_number} refined and advanced :refining → :refined "
        f"in {dur}",
        flush=True,
    )
    log_run("refine", repo=REPO, issue=issue_number,
            duration=dur, result="refined", exit=0)
    return 0
