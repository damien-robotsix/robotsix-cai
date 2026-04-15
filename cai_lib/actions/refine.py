"""cai_lib.actions.refine — handler for IssueState.REFINING.

Invoked by the FSM dispatcher after it has fetched an open issue and
verified its state is ``IssueState.REFINING``. Runs the ``cai-refine``
agent, parses its output, and fires the appropriate transition
(``refining_to_refined``, ``refining_to_exploration``, or a
decomposition path that labels the parent ``:parent``).
"""
from __future__ import annotations

import re
import subprocess
import sys
import time

from cai_lib.config import (
    REPO,
    LABEL_RAISED,
    LABEL_REFINING,
    LABEL_PARENT,
)
from cai_lib.fsm import apply_transition
from cai_lib.github import _gh_json, _set_labels, _build_issue_block
from cai_lib.subprocess_utils import _run, _run_claude_p
from cai_lib.logging_utils import log_run
from cai_lib.cmd_helpers import _strip_stored_plan_block
from cai_lib.cmd_implement import _parse_decomposition


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


def _find_sub_issue(parent_number: int, step: int) -> "int | None":
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

    Each sub-issue gets:
    - A title formatted as `[#{parent_number} Step {step}/{total}] {title}`
      (e.g. `[#123 Step 1/3] Add schema migration`)
    - HTML-comment markers for parent and step number,
      enabling the ordering gate in ``_select_fix_target``
    - A body with a back-reference to the parent issue

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
        title = f"[#{parent_number} Step {s['step']}/{total}] {s['title']}"
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


def handle_refine(issue: dict) -> int:
    """Invoke the cai-refine agent on *issue* (already at :refining)."""
    t0 = time.monotonic()

    issue_number = issue["number"]
    title = issue["title"]
    print(f"[cai refine] targeting #{issue_number}: {title}", flush=True)

    # Move :raised → :refining before the agent runs so observers see
    # the transient working state (useful for audits + the unified
    # driver). :refining issues picked up from the second pool are
    # already in the working state.
    issue_label_names_initial = [l["name"] for l in issue.get("labels", [])]  # noqa: E741
    if LABEL_RAISED in issue_label_names_initial:
        apply_transition(
            issue_number, "raise_to_refining",
            current_labels=issue_label_names_initial,
            log_prefix="cai refine",
        )

    # Build user message. Every invocation is treated as a fresh pass —
    # the agent may rewrite the body to incorporate exploration findings
    # and re-decide NextStep.
    user_message = _build_issue_block(issue)
    result = _run_claude_p(
        ["claude", "-p", "--agent", "cai-refine",
         "--dangerously-skip-permissions"],
        category="refine",
        agent="cai-refine",
        input=user_message,
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
        apply_transition(
            issue_number, "refining_to_refined",
            current_labels=[LABEL_REFINING],
            log_prefix="cai refine",
        )
        dur = f"{int(time.monotonic() - t0)}s"
        log_run("refine", repo=REPO, issue=issue_number,
                duration=dur, result="already_structured", exit=0)
        return 0

    # Check for multi-step decomposition. Parent issues take on
    # :parent and drop out of the normal FSM; sub-issues become the
    # new units of work at :raised.
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
                remove=[LABEL_REFINING],
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

    # Update the issue body.
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
        apply_transition(
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

    apply_transition(
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
