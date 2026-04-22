"""cai_lib.actions.split — handler for IssueState.REFINED / SPLITTING.

Invoked by the FSM dispatcher after ``cai-refine`` has written a
``## Refined Issue`` block and advanced the issue to ``:refined``.
Runs the ``cai-split`` agent to decide whether the refined scope
fits in a single PR (atomic) or needs to be decomposed into
ordered sub-issues.

Three possible outcomes per run:

- **Atomic + HIGH confidence** → fire ``splitting_to_planning`` so
  ``cai-plan`` picks the issue up next cycle.
- **Decompose + HIGH confidence** → create native GitHub sub-issues
  for each step, label the parent ``auto-improve:parent``, and drop
  the parent out of the normal drive path.
- **Anything else** (LOW confidence, missing confidence line,
  malformed decomposition, already at max depth, …) → divert to
  ``:human-needed`` via ``splitting_to_human`` with a reasoned
  comment so the admin can decide.

State-machine entry is idempotent across resumes: a fresh entry at
``IssueState.REFINED`` fires ``refined_to_splitting`` first; a
resume at ``IssueState.SPLITTING`` skips the entry fire and
re-invokes the agent.
"""
from __future__ import annotations

import sys
import time

from cai_lib.config import (
    REPO,
    LABEL_REFINED,
    LABEL_SPLITTING,
    LABEL_PARENT,
    MAX_DECOMPOSITION_DEPTH,
)
from cai_lib.fsm import (
    fire_trigger,
    get_issue_state,
    IssueState,
)
from cai_lib.fsm_confidence import Confidence, parse_confidence
from cai_lib.github import _build_issue_block, _set_labels
from cai_lib.subprocess_utils import _run_claude_p
from cai_lib.logging_utils import log_run
from cai_lib.cmd_implement import _parse_decomposition
from cai_lib.actions.refine import _create_sub_issues, _issue_depth


_ATOMIC_MARKER = "VERDICT: ATOMIC"
_UNCLEAR_MARKER = "VERDICT: UNCLEAR"
_DECOMPOSITION_MARKER = "## Multi-Step Decomposition"


def _extract_verdict_block(stdout: str, marker: str) -> str:
    """Return the slice starting at *marker* (trimmed), or empty string."""
    pos = stdout.find(marker)
    if pos == -1:
        return ""
    return stdout[pos:].strip()


def handle_split(issue: dict) -> int:
    """Evaluate scope of a refined issue via cai-split.

    Entry states:
    - :class:`IssueState.REFINED` (fresh entry — apply
      ``refined_to_splitting`` first).
    - :class:`IssueState.SPLITTING` (resume after crash or partial
      run — skip the entry fire).

    Returns the exit code of the ``cai-split`` invocation, or 0 on
    any clean routing decision (atomic, decomposed, or human-needed
    divert).
    """
    t0 = time.monotonic()

    issue_number = issue["number"]
    title = issue["title"]
    label_names = [l["name"] for l in issue.get("labels", [])]  # noqa: E741
    state = get_issue_state(label_names)

    print(f"[cai split] targeting #{issue_number}: {title}", flush=True)

    # 1. :refined → :splitting entry is now fired by ``drive_issue`` before
    # this handler runs (see ``cai_lib/dispatcher.py``). By the time we get
    # here the issue is always at :splitting; any other state is a label
    # corruption we refuse to process.
    if state != IssueState.SPLITTING:
        print(
            f"[cai split] #{issue_number} unexpected state {state!r} "
            f"— aborting to prevent label corruption",
            file=sys.stderr, flush=True,
        )
        log_run("split", repo=REPO, issue=issue_number,
                result="unexpected_state", exit=1)
        return 1

    # 2. Build the agent input and invoke cai-split.
    current_depth = _issue_depth(issue_number)
    user_message = _build_issue_block(issue)
    if current_depth >= MAX_DECOMPOSITION_DEPTH:
        user_message += (
            f"\n\nIMPORTANT: This issue is at decomposition depth "
            f"{current_depth} (max {MAX_DECOMPOSITION_DEPTH}). Do NOT "
            f"emit a `## Multi-Step Decomposition` block. Evaluate "
            f"only ATOMIC vs. UNCLEAR for this run."
        )

    result = _run_claude_p(
        ["claude", "-p", "--agent", "cai-split",
         "--dangerously-skip-permissions"],
        category="split",
        agent="cai-split",
        input=user_message,
    )
    print(result.stdout, flush=True)

    if result.returncode != 0:
        print(
            f"[cai split] claude -p failed (exit {result.returncode}):\n"
            f"{result.stderr}",
            flush=True,
        )
        dur = f"{int(time.monotonic() - t0)}s"
        log_run("split", repo=REPO, issue=issue_number,
                duration=dur, result="agent_failed", exit=result.returncode)
        return result.returncode

    stdout = result.stdout
    confidence = parse_confidence(stdout)

    # 3. Route on the verdict. Decomposition takes priority over ATOMIC
    # because a stray "VERDICT: ATOMIC" token inside a Step body must
    # not beat an explicit decomposition block.
    has_decomposition = _DECOMPOSITION_MARKER in stdout
    has_atomic = _ATOMIC_MARKER in stdout
    has_unclear = _UNCLEAR_MARKER in stdout

    # 3a. Decomposition path.
    if has_decomposition:
        if current_depth >= MAX_DECOMPOSITION_DEPTH:
            divert_reason = (
                f"cai-split emitted a Multi-Step Decomposition but this "
                f"issue is already at depth {current_depth} "
                f"(max {MAX_DECOMPOSITION_DEPTH}). Decomposing further "
                f"would exceed the allowed depth — the admin should "
                f"either re-scope the parent or approve this as a "
                f"single unit of work."
            )
            fire_trigger(
                issue_number, "splitting_to_human",
                current_labels=[LABEL_SPLITTING],
                log_prefix="cai split",
                divert_reason=divert_reason,
            )
            dur = f"{int(time.monotonic() - t0)}s"
            log_run("split", repo=REPO, issue=issue_number,
                    duration=dur, result="decompose_over_depth", exit=0)
            return 0

        if confidence != Confidence.HIGH:
            divert_reason = (
                f"cai-split emitted a Multi-Step Decomposition with "
                f"confidence {confidence.name if confidence else 'MISSING'} "
                f"(HIGH required). Admin review needed before creating "
                f"sub-issues."
            )
            fire_trigger(
                issue_number, "splitting_to_human",
                current_labels=[LABEL_SPLITTING],
                log_prefix="cai split",
                divert_reason=divert_reason,
            )
            dur = f"{int(time.monotonic() - t0)}s"
            log_run("split", repo=REPO, issue=issue_number,
                    duration=dur, result="decompose_low_confidence",
                    confidence=confidence.name if confidence else "MISSING",
                    exit=0)
            return 0

        steps = _parse_decomposition(stdout)
        if not steps or len(steps) < 2:
            divert_reason = (
                f"cai-split emitted a Multi-Step Decomposition block "
                f"but parsing yielded {len(steps)} step(s) — a valid "
                f"decomposition requires at least 2 steps. The agent "
                f"output may be malformed."
            )
            fire_trigger(
                issue_number, "splitting_to_human",
                current_labels=[LABEL_SPLITTING],
                log_prefix="cai split",
                divert_reason=divert_reason,
            )
            dur = f"{int(time.monotonic() - t0)}s"
            log_run("split", repo=REPO, issue=issue_number,
                    duration=dur, result="decompose_malformed",
                    steps=len(steps), exit=0)
            return 0

        # HIGH + well-formed decomposition — create sub-issues and
        # transition the parent out of the drive path.
        print(
            f"[cai split] #{issue_number} decomposed into "
            f"{len(steps)} steps",
            flush=True,
        )
        sub_nums = _create_sub_issues(
            steps, issue_number, title,
        )
        _set_labels(
            issue_number,
            add=[LABEL_PARENT],
            remove=[LABEL_SPLITTING],
            log_prefix="cai split",
        )
        dur = f"{int(time.monotonic() - t0)}s"
        log_run(
            "split", repo=REPO, issue=issue_number,
            duration=dur, result="decomposed",
            sub_issues=len(sub_nums), steps=len(steps), exit=0,
        )
        return 0

    # 3b. Atomic path. Require HIGH confidence; anything else diverts.
    if has_atomic:
        if confidence != Confidence.HIGH:
            divert_reason = (
                f"cai-split returned ATOMIC with confidence "
                f"{confidence.name if confidence else 'MISSING'} "
                f"(HIGH required). Admin review needed to approve or "
                f"re-split at narrower scope."
            )
            fire_trigger(
                issue_number, "splitting_to_human",
                current_labels=[LABEL_SPLITTING],
                log_prefix="cai split",
                divert_reason=divert_reason,
            )
            dur = f"{int(time.monotonic() - t0)}s"
            log_run("split", repo=REPO, issue=issue_number,
                    duration=dur, result="atomic_low_confidence",
                    confidence=confidence.name if confidence else "MISSING",
                    exit=0)
            return 0

        fire_trigger(
            issue_number, "splitting_to_planning",
            current_labels=[LABEL_SPLITTING],
            log_prefix="cai split",
        )
        dur = f"{int(time.monotonic() - t0)}s"
        print(
            f"[cai split] #{issue_number} verdict ATOMIC, advancing "
            f":splitting → :planning in {dur}",
            flush=True,
        )
        log_run("split", repo=REPO, issue=issue_number,
                duration=dur, result="atomic", exit=0)
        return 0

    # 3c. Unclear verdict or missing marker — divert to human.
    if has_unclear:
        verdict_block = _extract_verdict_block(stdout, _UNCLEAR_MARKER)
        divert_reason = (
            "cai-split returned VERDICT: UNCLEAR — the agent was not "
            "confident enough to decide atomic vs. decompose. Admin "
            "input needed.\n\n"
            f"{verdict_block}"
        )
    else:
        divert_reason = (
            "cai-split output did not contain a recognised verdict "
            "marker (expected `## Multi-Step Decomposition`, "
            "`VERDICT: ATOMIC`, or `VERDICT: UNCLEAR`). Agent output "
            "appears malformed."
        )

    fire_trigger(
        issue_number, "splitting_to_human",
        current_labels=[LABEL_SPLITTING],
        log_prefix="cai split",
        divert_reason=divert_reason,
    )
    dur = f"{int(time.monotonic() - t0)}s"
    log_run("split", repo=REPO, issue=issue_number,
            duration=dur,
            result="unclear" if has_unclear else "no_marker",
            exit=0)
    return 0
