"""FSM transition data and logic for the auto-improve lifecycle.

Defines the :class:`Transition` dataclass, the canonical transition lists
(:data:`ISSUE_TRANSITIONS`, :data:`PR_TRANSITIONS`), and all functions that
apply or query transitions. State enums live in :mod:`cai_lib.fsm_states`;
confidence parsing lives in :mod:`cai_lib.fsm_confidence`.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
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
from cai_lib.fsm_states import IssueState, PRState
from cai_lib.fsm_confidence import Confidence


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

    # APPLYING is transient — handle_maintain (dispatcher) drains it.
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
    # Recovery paths out of PR when the linked PR was closed unmerged
    # (re-plan from scratch) or never existed (orphan — needs a human).
    # Fired by handle_pr_bounce after inspecting recent closed PRs for
    # the issue's branch.
    Transition("pr_to_refined",              IssueState.PR,                IssueState.REFINED,
               labels_remove=[LABEL_PR_OPEN],             labels_add=[LABEL_REFINED]),
    Transition("pr_to_human_needed",         IssueState.PR,                IssueState.HUMAN_NEEDED,
               labels_remove=[LABEL_PR_OPEN],             labels_add=[LABEL_HUMAN_NEEDED]),
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
    # Merge handler park: when the merge agent's verdict is below the
    # configured confidence threshold, or when a recovery action (close /
    # merge) failed, the PR must leave APPROVED so the dispatcher stops
    # re-routing it to handle_merge. Without this transition the PR
    # carried a parallel ``needs-human-review`` flag while still labelled
    # ``pr:approved`` — two states disagreeing — and the merge handler
    # was re-invoked every drain tick only to short-circuit on its
    # "already evaluated at this SHA" guard.
    Transition("approved_to_human",
               PRState.APPROVED, PRState.PR_HUMAN_NEEDED,
               labels_remove=[LABEL_PR_APPROVED],
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


def _render_human_divert_reason(
    *,
    transition_name: str,
    transition: "Transition",
    confidence: Optional[Confidence],
    extra: str = "",
) -> str:
    """Render the user-visible reason for a confidence-gated divert.

    Kept close to the divert call sites so a future change to gate
    semantics only needs to touch one formatter.
    """
    conf_name = confidence.name if confidence is not None else "MISSING"
    required = transition.min_confidence.name
    lines = [
        "**🙋 Human attention needed**",
        "",
        f"Automation paused `{transition_name}` because the confidence gate "
        f"was not met.",
        "",
        f"- Required confidence: `{required}`",
        f"- Reported confidence: `{conf_name}`",
    ]
    if extra:
        lines.extend(["", extra.rstrip()])
    lines.extend([
        "",
        "Apply the `human:solved` label after leaving a comment to signal "
        "the divert is resolved and have the FSM resume.",
    ])
    return "\n".join(lines)


def apply_transition_with_confidence(
    issue_number: int,
    transition_name: str,
    confidence: Optional[Confidence],
    *,
    current_labels: Optional[list[str]] = None,
    extra_remove: Sequence[str] = (),
    log_prefix: str = "cai",
    set_labels=None,
    post_comment=None,
    reason_extra: str = "",
) -> tuple[bool, bool]:
    """Apply an issue FSM transition gated on *confidence*.

    Returns ``(ok, diverted)``:

    - When *confidence* is missing or below ``transition.min_confidence``,
      the intended state change is refused and the issue is instead moved
      to ``transition.human_label_if_below`` (defaults to
      :data:`LABEL_HUMAN_NEEDED`). An admin resumes the FSM by leaving a
      comment and applying ``human:solved`` — see :mod:`cai_lib.cmd_unblock`.
    - When confidence meets the threshold, delegates to
      :func:`apply_transition` and returns ``(ok, False)``.

    On a successful divert, also posts a comment on the issue explaining
    the reason (the failing transition and confidence gate). ``post_comment``
    is injectable for tests; defaults to ``cai_lib.github._post_issue_comment``.
    ``reason_extra`` lets the caller append handler-specific context (e.g. a
    failed-transition name when the divert is not driven by confidence).
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
    if ok:
        if post_comment is None:
            from cai_lib.github import _post_issue_comment as post_comment  # local import — avoids cycle
        post_comment(
            issue_number,
            _render_human_divert_reason(
                transition_name=transition_name,
                transition=transition,
                confidence=confidence,
                extra=reason_extra,
            ),
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
    post_comment=None,
    reason_extra: str = "",
) -> tuple[bool, bool]:
    """Confidence-gated PR transition. Mirrors ``apply_transition_with_confidence``.

    On successful divert, posts a comment on the PR with the failing
    transition / confidence values. ``post_comment`` is injectable for tests;
    defaults to ``cai_lib.github._post_pr_comment``.
    """
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
    if ok:
        if post_comment is None:
            from cai_lib.github import _post_pr_comment as post_comment  # local import — avoids cycle
        post_comment(
            pr_number,
            _render_human_divert_reason(
                transition_name=transition_name,
                transition=transition,
                confidence=confidence,
                extra=reason_extra,
            ),
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
