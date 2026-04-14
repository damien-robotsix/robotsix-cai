"""FSM data structures for the auto-improve lifecycle.

This module defines the explicit state machine that the auto-improve
pipeline follows. It is a pure data-structure module — no behaviour
in cai.py is changed by importing it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from cai_lib.config import (
    LABEL_RAISED, LABEL_REFINED, LABEL_PLANNED, LABEL_PLAN_APPROVED,
    LABEL_IN_PROGRESS, LABEL_IN_PR, LABEL_MERGED, LABEL_SOLVED,
    LABEL_NEEDS_EXPLORATION, LABEL_HUMAN_NEEDED, LABEL_PR_HUMAN_NEEDED,
)


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
    min_confidence: float = 0.0
    human_label_if_below: str = LABEL_HUMAN_NEEDED


ISSUE_TRANSITIONS: list[Transition] = [
    Transition("raise_to_refine",         IssueState.RAISED,            IssueState.REFINED,
               labels_remove=[LABEL_RAISED],            labels_add=[LABEL_REFINED],           min_confidence=0.6),
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
               labels_remove=[LABEL_IN_PR],             labels_add=[LABEL_MERGED],            min_confidence=0.8),
    Transition("merged_to_solved",        IssueState.MERGED,            IssueState.SOLVED,
               labels_remove=[LABEL_MERGED],            labels_add=[LABEL_SOLVED],            min_confidence=0.7),
    Transition("exploration_to_refine",   IssueState.NEEDS_EXPLORATION, IssueState.REFINED,
               labels_remove=[LABEL_NEEDS_EXPLORATION], labels_add=[LABEL_REFINED]),
    Transition("human_to_raised",         IssueState.HUMAN_NEEDED,      IssueState.RAISED,
               labels_remove=[LABEL_HUMAN_NEEDED],      labels_add=[LABEL_RAISED]),
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
               min_confidence=0.8, human_label_if_below=LABEL_PR_HUMAN_NEEDED),
    Transition("pr_to_human",                   PRState.REVIEWING,        PRState.PR_HUMAN_NEEDED,
               human_label_if_below=LABEL_PR_HUMAN_NEEDED),
    Transition("pr_human_to_reviewing",         PRState.PR_HUMAN_NEEDED,  PRState.REVIEWING,
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


def render_fsm_mermaid(transitions: list[Transition], title: str = "FSM") -> str:
    """Render *transitions* as a Mermaid stateDiagram-v2 block."""
    lines = ["stateDiagram-v2"]
    for t in transitions:
        from_name = t.from_state.name if hasattr(t.from_state, "name") else str(t.from_state)
        to_name   = t.to_state.name   if hasattr(t.to_state,   "name") else str(t.to_state)
        label = t.name
        if t.min_confidence > 0.0:
            label = f"{t.name} [≥{t.min_confidence}]"
        lines.append(f"    {from_name} --> {to_name} : {label}")
    return "\n".join(lines)
