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
    LABEL_RAISED, LABEL_REFINED, LABEL_PLANNED, LABEL_PLAN_APPROVED,
    LABEL_IN_PROGRESS, LABEL_IN_PR, LABEL_MERGED, LABEL_SOLVED,
    LABEL_NEEDS_EXPLORATION, LABEL_HUMAN_NEEDED, LABEL_PR_HUMAN_NEEDED,
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
    REFINED           = LABEL_REFINED
    PLANNED           = LABEL_PLANNED
    PLAN_APPROVED     = LABEL_PLAN_APPROVED
    IN_PROGRESS       = LABEL_IN_PROGRESS
    PR                = LABEL_IN_PR        # currently in the PR submachine
    MERGED            = LABEL_MERGED
    SOLVED            = LABEL_SOLVED
    NEEDS_EXPLORATION = LABEL_NEEDS_EXPLORATION
    HUMAN_NEEDED      = LABEL_HUMAN_NEEDED


class PRState(str, Enum):
    OPEN              = "pr:open"
    REVIEWING         = "pr:reviewing"
    REVISION_PENDING  = "pr:revision_pending"
    APPROVED          = "pr:approved"
    MERGED            = "pr:merged"
    PR_HUMAN_NEEDED   = "pr:human_needed"


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
    min_confidence: Confidence = Confidence.HIGH
    human_label_if_below: str = LABEL_HUMAN_NEEDED

    def accepts(self, confidence: Optional[Confidence]) -> bool:
        """True if *confidence* meets or exceeds this transition's threshold.

        ``None`` always fails — missing confidence must route to human review.
        """
        if confidence is None:
            return False
        return confidence >= self.min_confidence


ISSUE_TRANSITIONS: list[Transition] = [
    Transition("raise_to_refine",         IssueState.RAISED,            IssueState.REFINED,
               labels_remove=[LABEL_RAISED],            labels_add=[LABEL_REFINED]),
    Transition("raise_to_exploration",    IssueState.RAISED,            IssueState.NEEDS_EXPLORATION,
               labels_remove=[LABEL_RAISED],            labels_add=[LABEL_NEEDS_EXPLORATION]),
    Transition("raise_to_human",          IssueState.RAISED,            IssueState.HUMAN_NEEDED,
               labels_remove=[LABEL_RAISED],            labels_add=[LABEL_HUMAN_NEEDED]),
    Transition("refine_to_plan",          IssueState.REFINED,           IssueState.PLANNED,
               labels_remove=[LABEL_REFINED],           labels_add=[LABEL_PLANNED]),
    Transition("plan_to_approved",        IssueState.PLANNED,           IssueState.PLAN_APPROVED,
               labels_remove=[LABEL_PLANNED],           labels_add=[LABEL_PLAN_APPROVED]),
    Transition("approved_to_in_progress", IssueState.PLAN_APPROVED,     IssueState.IN_PROGRESS,
               labels_remove=[LABEL_PLAN_APPROVED],     labels_add=[LABEL_IN_PROGRESS]),
    Transition("refine_to_in_progress",   IssueState.REFINED,           IssueState.IN_PROGRESS,
               labels_remove=[LABEL_REFINED],           labels_add=[LABEL_IN_PROGRESS]),
    Transition("in_progress_to_pr",       IssueState.IN_PROGRESS,       IssueState.PR,
               labels_remove=[LABEL_IN_PROGRESS],       labels_add=[LABEL_IN_PR]),
    Transition("pr_to_merged",            IssueState.PR,                IssueState.MERGED,
               labels_remove=[LABEL_IN_PR],             labels_add=[LABEL_MERGED]),
    Transition("merged_to_solved",        IssueState.MERGED,            IssueState.SOLVED,
               labels_remove=[LABEL_MERGED],            labels_add=[LABEL_SOLVED]),
    Transition("exploration_to_refine",   IssueState.NEEDS_EXPLORATION, IssueState.REFINED,
               labels_remove=[LABEL_NEEDS_EXPLORATION], labels_add=[LABEL_REFINED]),
    Transition("human_to_raised",         IssueState.HUMAN_NEEDED,      IssueState.RAISED,
               labels_remove=[LABEL_HUMAN_NEEDED],      labels_add=[LABEL_RAISED]),
    # Admin-comment-driven re-entries out of HUMAN_NEEDED. Fired by
    # cmd_unblock after a Haiku agent classifies the admin's reply.
    Transition("human_to_refined",        IssueState.HUMAN_NEEDED,      IssueState.REFINED,
               labels_remove=[LABEL_HUMAN_NEEDED],      labels_add=[LABEL_REFINED]),
    Transition("human_to_planned",        IssueState.HUMAN_NEEDED,      IssueState.PLANNED,
               labels_remove=[LABEL_HUMAN_NEEDED],      labels_add=[LABEL_PLANNED]),
    Transition("human_to_plan_approved",  IssueState.HUMAN_NEEDED,      IssueState.PLAN_APPROVED,
               labels_remove=[LABEL_HUMAN_NEEDED],      labels_add=[LABEL_PLAN_APPROVED]),
    Transition("human_to_exploration",    IssueState.HUMAN_NEEDED,      IssueState.NEEDS_EXPLORATION,
               labels_remove=[LABEL_HUMAN_NEEDED],      labels_add=[LABEL_NEEDS_EXPLORATION]),
    Transition("human_to_solved",         IssueState.HUMAN_NEEDED,      IssueState.SOLVED,
               labels_remove=[LABEL_HUMAN_NEEDED],      labels_add=[LABEL_SOLVED]),
]


PR_TRANSITIONS: list[Transition] = [
    Transition("pr_open_to_reviewing",          PRState.OPEN,             PRState.REVIEWING,
               human_label_if_below=LABEL_PR_HUMAN_NEEDED),
    Transition("reviewing_to_revision_pending", PRState.REVIEWING,        PRState.REVISION_PENDING,
               human_label_if_below=LABEL_PR_HUMAN_NEEDED),
    Transition("revision_pending_to_reviewing", PRState.REVISION_PENDING, PRState.REVIEWING,
               human_label_if_below=LABEL_PR_HUMAN_NEEDED),
    Transition("reviewing_to_approved",         PRState.REVIEWING,        PRState.APPROVED,
               human_label_if_below=LABEL_PR_HUMAN_NEEDED),
    Transition("approved_to_merged",            PRState.APPROVED,         PRState.MERGED,
               human_label_if_below=LABEL_PR_HUMAN_NEEDED),
    Transition("pr_to_human",                   PRState.REVIEWING,        PRState.PR_HUMAN_NEEDED,
               human_label_if_below=LABEL_PR_HUMAN_NEEDED),
    Transition("pr_human_to_reviewing",         PRState.PR_HUMAN_NEEDED,  PRState.REVIEWING,
               human_label_if_below=LABEL_PR_HUMAN_NEEDED),
    # Admin-comment-driven re-entries out of PR_HUMAN_NEEDED.
    Transition("pr_human_to_revision_pending",  PRState.PR_HUMAN_NEEDED,  PRState.REVISION_PENDING,
               human_label_if_below=LABEL_PR_HUMAN_NEEDED),
    Transition("pr_human_to_approved",          PRState.PR_HUMAN_NEEDED,  PRState.APPROVED,
               human_label_if_below=LABEL_PR_HUMAN_NEEDED),
    Transition("pr_human_to_merged",            PRState.PR_HUMAN_NEEDED,  PRState.MERGED,
               human_label_if_below=LABEL_PR_HUMAN_NEEDED),
]


def get_issue_state(labels: list[str]) -> Optional[IssueState]:
    """Return the first IssueState whose label value appears in *labels*."""
    label_set = set(labels)
    for state in IssueState:
        if state.value in label_set:
            return state
    return None


def get_pr_state(pr: dict) -> PRState:
    """Derive the current PRState from a GitHub PR JSON dict."""
    if pr.get("merged") or pr.get("mergedAt") or pr.get("state") == "MERGED":
        return PRState.MERGED
    review_decision = pr.get("reviewDecision") or ""
    if review_decision == "APPROVED":
        return PRState.APPROVED
    if review_decision == "CHANGES_REQUESTED":
        return PRState.REVISION_PENDING
    reviews = pr.get("reviews", {})
    total_count = 0
    if isinstance(reviews, dict):
        total_count = reviews.get("totalCount", 0)
    elif isinstance(reviews, list):
        total_count = len(reviews)
    if total_count > 0:
        return PRState.REVIEWING
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
    used for auxiliary labels (e.g. ``human:submitted``) that aren't part
    of the canonical FSM but must be cleared alongside the state change.

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


def render_fsm_mermaid(transitions: list[Transition], title: str = "FSM") -> str:
    """Render *transitions* as a Mermaid stateDiagram-v2 block."""
    lines = ["stateDiagram-v2"]
    for t in transitions:
        from_name = t.from_state.name if hasattr(t.from_state, "name") else str(t.from_state)
        to_name   = t.to_state.name   if hasattr(t.to_state,   "name") else str(t.to_state)
        label = f"{t.name} [≥{t.min_confidence.name}]"
        lines.append(f"    {from_name} --> {to_name} : {label}")
    return "\n".join(lines)
