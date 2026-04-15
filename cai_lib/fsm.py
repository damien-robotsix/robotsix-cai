"""FSM data structures for the auto-improve lifecycle.

This module defines the explicit state machine that the auto-improve
pipeline follows. Transitions are data; drivers in ``cai.py`` apply
them through :func:`apply_transition` or
:func:`apply_transition_with_confidence`.
"""
from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Sequence

from cai_lib.config import (
    LABEL_RAISED, LABEL_REFINING, LABEL_REFINED, LABEL_PLANNING,
    LABEL_PLANNED, LABEL_PLAN_APPROVED,
    LABEL_IN_PROGRESS, LABEL_PR_OPEN, LABEL_MERGED, LABEL_SOLVED,
    LABEL_NEEDS_EXPLORATION, LABEL_HUMAN_NEEDED, LABEL_PR_HUMAN_NEEDED,
    LABEL_TRIAGING, LABEL_APPLYING, LABEL_APPLIED,
    LABEL_PR_REVIEWING_CODE, LABEL_PR_REVISION_PENDING,
    LABEL_PR_REVIEWING_DOCS, LABEL_PR_APPROVED, LABEL_PR_REBASING,
    LABEL_PR_CI_FAILING,
)


class Confidence(Enum):
    """Qualitative confidence level emitted by agents.

    Ordered so ``Confidence.LOW < Confidence.MEDIUM < Confidence.HIGH`` —
    use comparison operators to gate transitions rather than comparing
    raw ints.
    """
    LOW    = 1
    MEDIUM = 2
    HIGH   = 3

    def __lt__(self, other: "Confidence") -> bool:
        if not isinstance(other, Confidence):
            return NotImplemented
        return self.value < other.value

    def __le__(self, other: "Confidence") -> bool:
        if not isinstance(other, Confidence):
            return NotImplemented
        return self.value <= other.value

    def __gt__(self, other: "Confidence") -> bool:
        if not isinstance(other, Confidence):
            return NotImplemented
        return self.value > other.value

    def __ge__(self, other: "Confidence") -> bool:
        if not isinstance(other, Confidence):
            return NotImplemented
        return self.value >= other.value


_CONFIDENCE_RE = re.compile(
    r"^\s*Confidence\s*[:=]\s*(LOW|MEDIUM|HIGH)\s*$",
    re.IGNORECASE | re.MULTILINE,
)


def parse_confidence(text: str) -> Optional[Confidence]:
    """Extract ``Confidence: LOW|MEDIUM|HIGH`` from agent structured output.

    Returns the parsed level, or ``None`` when no well-formed line is
    present. Callers must treat ``None`` as "missing" and divert to
    HUMAN_NEEDED — never assume a default level.
    """
    if not text:
        return None
    m = _CONFIDENCE_RE.search(text)
    if not m:
        return None
    return Confidence[m.group(1).upper()]


_RESUME_RE = re.compile(
    r"^\s*ResumeTo\s*[:=]\s*([A-Z_]+)\s*$",
    re.MULTILINE,
)


def parse_resume_target(text: str) -> Optional[str]:
    """Extract ``ResumeTo: <STATE_NAME>`` from a cai-unblock agent reply.

    Returns the raw state name as written by the agent (uppercased per
    our structured-output convention) or ``None`` if the marker is
    missing. The caller decides whether the returned name maps to a
    real IssueState/PRState member.
    """
    if not text:
        return None
    m = _RESUME_RE.search(text)
    if not m:
        return None
    return m.group(1).upper()


class IssueState(str, Enum):
    RAISED            = LABEL_RAISED
    TRIAGING          = LABEL_TRIAGING     # cai-triage is actively running
    APPLYING          = LABEL_APPLYING     # cai-maintain is actively applying ops
    APPLIED           = LABEL_APPLIED      # ops applied; awaiting verification
    REFINING          = LABEL_REFINING     # cai-refine is actively running
    REFINED           = LABEL_REFINED      # refine done, awaiting plan pickup
    PLANNING          = LABEL_PLANNING     # cai-plan is actively running
    PLANNED           = LABEL_PLANNED      # plan stored, awaiting approval
    PLAN_APPROVED     = LABEL_PLAN_APPROVED
    IN_PROGRESS       = LABEL_IN_PROGRESS
    PR                = LABEL_PR_OPEN      # currently in the PR submachine
    MERGED            = LABEL_MERGED
    SOLVED            = LABEL_SOLVED
    NEEDS_EXPLORATION = LABEL_NEEDS_EXPLORATION
    HUMAN_NEEDED      = LABEL_HUMAN_NEEDED


class PRState(str, Enum):
    """Persistent PR pipeline state, tracked via GitHub labels.

    Unlike the prior model (which derived state from GitHub's native
    ``reviewDecision`` and ran CI / docs review as out-of-band loops),
    every state here is first-class: one label per state, one action
    per state. See ``PR_TRANSITIONS`` for allowed moves.
    """
    OPEN              = "pr:open"                     # no label yet
    REVIEWING_CODE    = LABEL_PR_REVIEWING_CODE       # cai-review-pr runs
    REVISION_PENDING  = LABEL_PR_REVISION_PENDING     # findings; awaiting revise push
    REVIEWING_DOCS    = LABEL_PR_REVIEWING_DOCS       # cai-review-docs runs
    APPROVED          = LABEL_PR_APPROVED             # docs clean; merge handler picks it up
    REBASING          = LABEL_PR_REBASING             # mergeable=CONFLICTING; cai-rebase runs
    CI_FAILING        = LABEL_PR_CI_FAILING           # cai-fix-ci runs
    MERGED            = "pr:merged"                   # derived from gh merged flag
    PR_HUMAN_NEEDED   = LABEL_PR_HUMAN_NEEDED         # parked for admin comment


@dataclass
class Transition:
    name: str
    from_state: IssueState | PRState
    to_state:   IssueState | PRState
    labels_add:    list[str] = field(default_factory=list)
    labels_remove: list[str] = field(default_factory=list)
    # Minimum confidence the emitting agent must report for the
    # transition to fire. Default HIGH means only fully-confident moves
    # auto-advance; anything lower diverts to ``human_label_if_below``.
    # Set to None to indicate that gating is handled at the application
    # level (e.g. in cmd_triage) rather than by the FSM infrastructure.
    min_confidence: Optional[Confidence] = Confidence.HIGH
    human_label_if_below: str = LABEL_HUMAN_NEEDED

    def accepts(self, confidence: Optional[Confidence]) -> bool:
        """True if *confidence* meets or exceeds this transition's threshold.

        If ``min_confidence`` is ``None`` the transition has no FSM-level
        gate — the caller is responsible for confidence checks. Returns
        ``True`` unconditionally in that case.

        ``None`` *confidence* always fails when ``min_confidence`` is set.
        """
        if self.min_confidence is None:
            return True  # no FSM-level gate; caller handles confidence
        if confidence is None:
            return False
        return confidence >= self.min_confidence


ISSUE_TRANSITIONS: list[Transition] = [
    # RAISED: either pick up for refinement, or punt to human.
    Transition("raise_to_refining",          IssueState.RAISED,            IssueState.REFINING,
               labels_remove=[LABEL_RAISED],            labels_add=[LABEL_REFINING]),
    Transition("raise_to_human",             IssueState.RAISED,            IssueState.HUMAN_NEEDED,
               labels_remove=[LABEL_RAISED],            labels_add=[LABEL_HUMAN_NEEDED]),

    # TRIAGING is transient — cai-triage is classifying the issue.
    # raise_to_triaging is the normal entry; raise_to_refining still
    # exists as a bypass (direct/manual refinement, cai-refine --issue N).
    Transition("raise_to_triaging",        IssueState.RAISED,    IssueState.TRIAGING,
               labels_remove=[LABEL_RAISED],   labels_add=[LABEL_TRIAGING]),
    Transition("triaging_to_refining",     IssueState.TRIAGING,  IssueState.REFINING,
               labels_remove=[LABEL_TRIAGING], labels_add=[LABEL_REFINING]),
    Transition("triaging_to_human",        IssueState.TRIAGING,  IssueState.HUMAN_NEEDED,
               labels_remove=[LABEL_TRIAGING], labels_add=[LABEL_HUMAN_NEEDED]),

    # TRIAGING skip-ahead paths — gating is at the application level in
    # cmd_triage; these transitions carry no FSM-level confidence gate.
    Transition("triaging_to_plan_approved", IssueState.TRIAGING,      IssueState.PLAN_APPROVED,
               labels_remove=[LABEL_TRIAGING],   labels_add=[LABEL_PLAN_APPROVED],
               min_confidence=None),
    Transition("triaging_to_applying",      IssueState.TRIAGING,      IssueState.APPLYING,
               labels_remove=[LABEL_TRIAGING],   labels_add=[LABEL_APPLYING],
               min_confidence=None),

    # APPLYING is transient — cmd_maintain (Step 3) drains it.
    Transition("applying_to_applied",       IssueState.APPLYING,      IssueState.APPLIED,
               labels_remove=[LABEL_APPLYING],   labels_add=[LABEL_APPLIED],
               min_confidence=Confidence.HIGH),
    Transition("applying_to_human",         IssueState.APPLYING,      IssueState.HUMAN_NEEDED,
               labels_remove=[LABEL_APPLYING],   labels_add=[LABEL_HUMAN_NEEDED]),

    # APPLIED → SOLVED is the final maintenance completion step.
    Transition("applied_to_solved",         IssueState.APPLIED,       IssueState.SOLVED,
               labels_remove=[LABEL_APPLIED],    labels_add=[LABEL_SOLVED]),

    # REFINING is transient — cai-refine is running. The confidence gate
    # on refining_to_refined diverts to HUMAN_NEEDED when refinement
    # isn't high-confidence. refining_to_exploration is the agent's
    # explicit "need more info" branch; refining_to_human is the
    # explicit "I can't do this" branch.
    Transition("refining_to_refined",        IssueState.REFINING,          IssueState.REFINED,
               labels_remove=[LABEL_REFINING],          labels_add=[LABEL_REFINED]),
    Transition("refining_to_exploration",    IssueState.REFINING,          IssueState.NEEDS_EXPLORATION,
               labels_remove=[LABEL_REFINING],          labels_add=[LABEL_NEEDS_EXPLORATION]),
    Transition("refining_to_human",          IssueState.REFINING,          IssueState.HUMAN_NEEDED,
               labels_remove=[LABEL_REFINING],          labels_add=[LABEL_HUMAN_NEEDED]),

    # Exploration loops back to refining (not refined) so the refine
    # agent re-evaluates with the new findings before deciding plan.
    Transition("exploration_to_refining",    IssueState.NEEDS_EXPLORATION, IssueState.REFINING,
               labels_remove=[LABEL_NEEDS_EXPLORATION], labels_add=[LABEL_REFINING]),

    # REFINED → PLANNING is the auto-advance: whoever drives the
    # pipeline (cmd_plan / unified driver) picks up a :refined issue
    # and immediately moves it to :planning when it starts the plan
    # agent. There is no human gate here.
    Transition("refined_to_planning",        IssueState.REFINED,           IssueState.PLANNING,
               labels_remove=[LABEL_REFINED],           labels_add=[LABEL_PLANNING]),

    # PLANNING is transient — cai-plan is running. Same confidence gate
    # pattern as refining.
    Transition("planning_to_planned",        IssueState.PLANNING,          IssueState.PLANNED,
               labels_remove=[LABEL_PLANNING],          labels_add=[LABEL_PLANNED]),
    Transition("planning_to_human",          IssueState.PLANNING,          IssueState.HUMAN_NEEDED,
               labels_remove=[LABEL_PLANNING],          labels_add=[LABEL_HUMAN_NEEDED]),

    # PLANNED → PLAN_APPROVED auto-advances on high confidence; below
    # that, divert to human for explicit admin approval. A dedicated
    # planned_to_human transition covers the explicit "needs human" case.
    Transition("planned_to_plan_approved",   IssueState.PLANNED,           IssueState.PLAN_APPROVED,
               labels_remove=[LABEL_PLANNED],           labels_add=[LABEL_PLAN_APPROVED]),
    Transition("planned_to_human",           IssueState.PLANNED,           IssueState.HUMAN_NEEDED,
               labels_remove=[LABEL_PLANNED],           labels_add=[LABEL_HUMAN_NEEDED]),

    Transition("approved_to_in_progress",    IssueState.PLAN_APPROVED,     IssueState.IN_PROGRESS,
               labels_remove=[LABEL_PLAN_APPROVED],     labels_add=[LABEL_IN_PROGRESS]),
    Transition("in_progress_to_pr",          IssueState.IN_PROGRESS,       IssueState.PR,
               labels_remove=[LABEL_IN_PROGRESS],       labels_add=[LABEL_PR_OPEN]),
    Transition("pr_to_merged",               IssueState.PR,                IssueState.MERGED,
               labels_remove=[LABEL_PR_OPEN],             labels_add=[LABEL_MERGED]),
    Transition("merged_to_solved",           IssueState.MERGED,            IssueState.SOLVED,
               labels_remove=[LABEL_MERGED],            labels_add=[LABEL_SOLVED]),

    Transition("human_to_raised",            IssueState.HUMAN_NEEDED,      IssueState.RAISED,
               labels_remove=[LABEL_HUMAN_NEEDED],      labels_add=[LABEL_RAISED]),
    # Admin-comment-driven re-entries out of HUMAN_NEEDED. Fired by
    # cmd_unblock after a Haiku agent classifies the admin's reply.
    # Resume into REFINING (not REFINED) so the refine agent re-runs
    # with the admin's input in context — REFINED is an auto-advance
    # waypoint, not a sensible re-entry point.
    Transition("human_to_refining",          IssueState.HUMAN_NEEDED,      IssueState.REFINING,
               labels_remove=[LABEL_HUMAN_NEEDED],      labels_add=[LABEL_REFINING]),
    # Admin greenlights the already-stored plan — jump past the
    # planned→approved gate.
    Transition("human_to_plan_approved",     IssueState.HUMAN_NEEDED,      IssueState.PLAN_APPROVED,
               labels_remove=[LABEL_HUMAN_NEEDED],      labels_add=[LABEL_PLAN_APPROVED]),
    Transition("human_to_exploration",       IssueState.HUMAN_NEEDED,      IssueState.NEEDS_EXPLORATION,
               labels_remove=[LABEL_HUMAN_NEEDED],      labels_add=[LABEL_NEEDS_EXPLORATION]),
    Transition("human_to_solved",            IssueState.HUMAN_NEEDED,      IssueState.SOLVED,
               labels_remove=[LABEL_HUMAN_NEEDED],      labels_add=[LABEL_SOLVED]),
]


PR_TRANSITIONS: list[Transition] = [
    # Entry: brand-new PR → code review.
    Transition("open_to_reviewing_code",
               PRState.OPEN, PRState.REVIEWING_CODE,
               labels_add=[LABEL_PR_REVIEWING_CODE],
               human_label_if_below=LABEL_PR_HUMAN_NEEDED),

    # Code-review outcomes.
    Transition("reviewing_code_to_revision_pending",
               PRState.REVIEWING_CODE, PRState.REVISION_PENDING,
               labels_remove=[LABEL_PR_REVIEWING_CODE],
               labels_add=[LABEL_PR_REVISION_PENDING],
               human_label_if_below=LABEL_PR_HUMAN_NEEDED),
    # Gated: advance to docs review only at HIGH confidence.
    Transition("reviewing_code_to_reviewing_docs",
               PRState.REVIEWING_CODE, PRState.REVIEWING_DOCS,
               labels_remove=[LABEL_PR_REVIEWING_CODE],
               labels_add=[LABEL_PR_REVIEWING_DOCS],
               human_label_if_below=LABEL_PR_HUMAN_NEEDED),

    # After a revise push, the new SHA needs code review again.
    Transition("revision_pending_to_reviewing_code",
               PRState.REVISION_PENDING, PRState.REVIEWING_CODE,
               labels_remove=[LABEL_PR_REVISION_PENDING],
               labels_add=[LABEL_PR_REVIEWING_CODE],
               human_label_if_below=LABEL_PR_HUMAN_NEEDED),

    # Docs review may self-heal by pushing; a new SHA → back to code review.
    Transition("reviewing_docs_to_reviewing_code",
               PRState.REVIEWING_DOCS, PRState.REVIEWING_CODE,
               labels_remove=[LABEL_PR_REVIEWING_DOCS],
               labels_add=[LABEL_PR_REVIEWING_CODE],
               human_label_if_below=LABEL_PR_HUMAN_NEEDED),
    # Docs clean → approved. The merge handler picks up APPROVED as a
    # separate state so future pre-merge steps (release notes, tag
    # checks, …) can slot in without bloating the review handler.
    Transition("reviewing_docs_to_approved",
               PRState.REVIEWING_DOCS, PRState.APPROVED,
               labels_remove=[LABEL_PR_REVIEWING_DOCS],
               labels_add=[LABEL_PR_APPROVED],
               human_label_if_below=LABEL_PR_HUMAN_NEEDED),
    # Terminal gate: approved → merged (CI-green check is at merge time).
    Transition("approved_to_merged",
               PRState.APPROVED, PRState.MERGED,
               labels_remove=[LABEL_PR_APPROVED],
               human_label_if_below=LABEL_PR_HUMAN_NEEDED),
    # If new commits arrive while APPROVED, kick back to code review.
    Transition("approved_to_reviewing_code",
               PRState.APPROVED, PRState.REVIEWING_CODE,
               labels_remove=[LABEL_PR_APPROVED],
               labels_add=[LABEL_PR_REVIEWING_CODE],
               human_label_if_below=LABEL_PR_HUMAN_NEEDED),

    # CI orthogonal gate: any pre-merge state can dive into CI_FAILING
    # on red checks; once green, return to code review since the branch
    # has new commits that need re-review.
    Transition("reviewing_code_to_ci_failing",
               PRState.REVIEWING_CODE, PRState.CI_FAILING,
               labels_remove=[LABEL_PR_REVIEWING_CODE],
               labels_add=[LABEL_PR_CI_FAILING],
               human_label_if_below=LABEL_PR_HUMAN_NEEDED),
    Transition("revision_pending_to_ci_failing",
               PRState.REVISION_PENDING, PRState.CI_FAILING,
               labels_remove=[LABEL_PR_REVISION_PENDING],
               labels_add=[LABEL_PR_CI_FAILING],
               human_label_if_below=LABEL_PR_HUMAN_NEEDED),
    Transition("reviewing_docs_to_ci_failing",
               PRState.REVIEWING_DOCS, PRState.CI_FAILING,
               labels_remove=[LABEL_PR_REVIEWING_DOCS],
               labels_add=[LABEL_PR_CI_FAILING],
               human_label_if_below=LABEL_PR_HUMAN_NEEDED),
    Transition("approved_to_ci_failing",
               PRState.APPROVED, PRState.CI_FAILING,
               labels_remove=[LABEL_PR_APPROVED],
               labels_add=[LABEL_PR_CI_FAILING],
               human_label_if_below=LABEL_PR_HUMAN_NEEDED),
    Transition("ci_failing_to_reviewing_code",
               PRState.CI_FAILING, PRState.REVIEWING_CODE,
               labels_remove=[LABEL_PR_CI_FAILING],
               labels_add=[LABEL_PR_REVIEWING_CODE],
               human_label_if_below=LABEL_PR_HUMAN_NEEDED),

    # Rebase orthogonal gate: any pre-merge state can dive into REBASING
    # when the dispatcher detects mergeable=CONFLICTING. The handler
    # always exits to REVIEWING_CODE (success or failure) — the rebase
    # outcome is posted as a PR comment so the next reviewer sees what
    # happened and can either approve the rebased SHA, leave findings,
    # or escalate to human if conflicts were unresolvable.
    Transition("reviewing_code_to_rebasing",
               PRState.REVIEWING_CODE, PRState.REBASING,
               labels_remove=[LABEL_PR_REVIEWING_CODE],
               labels_add=[LABEL_PR_REBASING],
               human_label_if_below=LABEL_PR_HUMAN_NEEDED),
    Transition("revision_pending_to_rebasing",
               PRState.REVISION_PENDING, PRState.REBASING,
               labels_remove=[LABEL_PR_REVISION_PENDING],
               labels_add=[LABEL_PR_REBASING],
               human_label_if_below=LABEL_PR_HUMAN_NEEDED),
    Transition("reviewing_docs_to_rebasing",
               PRState.REVIEWING_DOCS, PRState.REBASING,
               labels_remove=[LABEL_PR_REVIEWING_DOCS],
               labels_add=[LABEL_PR_REBASING],
               human_label_if_below=LABEL_PR_HUMAN_NEEDED),
    Transition("approved_to_rebasing",
               PRState.APPROVED, PRState.REBASING,
               labels_remove=[LABEL_PR_APPROVED],
               labels_add=[LABEL_PR_REBASING],
               human_label_if_below=LABEL_PR_HUMAN_NEEDED),
    Transition("ci_failing_to_rebasing",
               PRState.CI_FAILING, PRState.REBASING,
               labels_remove=[LABEL_PR_CI_FAILING],
               labels_add=[LABEL_PR_REBASING],
               human_label_if_below=LABEL_PR_HUMAN_NEEDED),
    Transition("rebasing_to_reviewing_code",
               PRState.REBASING, PRState.REVIEWING_CODE,
               labels_remove=[LABEL_PR_REBASING],
               labels_add=[LABEL_PR_REVIEWING_CODE],
               human_label_if_below=LABEL_PR_HUMAN_NEEDED),

    # Human-needed divert + resume paths.
    Transition("reviewing_code_to_human",
               PRState.REVIEWING_CODE, PRState.PR_HUMAN_NEEDED,
               labels_remove=[LABEL_PR_REVIEWING_CODE],
               labels_add=[LABEL_PR_HUMAN_NEEDED],
               human_label_if_below=LABEL_PR_HUMAN_NEEDED),
    Transition("pr_human_to_reviewing_code",
               PRState.PR_HUMAN_NEEDED, PRState.REVIEWING_CODE,
               labels_remove=[LABEL_PR_HUMAN_NEEDED],
               labels_add=[LABEL_PR_REVIEWING_CODE],
               human_label_if_below=LABEL_PR_HUMAN_NEEDED),
    Transition("pr_human_to_revision_pending",
               PRState.PR_HUMAN_NEEDED, PRState.REVISION_PENDING,
               labels_remove=[LABEL_PR_HUMAN_NEEDED],
               labels_add=[LABEL_PR_REVISION_PENDING],
               human_label_if_below=LABEL_PR_HUMAN_NEEDED),
    Transition("pr_human_to_reviewing_docs",
               PRState.PR_HUMAN_NEEDED, PRState.REVIEWING_DOCS,
               labels_remove=[LABEL_PR_HUMAN_NEEDED],
               labels_add=[LABEL_PR_REVIEWING_DOCS],
               human_label_if_below=LABEL_PR_HUMAN_NEEDED),
    Transition("pr_human_to_approved",
               PRState.PR_HUMAN_NEEDED, PRState.APPROVED,
               labels_remove=[LABEL_PR_HUMAN_NEEDED],
               labels_add=[LABEL_PR_APPROVED],
               human_label_if_below=LABEL_PR_HUMAN_NEEDED),
    # NOTE: no pr_human_to_merged — PR_HUMAN_NEEDED must funnel back
    # through a reviewable state so a PR never bypasses review on its
    # way to MERGED.
]


def get_issue_state(labels: list[str]) -> Optional[IssueState]:
    """Return the first IssueState whose label value appears in *labels*."""
    label_set = set(labels)
    for state in IssueState:
        if state.value in label_set:
            return state
    return None


_PR_LABEL_STATES = [
    (LABEL_PR_HUMAN_NEEDED,     PRState.PR_HUMAN_NEEDED),
    (LABEL_PR_REBASING,         PRState.REBASING),
    (LABEL_PR_CI_FAILING,       PRState.CI_FAILING),
    (LABEL_PR_REVISION_PENDING, PRState.REVISION_PENDING),
    (LABEL_PR_APPROVED,         PRState.APPROVED),
    (LABEL_PR_REVIEWING_DOCS,   PRState.REVIEWING_DOCS),
    (LABEL_PR_REVIEWING_CODE,   PRState.REVIEWING_CODE),
]


def get_pr_state(pr: dict) -> PRState:
    """Derive the current PRState from a GitHub PR JSON dict.

    Precedence:
    1. Merged flag → ``MERGED`` (terminal).
    2. Pipeline labels (checked in ``_PR_LABEL_STATES`` order so
       human-needed and CI-failing outrank any stuck review label).
    3. No pipeline label → ``OPEN`` (brand-new PR; dispatcher applies
       ``open_to_reviewing_code``).

    CI-red-overrides-label is NOT baked in here — the dispatcher
    compares check status against the current state and explicitly
    applies a ``*_to_ci_failing`` transition. Keeping derivation pure
    lets tests stub PR dicts without checkrollup data.
    """
    if pr.get("mergedAt") or pr.get("state") == "MERGED":
        return PRState.MERGED
    labels_raw = pr.get("labels", [])
    label_set = {
        (lbl.get("name") if isinstance(lbl, dict) else lbl)
        for lbl in labels_raw
    }
    for label_value, state in _PR_LABEL_STATES:
        if label_value in label_set:
            return state
    return PRState.OPEN


_ALL_TRANSITIONS: list[Transition] = ISSUE_TRANSITIONS + PR_TRANSITIONS


def find_transition(name: str, transitions: Sequence[Transition] = _ALL_TRANSITIONS) -> Transition:
    """Return the Transition with the given *name*. Raises KeyError if unknown."""
    for t in transitions:
        if t.name == name:
            return t
    raise KeyError(f"unknown transition: {name!r}")


# ---------------------------------------------------------------------------
# Pending-marker helpers
#
# When a transition diverts to HUMAN_NEEDED because confidence was too low (or
# missing), we append a hidden marker to the issue body so the resume loop
# knows what the agent was trying to do. The marker survives edits because we
# key on the delimiter pair and replace in place.
# ---------------------------------------------------------------------------

_PENDING_MARKER_START = "<!-- cai-fsm-pending"
_PENDING_MARKER_END   = "-->"
_PENDING_MARKER_RE = re.compile(
    r"<!--\s*cai-fsm-pending\s+(.*?)\s*-->",
    re.DOTALL,
)


def render_pending_marker(
    *,
    transition_name: str,
    from_state: IssueState | PRState,
    intended_state: IssueState | PRState,
    confidence: Optional[Confidence],
) -> str:
    """Serialize a pending-transition marker for an issue body."""
    conf = confidence.name if confidence is not None else "MISSING"
    from_name = from_state.name if hasattr(from_state, "name") else str(from_state)
    intended_name = (
        intended_state.name if hasattr(intended_state, "name") else str(intended_state)
    )
    return (
        f"{_PENDING_MARKER_START} "
        f"transition={transition_name} from={from_name} "
        f"intended={intended_name} conf={conf} {_PENDING_MARKER_END}"
    )


def parse_pending_marker(body: str) -> Optional[dict]:
    """Extract the pending-transition marker from an issue *body*.

    Returns a dict with keys ``transition``, ``from``, ``intended``, ``conf``
    (all strings), or ``None`` if no marker is present.
    """
    if not body:
        return None
    m = _PENDING_MARKER_RE.search(body)
    if not m:
        return None
    fields: dict = {}
    for token in m.group(1).split():
        if "=" in token:
            k, v = token.split("=", 1)
            fields[k] = v
    if "transition" not in fields:
        return None
    return fields


def strip_pending_marker(body: str) -> str:
    """Remove any pending-transition marker from *body* (and trailing blank lines)."""
    if not body:
        return body
    new = _PENDING_MARKER_RE.sub("", body)
    # Collapse the blank lines the marker may have left behind.
    return re.sub(r"\n{3,}", "\n\n", new).rstrip() + ("\n" if body.endswith("\n") else "")


def apply_transition(
    issue_number: int,
    transition_name: str,
    *,
    current_labels: Optional[list[str]] = None,
    extra_remove: Sequence[str] = (),
    log_prefix: str = "cai",
    set_labels=None,
) -> bool:
    """Apply a named issue FSM transition via ``_set_labels``.

    When *current_labels* is provided, the current IssueState is derived and
    compared to ``transition.from_state``. A mismatch is refused (logs and
    returns False) so drift cannot silently compound.

    *extra_remove* is appended to the transition's own ``labels_remove`` —
    used for auxiliary labels that aren't part of the canonical FSM but
    must be cleared alongside the state change.

    *set_labels* is injectable for tests; defaults to
    ``cai_lib.github._set_labels``.
    """
    transition = find_transition(transition_name, ISSUE_TRANSITIONS)

    if current_labels is not None:
        current = get_issue_state(current_labels)
        if current != transition.from_state:
            print(
                f"[{log_prefix}] refusing transition {transition_name!r} on "
                f"#{issue_number}: current state {current} does not match "
                f"expected {transition.from_state}",
                file=sys.stderr,
            )
            return False

    if set_labels is None:
        from cai_lib.github import _set_labels as set_labels  # local import — avoids cycle at module load

    return set_labels(
        issue_number,
        add=list(transition.labels_add),
        remove=list(transition.labels_remove) + list(extra_remove),
        log_prefix=log_prefix,
    )


def apply_transition_with_confidence(
    issue_number: int,
    transition_name: str,
    confidence: Optional[Confidence],
    *,
    current_labels: Optional[list[str]] = None,
    extra_remove: Sequence[str] = (),
    log_prefix: str = "cai",
    set_labels=None,
) -> tuple[bool, bool]:
    """Apply an issue FSM transition gated on *confidence*.

    Returns ``(ok, diverted)``:

    - When *confidence* is missing or below ``transition.min_confidence``,
      the intended state change is refused and the issue is instead moved
      to ``transition.human_label_if_below`` (defaults to
      :data:`LABEL_HUMAN_NEEDED`). The caller is responsible for appending
      a pending marker to the issue body so the resume loop can pick up
      where the automation stopped — see :func:`render_pending_marker`.
    - When confidence meets the threshold, delegates to
      :func:`apply_transition` and returns ``(ok, False)``.
    """
    transition = find_transition(transition_name, ISSUE_TRANSITIONS)

    if transition.accepts(confidence):
        ok = apply_transition(
            issue_number, transition_name,
            current_labels=current_labels,
            extra_remove=extra_remove,
            log_prefix=log_prefix,
            set_labels=set_labels,
        )
        return ok, False

    # Divert: clear the from_state label and apply the human-needed label.
    if current_labels is not None:
        current = get_issue_state(current_labels)
        if current != transition.from_state:
            print(
                f"[{log_prefix}] refusing divert for {transition_name!r} on "
                f"#{issue_number}: current state {current} does not match "
                f"expected {transition.from_state}",
                file=sys.stderr,
            )
            return False, False

    if set_labels is None:
        from cai_lib.github import _set_labels as set_labels  # local import — avoids cycle at module load

    conf_name = confidence.name if confidence is not None else "MISSING"
    print(
        f"[{log_prefix}] diverting {transition_name!r} on #{issue_number} to "
        f"{transition.human_label_if_below} (confidence={conf_name}, "
        f"required={transition.min_confidence.name})",
        flush=True,
    )
    ok = set_labels(
        issue_number,
        add=[transition.human_label_if_below],
        remove=list(transition.labels_remove) + list(extra_remove),
        log_prefix=log_prefix,
    )
    return ok, True


def resume_transition_for(target_state_name: str) -> Optional[Transition]:
    """Map a ``ResumeTo: <STATE>`` token to the matching ``human_to_<state>`` transition.

    Only transitions whose ``from_state`` is :attr:`IssueState.HUMAN_NEEDED`
    are considered. Returns ``None`` when the name does not correspond to
    a known IssueState or no resume transition lands on that state.
    """
    if not target_state_name:
        return None
    try:
        target = IssueState[target_state_name.upper()]
    except KeyError:
        return None
    for t in ISSUE_TRANSITIONS:
        if t.from_state == IssueState.HUMAN_NEEDED and t.to_state == target:
            return t
    return None


def apply_pr_transition(
    pr_number: int,
    transition_name: str,
    *,
    current_pr: Optional[dict] = None,
    log_prefix: str = "cai",
    set_pr_labels=None,
) -> bool:
    """Apply a named PR FSM transition via ``_set_pr_labels``.

    When *current_pr* is provided, the current PRState is derived and
    compared to ``transition.from_state``. A mismatch is refused (logs
    and returns False) so drift cannot silently compound.

    *set_pr_labels* is injectable for tests; defaults to
    ``cai_lib.github._set_pr_labels``.
    """
    transition = find_transition(transition_name, PR_TRANSITIONS)

    if current_pr is not None:
        current = get_pr_state(current_pr)
        if current != transition.from_state:
            print(
                f"[{log_prefix}] refusing PR transition {transition_name!r} on "
                f"#{pr_number}: current state {current} does not match "
                f"expected {transition.from_state}",
                file=sys.stderr,
            )
            return False

    if set_pr_labels is None:
        from cai_lib.github import _set_pr_labels as set_pr_labels  # local import — avoids cycle at module load

    return set_pr_labels(
        pr_number,
        add=list(transition.labels_add),
        remove=list(transition.labels_remove),
        log_prefix=log_prefix,
    )


def apply_pr_transition_with_confidence(
    pr_number: int,
    transition_name: str,
    confidence: Optional[Confidence],
    *,
    current_pr: Optional[dict] = None,
    log_prefix: str = "cai",
    set_pr_labels=None,
) -> tuple[bool, bool]:
    """Confidence-gated PR transition. Mirrors ``apply_transition_with_confidence``."""
    transition = find_transition(transition_name, PR_TRANSITIONS)

    if transition.accepts(confidence):
        ok = apply_pr_transition(
            pr_number, transition_name,
            current_pr=current_pr,
            log_prefix=log_prefix,
            set_pr_labels=set_pr_labels,
        )
        return ok, False

    if current_pr is not None:
        current = get_pr_state(current_pr)
        if current != transition.from_state:
            print(
                f"[{log_prefix}] refusing PR divert for {transition_name!r} on "
                f"#{pr_number}: current state {current} does not match "
                f"expected {transition.from_state}",
                file=sys.stderr,
            )
            return False, False

    if set_pr_labels is None:
        from cai_lib.github import _set_pr_labels as set_pr_labels  # local import — avoids cycle at module load

    conf_name = confidence.name if confidence is not None else "MISSING"
    print(
        f"[{log_prefix}] diverting PR {transition_name!r} on #{pr_number} to "
        f"{transition.human_label_if_below} (confidence={conf_name}, "
        f"required={transition.min_confidence.name})",
        flush=True,
    )
    ok = set_pr_labels(
        pr_number,
        add=[transition.human_label_if_below],
        remove=list(transition.labels_remove),
        log_prefix=log_prefix,
    )
    return ok, True


def resume_pr_transition_for(target_state_name: str) -> Optional[Transition]:
    """PR-submachine counterpart of :func:`resume_transition_for`.

    Maps a ``ResumeTo: <STATE>`` token to the matching
    ``pr_human_to_<state>`` transition whose ``from_state`` is
    :attr:`PRState.PR_HUMAN_NEEDED`. Returns ``None`` when the name is
    not a known :class:`PRState` member or no resume transition lands
    on that state.

    The two resolvers are split (rather than unified) because
    :attr:`IssueState.MERGED` and :attr:`PRState.MERGED` share a name —
    the caller already knows whether it's acting on an issue or a PR,
    so each side stays unambiguous by construction.
    """
    if not target_state_name:
        return None
    try:
        target = PRState[target_state_name.upper()]
    except KeyError:
        return None
    for t in PR_TRANSITIONS:
        if t.from_state == PRState.PR_HUMAN_NEEDED and t.to_state == target:
            return t
    return None


def render_fsm_mermaid(transitions: list[Transition], title: str = "FSM") -> str:
    """Render *transitions* as a Mermaid stateDiagram-v2 block."""
    lines = ["stateDiagram-v2"]
    for t in transitions:
        from_name = t.from_state.name if hasattr(t.from_state, "name") else str(t.from_state)
        to_name   = t.to_state.name   if hasattr(t.to_state,   "name") else str(t.to_state)
        gate = f"≥{t.min_confidence.name}" if t.min_confidence is not None else "caller-gated"
        label = f"{t.name} [{gate}]"
        lines.append(f"    {from_name} --> {to_name} : {label}")
    return "\n".join(lines)
