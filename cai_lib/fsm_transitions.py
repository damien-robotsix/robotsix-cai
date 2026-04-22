"""FSM transition data and logic for the auto-improve lifecycle.

Defines the :class:`Transition` dataclass, the canonical transition lists
(:data:`ISSUE_TRANSITIONS`, :data:`PR_TRANSITIONS`), and the :func:`fire_trigger`
dispatch function. State enums live in :mod:`cai_lib.fsm_states`;
confidence parsing lives in :mod:`cai_lib.fsm_confidence`.
"""
from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from typing import Callable, Optional, Sequence

from transitions import Machine, MachineError
from transitions.extensions import GraphMachine

from cai_lib.config import (
    LABEL_RAISED, LABEL_REFINING, LABEL_REFINED, LABEL_SPLITTING,
    LABEL_PLANNING, LABEL_PLANNED, LABEL_PLAN_APPROVED,
    LABEL_IN_PROGRESS, LABEL_PR_OPEN, LABEL_MERGED, LABEL_SOLVED,
    LABEL_NEEDS_EXPLORATION, LABEL_HUMAN_NEEDED, LABEL_PR_HUMAN_NEEDED,
    LABEL_TRIAGING, LABEL_APPLYING, LABEL_APPLIED,
    LABEL_PLAN_NEEDS_REVIEW, LABEL_RESCUE_ATTEMPTED,
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
    # Relaxed threshold (#986): when cai-maintain synthesised the Ops
    # from a stored plan block because no explicit `Ops:` header was
    # present on the issue body, it emits an `Ops-source: inferred`
    # marker and the handler picks this sibling transition so a
    # successful inferred-ops execution at MEDIUM confidence still
    # advances to :applied rather than parking at :human-needed. The
    # only difference from applying_to_applied is the min_confidence
    # gate (MEDIUM instead of HIGH); labels move identically.
    Transition("applying_to_applied_inferred_ops", IssueState.APPLYING, IssueState.APPLIED,
               labels_remove=[LABEL_APPLYING],   labels_add=[LABEL_APPLIED],
               min_confidence=Confidence.MEDIUM),
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

    # REFINED → SPLITTING is the auto-advance: the dispatcher picks up
    # a :refined issue and hands it to cai-split for scope evaluation
    # before any plan work. The legacy refined_to_planning edge is kept
    # for backwards compatibility with resume paths but is no longer
    # fired by the unified driver.
    Transition("refined_to_splitting",       IssueState.REFINED,           IssueState.SPLITTING,
               labels_remove=[LABEL_REFINED],           labels_add=[LABEL_SPLITTING]),
    Transition("refined_to_planning",        IssueState.REFINED,           IssueState.PLANNING,
               labels_remove=[LABEL_REFINED],           labels_add=[LABEL_PLANNING]),

    # SPLITTING is transient — cai-split is evaluating whether the
    # refined scope is atomic (single PR) or needs multi-step
    # decomposition. Atomic + HIGH → PLANNING (cai-plan runs next).
    # Decompose + HIGH is handled by handle_split directly via
    # _set_labels(LABEL_PARENT) — no state transition for that branch.
    # LOW confidence or malformed output diverts to HUMAN_NEEDED.
    Transition("splitting_to_planning",      IssueState.SPLITTING,         IssueState.PLANNING,
               labels_remove=[LABEL_SPLITTING],         labels_add=[LABEL_PLANNING]),
    Transition("splitting_to_human",         IssueState.SPLITTING,         IssueState.HUMAN_NEEDED,
               labels_remove=[LABEL_SPLITTING],         labels_add=[LABEL_HUMAN_NEEDED]),

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
    # Anchor-mitigation relaxation (#918): same label move as
    # planned_to_plan_approved, but accepts MEDIUM confidence.
    # handle_plan_gate (cai_lib.actions.plan) picks this transition
    # only when the selected plan text carries an explicit anchor-
    # based risk-mitigation note (the phrase
    # "locate edits by anchor text ... not by line number"), which
    # signals that the only residual risks are implementation-detail
    # (line-number drift, fence escaping, cosmetic wording) and that
    # the fix agent has been instructed to Read first and anchor on
    # surrounding text rather than line numbers. Plans without the
    # marker go through the default HIGH-threshold transition and
    # MEDIUM still diverts them.
    Transition("planned_to_plan_approved_mitigated", IssueState.PLANNED,   IssueState.PLAN_APPROVED,
               labels_remove=[LABEL_PLANNED],           labels_add=[LABEL_PLAN_APPROVED],
               min_confidence=Confidence.MEDIUM),
    # Docs-only relaxation (#989): same label move as
    # planned_to_plan_approved, but accepts MEDIUM confidence.
    # handle_plan_gate (cai_lib.actions.plan) picks this transition
    # purely from the structure of the selected plan — specifically,
    # when every backticked path listed in the plan's
    # ``### Files to change`` section begins with ``docs/``. No plan-
    # text marker phrase is required: the planner already declares
    # its file targets in the standard Files-to-change block, and
    # that declaration is the trusted structural signal. The blast
    # radius of a documentation-only pass is low (no Python, YAML,
    # shell, workflow, or test file is touched), and cai-review-docs
    # owns the affected files on subsequent PRs. Plans whose
    # Files-to-change block includes any non-docs path, or whose
    # block is missing entirely, fall through to the default
    # HIGH-threshold transition and MEDIUM still diverts them.
    Transition("planned_to_plan_approved_docs_only", IssueState.PLANNED,   IssueState.PLAN_APPROVED,
               labels_remove=[LABEL_PLANNED],           labels_add=[LABEL_PLAN_APPROVED],
               min_confidence=Confidence.MEDIUM),
    # Approvable-at-medium relaxation (#1008): same label move as
    # planned_to_plan_approved, but accepts MEDIUM confidence.
    # handle_plan_gate (cai_lib.actions.plan) picks this transition
    # when cai-select's structured JSON output set
    # ``approvable_at_medium: true`` — i.e. the selecting agent judged
    # that the plan's only residual risks are soft / non-blocking
    # (line-number-verification-only, additive schema fields, soft
    # length caps, preferred-but-not-required path divergence) and do
    # not warrant admin intervention. Plans without the flag fall
    # through to the default HIGH-threshold transition; MEDIUM still
    # diverts them so the flag is the sole bypass channel.
    Transition("planned_to_plan_approved_approvable", IssueState.PLANNED,  IssueState.PLAN_APPROVED,
               labels_remove=[LABEL_PLANNED],           labels_add=[LABEL_PLAN_APPROVED],
               min_confidence=Confidence.MEDIUM),
    Transition("planned_to_human",           IssueState.PLANNED,           IssueState.HUMAN_NEEDED,
               labels_remove=[LABEL_PLANNED],           labels_add=[LABEL_HUMAN_NEEDED]),

    Transition("approved_to_in_progress",    IssueState.PLAN_APPROVED,     IssueState.IN_PROGRESS,
               labels_remove=[LABEL_PLAN_APPROVED],     labels_add=[LABEL_IN_PROGRESS]),
    # Admin-sigil-driven rollback (#1142): when an admin drops the
    # ``<!-- cai-resplit -->`` sigil in a comment on a :plan-approved
    # issue, Phase 0.7 of ``cai cycle`` fires this transition so the
    # issue lands at :refined and ``handle_split`` re-evaluates scope
    # decomposition on the next tick. Caller-gated (no FSM-level
    # confidence threshold) — the sigil literal-string match is the
    # sole gate. Parallels ``in_progress_to_refining`` and
    # ``human_to_splitting`` precedents.
    Transition("plan_approved_to_refined",   IssueState.PLAN_APPROVED,     IssueState.REFINED,
               labels_remove=[LABEL_PLAN_APPROVED],     labels_add=[LABEL_REFINED],
               min_confidence=None),
    Transition("in_progress_to_pr",          IssueState.IN_PROGRESS,       IssueState.PR,
               labels_remove=[LABEL_IN_PROGRESS],       labels_add=[LABEL_PR_OPEN]),
    # Re-planning gate for MEDIUM-confidence plans that implement struggled
    # with. Not currently fired by handle_implement (which escalates to
    # :human-needed after 2 consecutive test failures as a cost optimization).
    # Reserved for potential future use when implementing MEDIUM-plan
    # auto-refine logic. Not confidence-gated at the FSM level.
    Transition("in_progress_to_refining",    IssueState.IN_PROGRESS,       IssueState.REFINING,
               labels_remove=[LABEL_IN_PROGRESS],       labels_add=[LABEL_REFINING],
               min_confidence=None),
    # Explicit IN_PROGRESS → HUMAN_NEEDED park used by the four
    # implement-side escalation paths (early-abort guard, pre-screen
    # spike verdict, subagent-no-change spike marker, repeated
    # test-failures on a non-MEDIUM plan). Caller-gated (no FSM-level
    # confidence threshold) — the handler decides when to park and
    # supplies the divert_reason text that fire_trigger renders
    # into the MARKER-bearing comment the audit parser picks up. Added
    # in response to issue #1083: before this transition existed the
    # four code paths called ``_set_labels(add=[LABEL_HUMAN_NEEDED])``
    # directly with a hand-rolled comment that was missing the
    # ``Automation paused`` / ``Required confidence:`` /
    # ``Reported confidence:`` lines, silently breaking
    # ``_fetch_human_needed_issues`` in cmd_agents.py.
    Transition("in_progress_to_human_needed", IssueState.IN_PROGRESS,      IssueState.HUMAN_NEEDED,
               labels_remove=[LABEL_IN_PROGRESS],       labels_add=[LABEL_HUMAN_NEEDED],
               min_confidence=None),
    Transition("pr_to_merged",               IssueState.PR,                IssueState.MERGED,
               labels_remove=[LABEL_PR_OPEN],             labels_add=[LABEL_MERGED]),
    # Recovery paths out of PR when the linked PR was closed unmerged
    # (re-plan from scratch) or never existed (orphan — needs a human).
    # Fired by handle_pr_bounce after inspecting recent closed PRs for
    # the issue's branch.
    Transition("pr_to_refined",              IssueState.PR,                IssueState.REFINED,
               labels_remove=[LABEL_PR_OPEN],             labels_add=[LABEL_REFINED]),
    # Merge-side approach-mismatch escalation (#1075): cai-merge closes
    # the PR and fires this transition so cai-implement re-runs under
    # Opus on the next tick. Caller-gated (handler decides when to
    # fire); no FSM-level confidence threshold.
    Transition("pr_to_plan_approved",        IssueState.PR,                IssueState.PLAN_APPROVED,
               labels_remove=[LABEL_PR_OPEN],             labels_add=[LABEL_PLAN_APPROVED],
               min_confidence=None),
    Transition("pr_to_human_needed",         IssueState.PR,                IssueState.HUMAN_NEEDED,
               labels_remove=[LABEL_PR_OPEN],             labels_add=[LABEL_HUMAN_NEEDED]),
    Transition("merged_to_solved",           IssueState.MERGED,            IssueState.SOLVED,
               labels_remove=[LABEL_MERGED],            labels_add=[LABEL_SOLVED]),

    # Every `human_to_*` resume transition also strips the
    # supplementary LABEL_PLAN_NEEDS_REVIEW marker (#1128) and the
    # LABEL_RESCUE_ATTEMPTED marker so neither signal lingers after the
    # admin has actually resolved the divert. `gh issue edit
    # --remove-label` is idempotent when a label is not present, so the
    # extra entries are no-ops for the common case where the labels were
    # never applied.
    Transition("human_to_raised",            IssueState.HUMAN_NEEDED,      IssueState.RAISED,
               labels_remove=[LABEL_HUMAN_NEEDED, LABEL_PLAN_NEEDS_REVIEW, LABEL_RESCUE_ATTEMPTED],
               labels_add=[LABEL_RAISED]),
    # Admin-comment-driven re-entries out of HUMAN_NEEDED. Fired by
    # cmd_unblock after a Haiku agent classifies the admin's reply.
    # Resume into REFINING (not REFINED) so the refine agent re-runs
    # with the admin's input in context — REFINED is an auto-advance
    # waypoint, not a sensible re-entry point.
    Transition("human_to_refining",          IssueState.HUMAN_NEEDED,      IssueState.REFINING,
               labels_remove=[LABEL_HUMAN_NEEDED, LABEL_PLAN_NEEDS_REVIEW, LABEL_RESCUE_ATTEMPTED],
               labels_add=[LABEL_REFINING]),
    # Admin wants cai-split to re-evaluate scope without re-running
    # cai-refine (e.g. they've manually narrowed the refined body and
    # want a fresh atomic/decompose verdict).
    Transition("human_to_splitting",         IssueState.HUMAN_NEEDED,      IssueState.SPLITTING,
               labels_remove=[LABEL_HUMAN_NEEDED, LABEL_PLAN_NEEDS_REVIEW, LABEL_RESCUE_ATTEMPTED],
               labels_add=[LABEL_SPLITTING]),
    # Admin greenlights the already-stored plan — jump past the
    # planned→approved gate.
    Transition("human_to_plan_approved",     IssueState.HUMAN_NEEDED,      IssueState.PLAN_APPROVED,
               labels_remove=[LABEL_HUMAN_NEEDED, LABEL_PLAN_NEEDS_REVIEW, LABEL_RESCUE_ATTEMPTED],
               labels_add=[LABEL_PLAN_APPROVED]),
    Transition("human_to_exploration",       IssueState.HUMAN_NEEDED,      IssueState.NEEDS_EXPLORATION,
               labels_remove=[LABEL_HUMAN_NEEDED, LABEL_PLAN_NEEDS_REVIEW, LABEL_RESCUE_ATTEMPTED],
               labels_add=[LABEL_NEEDS_EXPLORATION]),
    Transition("human_to_solved",            IssueState.HUMAN_NEEDED,      IssueState.SOLVED,
               labels_remove=[LABEL_HUMAN_NEEDED, LABEL_PLAN_NEEDS_REVIEW, LABEL_RESCUE_ATTEMPTED],
               labels_add=[LABEL_SOLVED]),
]


PR_TRANSITIONS: list[Transition] = [
    # Entry: brand-new PR → code review.
    Transition("open_to_reviewing_code",
               PRState.OPEN, PRState.REVIEWING_CODE,
               labels_add=[LABEL_PR_REVIEWING_CODE],
               human_label_if_below=LABEL_PR_HUMAN_NEEDED),
    # Non-bot-branch park (#1065): when a PR is opened on a branch
    # that does NOT match ``auto-improve/<N>-…``, ``handle_open_to_review``
    # applies this transition instead of ``open_to_reviewing_code`` so
    # the PR parks at ``pr:human-needed`` at PR-open time — before any
    # review / rebase / docs cycle is spent. Without this sibling the
    # only park path was ``approved_to_human`` in ``handle_merge``
    # (``result=not_bot_branch``), reached only after a full pipeline
    # run. Not FSM-confidence-gated — the handler decides
    # deterministically from the branch name.
    Transition("open_to_human",
               PRState.OPEN, PRState.PR_HUMAN_NEEDED,
               labels_add=[LABEL_PR_HUMAN_NEEDED],
               min_confidence=None,
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
    # Merge handler redirect: when the merge agent's verdict is LOW
    # confidence + action=hold AND the reasoning cites only a concrete
    # mechanically-fixable code bug (AttributeError, NameError, wrong
    # field / method name, typo, etc.), route back through cai-revise
    # instead of parking at PR_HUMAN_NEEDED. cai-revise can address the
    # cited bug in one shot, whereas parking would burn a rescue cycle
    # for a trivially fixable finding (issue #1055). The merge handler
    # is responsible for (a) posting a follow-up comment with a heading
    # NOT in cai-comment-filter's bot-self-comment allowlist so the
    # filter treats it as unresolved, and (b) firing this transition
    # only on LOW+hold verdicts that match the concrete-bug detector.
    # All other held verdicts continue to use ``approved_to_human``.
    Transition("approved_to_revision_pending",
               PRState.APPROVED, PRState.REVISION_PENDING,
               labels_remove=[LABEL_PR_APPROVED],
               labels_add=[LABEL_PR_REVISION_PENDING],
               human_label_if_below=LABEL_PR_HUMAN_NEEDED),
    # PR-side resume transitions also strip LABEL_RESCUE_ATTEMPTED so a
    # PR that re-enters PR_HUMAN_NEEDED later gets a fresh autonomous
    # rescue evaluation (matches the issue-side `human_to_*` policy).
    Transition("pr_human_to_reviewing_code",
               PRState.PR_HUMAN_NEEDED, PRState.REVIEWING_CODE,
               labels_remove=[LABEL_PR_HUMAN_NEEDED, LABEL_RESCUE_ATTEMPTED],
               labels_add=[LABEL_PR_REVIEWING_CODE],
               human_label_if_below=LABEL_PR_HUMAN_NEEDED),
    Transition("pr_human_to_revision_pending",
               PRState.PR_HUMAN_NEEDED, PRState.REVISION_PENDING,
               labels_remove=[LABEL_PR_HUMAN_NEEDED, LABEL_RESCUE_ATTEMPTED],
               labels_add=[LABEL_PR_REVISION_PENDING],
               human_label_if_below=LABEL_PR_HUMAN_NEEDED),
    Transition("pr_human_to_reviewing_docs",
               PRState.PR_HUMAN_NEEDED, PRState.REVIEWING_DOCS,
               labels_remove=[LABEL_PR_HUMAN_NEEDED, LABEL_RESCUE_ATTEMPTED],
               labels_add=[LABEL_PR_REVIEWING_DOCS],
               human_label_if_below=LABEL_PR_HUMAN_NEEDED),
    Transition("pr_human_to_approved",
               PRState.PR_HUMAN_NEEDED, PRState.APPROVED,
               labels_remove=[LABEL_PR_HUMAN_NEEDED, LABEL_RESCUE_ATTEMPTED],
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


# ──────────────────────────────────────────────────────────────────────────
# Machine-based FSM dispatch — fire_trigger() and supporting internals
# ──────────────────────────────────────────────────────────────────────────

class _FsmModel:
    """Minimal model object for per-call ephemeral pytransitions.Machine."""
    pass


def _confidence_ok(min_confidence: Optional[Confidence]) -> Callable:
    """Factory returning a pytransitions condition callable.

    When *_confidence_gated* is False in ``event_data.kwargs`` (e.g. from
    confidence-free callers), the check is
    bypassed so such callers never accidentally trigger a divert.
    """
    def _check(event_data) -> bool:
        if not event_data.kwargs.get("_confidence_gated", False):
            return True  # No gating requested; always pass primary
        if min_confidence is None:
            return True  # No FSM-level gate on this transition
        supplied = event_data.kwargs.get("_confidence")
        return supplied is not None and supplied >= min_confidence
    return _check


def _before_human_needed(event_data) -> None:
    """Enforce the HUMAN_NEEDED / PR_HUMAN_NEEDED divert_reason invariant.

    Runs as a pytransitions ``before`` callback on all transitions whose
    destination is HUMAN_NEEDED or PR_HUMAN_NEEDED.  Raises ``MachineError``
    (cancelling the transition) when the caller has not supplied a non-empty
    ``_divert_reason``, enforcing the divert-reason invariant.
    """
    divert_reason = event_data.kwargs.get("_divert_reason") or ""
    if not divert_reason.strip():
        trigger_name = event_data.event.name
        number = event_data.kwargs.get("_number")
        log_prefix = event_data.kwargs.get("_log_prefix", "cai")
        is_pr = event_data.kwargs.get("_is_pr", False)
        entity = "PR_HUMAN_NEEDED" if is_pr else "HUMAN_NEEDED"
        print(
            f"[{log_prefix}] refusing silent {entity} divert "
            f"{trigger_name!r} on #{number}: caller must pass "
            f"a non-empty divert_reason so the divert-reason comment "
            f"can be posted (see cai_lib.fsm_transitions invariant)",
            file=sys.stderr,
        )
        raise MachineError(f"Missing divert_reason for {entity} transition")


def _after_label_change_normal(event_data) -> None:
    """After-callback for normal (non-divert) transitions.

    Applies ``labels_add`` / ``labels_remove`` from the catalog entry and,
    when the destination is HUMAN_NEEDED / PR_HUMAN_NEEDED, posts the
    ``_divert_reason`` comment supplied by the caller.
    """
    trigger_name = event_data.event.name
    is_pr = event_data.kwargs.get("_is_pr", False)
    number = event_data.kwargs.get("_number")
    extra_remove = event_data.kwargs.get("_extra_remove", ())
    log_prefix = event_data.kwargs.get("_log_prefix", "cai")
    divert_reason = event_data.kwargs.get("_divert_reason") or ""
    set_fn = event_data.kwargs.get("_set_pr_labels_fn" if is_pr else "_set_labels_fn")
    post_fn = event_data.kwargs.get("_post_comment_fn")
    result_box = event_data.kwargs.get("_result_box", {})

    transition_list = PR_TRANSITIONS if is_pr else ISSUE_TRANSITIONS
    original_trans = find_transition(trigger_name, transition_list)

    add_labels = list(original_trans.labels_add)
    remove_labels = list(original_trans.labels_remove) + list(extra_remove)

    if set_fn is None:
        if is_pr:
            from cai_lib.github import _set_pr_labels as set_fn  # local import — avoids cycle
        else:
            from cai_lib.github import _set_labels as set_fn  # local import — avoids cycle

    ok = set_fn(number, add=add_labels, remove=remove_labels, log_prefix=log_prefix)
    result_box["ok"] = ok
    if not ok:
        return

    # Post HUMAN_NEEDED comment for explicit human-destination transitions.
    human_dest_name = PRState.PR_HUMAN_NEEDED.name if is_pr else IssueState.HUMAN_NEEDED.name
    if original_trans.to_state.name != human_dest_name:
        return

    msg = _render_human_divert_reason(
        transition_name=trigger_name,
        transition=original_trans,
        confidence=None,
        extra=divert_reason,
    )
    if post_fn is None:
        if is_pr:
            from cai_lib.github import _post_pr_comment as post_fn  # local import — avoids cycle
        else:
            from cai_lib.github import _post_issue_comment as post_fn  # local import — avoids cycle
    post_fn(number, msg, log_prefix=log_prefix)


def _after_label_change_divert(event_data) -> None:
    """After-callback for confidence-gated divert siblings.

    Applies ``human_label_if_below`` + ``labels_remove`` from the original
    catalog entry and posts the confidence-gate divert reason comment so the
    audit parser and ``cai unblock`` have context.
    """
    trigger_name = event_data.event.name
    is_pr = event_data.kwargs.get("_is_pr", False)
    number = event_data.kwargs.get("_number")
    extra_remove = event_data.kwargs.get("_extra_remove", ())
    log_prefix = event_data.kwargs.get("_log_prefix", "cai")
    confidence = event_data.kwargs.get("_confidence")
    reason_extra = event_data.kwargs.get("_reason_extra", "")
    set_fn = event_data.kwargs.get("_set_pr_labels_fn" if is_pr else "_set_labels_fn")
    post_fn = event_data.kwargs.get("_post_comment_fn")
    result_box = event_data.kwargs.get("_result_box", {})

    transition_list = PR_TRANSITIONS if is_pr else ISSUE_TRANSITIONS
    original_trans = find_transition(trigger_name, transition_list)

    add_labels = [original_trans.human_label_if_below]
    remove_labels = list(original_trans.labels_remove) + list(extra_remove)

    if set_fn is None:
        if is_pr:
            from cai_lib.github import _set_pr_labels as set_fn  # local import — avoids cycle
        else:
            from cai_lib.github import _set_labels as set_fn  # local import — avoids cycle

    ok = set_fn(number, add=add_labels, remove=remove_labels, log_prefix=log_prefix)
    result_box["ok"] = ok
    if not ok:
        return

    # Post confidence-gate divert reason comment.
    msg = _render_human_divert_reason(
        transition_name=trigger_name,
        transition=original_trans,
        confidence=confidence,
        extra=reason_extra,
    )
    if post_fn is None:
        if is_pr:
            from cai_lib.github import _post_pr_comment as post_fn  # local import — avoids cycle
        else:
            from cai_lib.github import _post_issue_comment as post_fn  # local import — avoids cycle
    post_fn(number, msg, log_prefix=log_prefix)


def _build_issue_machine(
    issue_number: int,
    current_labels: Optional[list[str]],
    trigger_name: str,
) -> tuple["Machine", "_FsmModel"]:
    """Construct an ephemeral pytransitions.Machine for issue FSM dispatch.

    Returns ``(machine, model)``.  The model's initial state is derived from
    *current_labels*; when *current_labels* is ``None`` the state is set to
    the ``from_state`` of *trigger_name* so that state-mismatch validation is
    skipped (allows optional state validation for callers that omit
    ``current_labels``).

    Deprecation note: ``Transition.accepts()``, ``Transition.labels_add``,
    and ``Transition.labels_remove`` are still used by the Mermaid renderer
    and the shim adapters; they are preserved on the dataclass for now.
    """
    original_trans = find_transition(trigger_name, ISSUE_TRANSITIONS)
    if current_labels is None:
        initial_state = original_trans.from_state.name
    else:
        state_obj = get_issue_state(current_labels)
        initial_state = state_obj.name if state_obj is not None else IssueState.RAISED.name

    model = _FsmModel()
    machine = Machine(
        model=model,
        states=[s.name for s in IssueState],
        initial=initial_state,
        ignore_invalid_triggers=False,
        auto_transitions=False,
        send_event=True,
    )

    for trans in ISSUE_TRANSITIONS:
        is_human_dest = (trans.to_state == IssueState.HUMAN_NEEDED)
        before_cbs: list = [_before_human_needed] if is_human_dest else []

        if trans.min_confidence is not None:
            # Primary sibling: fires when the confidence check passes.
            machine.add_transition(
                trigger=trans.name,
                source=trans.from_state.name,
                dest=trans.to_state.name,
                conditions=[_confidence_ok(trans.min_confidence)],
                before=before_cbs,
                after=[_after_label_change_normal],
            )
            # Divert sibling: fires when the confidence check fails.
            machine.add_transition(
                trigger=trans.name,
                source=trans.from_state.name,
                dest=IssueState.HUMAN_NEEDED.name,
                unless=[_confidence_ok(trans.min_confidence)],
                after=[_after_label_change_divert],
            )
        else:
            # Unconditional (caller-gated or no confidence gate).
            machine.add_transition(
                trigger=trans.name,
                source=trans.from_state.name,
                dest=trans.to_state.name,
                before=before_cbs,
                after=[_after_label_change_normal],
            )

    return machine, model


def _build_pr_machine(
    pr_number: int,
    current_pr: Optional[dict],
    trigger_name: str,
) -> tuple["Machine", "_FsmModel"]:
    """Construct an ephemeral pytransitions.Machine for PR FSM dispatch.

    Symmetric counterpart of :func:`_build_issue_machine` for the PR
    submachine.  Uses ``PR_TRANSITIONS``, ``get_pr_state``, and
    ``PRState`` in place of their issue equivalents.
    """
    original_trans = find_transition(trigger_name, PR_TRANSITIONS)
    if current_pr is None:
        initial_state = original_trans.from_state.name
    else:
        state_obj = get_pr_state(current_pr)
        initial_state = state_obj.name

    model = _FsmModel()
    machine = Machine(
        model=model,
        states=[s.name for s in PRState],
        initial=initial_state,
        ignore_invalid_triggers=False,
        auto_transitions=False,
        send_event=True,
    )

    for trans in PR_TRANSITIONS:
        is_human_dest = (trans.to_state == PRState.PR_HUMAN_NEEDED)
        before_cbs: list = [_before_human_needed] if is_human_dest else []

        if trans.min_confidence is not None:
            machine.add_transition(
                trigger=trans.name,
                source=trans.from_state.name,
                dest=trans.to_state.name,
                conditions=[_confidence_ok(trans.min_confidence)],
                before=before_cbs,
                after=[_after_label_change_normal],
            )
            machine.add_transition(
                trigger=trans.name,
                source=trans.from_state.name,
                dest=PRState.PR_HUMAN_NEEDED.name,
                unless=[_confidence_ok(trans.min_confidence)],
                after=[_after_label_change_divert],
            )
        else:
            machine.add_transition(
                trigger=trans.name,
                source=trans.from_state.name,
                dest=trans.to_state.name,
                before=before_cbs,
                after=[_after_label_change_normal],
            )

    return machine, model


def fire_trigger(
    number: int,
    trigger_name: str,
    *,
    is_pr: bool = False,
    confidence: Optional[Confidence] = None,
    _confidence_gated: bool = False,
    log_prefix: str = "cai",
    current_labels: Optional[list[str]] = None,
    current_pr: Optional[dict] = None,
    extra_remove: Sequence[str] = (),
    divert_reason: str = "",
    reason_extra: str = "",
    set_labels=None,
    post_comment=None,
    set_pr_labels=None,
) -> tuple[bool, bool]:
    """Single FSM dispatch entry point using an ephemeral pytransitions.Machine.

    Builds a per-call :class:`~transitions.Machine` from ``ISSUE_TRANSITIONS``
    or ``PR_TRANSITIONS``, sets its initial state from *current_labels* /
    *current_pr*, fires *trigger_name*, and applies the corresponding GitHub
    label changes via the after-callbacks.

    Args:
        number: Issue or PR number.
        trigger_name: Name of the FSM trigger to fire (must match a
            :attr:`Transition.name` in the appropriate catalog).
        is_pr: ``True`` for PR transitions, ``False`` (default) for issues.
        confidence: Confidence level reported by the agent; only relevant
            when *_confidence_gated* is ``True``.
        _confidence_gated: When ``True`` the confidence check is applied and
            a below-threshold confidence diverts to HUMAN_NEEDED.  When
            ``False`` the transition fires unconditionally (no divert risk).
        log_prefix: Prefix for log messages.
        current_labels: Current issue labels used to derive the initial FSM
            state.  When ``None`` the initial state is set to the
            transition's ``from_state`` so state-mismatch validation is
            skipped (backward-compat with callers that omit labels).
        current_pr: Current PR JSON dict used to derive the initial FSM
            state for PR transitions.
        extra_remove: Additional labels to remove beyond the transition's
            own ``labels_remove``.
        divert_reason: Non-empty string required when the transition's
            ``to_state`` is HUMAN_NEEDED / PR_HUMAN_NEEDED.  Enforces the
            silent-divert invariant — human-needed transitions require a reason.
        reason_extra: Extra context appended to the confidence-gate divert
            comment posted when *_confidence_gated* is ``True`` and
            confidence falls below the threshold.
        set_labels: Injectable ``_set_labels`` for tests (issue side).
        post_comment: Injectable comment poster for tests.
        set_pr_labels: Injectable ``_set_pr_labels`` for tests (PR side).

    Returns:
        ``(ok, diverted)`` — *ok* is ``True`` when the transition succeeded
        (labels applied), ``False`` when refused or errored.  *diverted* is
        ``True`` when the transition ended at HUMAN_NEEDED / PR_HUMAN_NEEDED
        rather than the intended destination (confidence too low).

    Raises:
        KeyError: When *trigger_name* is not found in the transition catalog.
    """
    # Validate trigger name first — propagates KeyError for unknown triggers,
    # matching the behaviour callers expect from find_transition.
    transition_list = PR_TRANSITIONS if is_pr else ISSUE_TRANSITIONS
    original_trans = find_transition(trigger_name, transition_list)

    try:
        if is_pr:
            machine, model = _build_pr_machine(number, current_pr, trigger_name)
        else:
            machine, model = _build_issue_machine(number, current_labels, trigger_name)

        result_box: dict = {"ok": True}
        trigger_fn = getattr(model, trigger_name)
        trigger_fn(
            _number=number,
            _is_pr=is_pr,
            _confidence_gated=_confidence_gated,
            _confidence=confidence,
            _log_prefix=log_prefix,
            _extra_remove=tuple(extra_remove),
            _divert_reason=divert_reason or "",
            _reason_extra=reason_extra,
            _set_labels_fn=set_labels,
            _set_pr_labels_fn=set_pr_labels,
            _post_comment_fn=post_comment,
            _result_box=result_box,
        )

        ok = result_box.get("ok", True)
        # Diverted when the machine landed somewhere other than the intended destination.
        diverted = (model.state != original_trans.to_state.name)

        if diverted and _confidence_gated:
            conf_name = confidence.name if confidence is not None else "MISSING"
            req = (
                original_trans.min_confidence.name
                if original_trans.min_confidence is not None
                else "caller-gated"
            )
            print(
                f"[{log_prefix}] diverting {trigger_name!r} on "
                f"{'PR' if is_pr else 'issue'} #{number} to "
                f"{original_trans.human_label_if_below} "
                f"(confidence={conf_name}, required={req})",
                flush=True,
            )

        return ok, diverted

    except MachineError as exc:
        print(
            f"[{log_prefix}] FSM refused {trigger_name!r} on "
            f"{'PR' if is_pr else 'issue'} #{number}: {exc}",
            file=sys.stderr,
        )
        return False, False
    except Exception as exc:
        print(
            f"[{log_prefix}] FSM error firing {trigger_name!r} on "
            f"{'PR' if is_pr else 'issue'} #{number}: {exc}",
            file=sys.stderr,
        )
        return False, False


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
    required = (
        transition.min_confidence.name
        if transition.min_confidence is not None
        else "caller-gated"
    )
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


class _SentinelModel:
    """Empty model passed to :class:`GraphMachine` for diagram-only use.

    ``transitions`` requires a model object to bind triggers to, but
    ``render_fsm_mermaid`` never fires them — the machine exists solely
    to emit a Mermaid source string. The sanitised condition names
    (``ge_HIGH`` / ``ge_MEDIUM`` / ``ge_LOW`` / ``caller_gated``) are
    display-only labels; they are never resolved at runtime because
    nothing ever calls ``.trigger(...)`` on this model.
    """
    pass


def _build_mermaid_machine(transitions_list: list[Transition]) -> GraphMachine:
    """Construct a :class:`GraphMachine` for Mermaid rendering.

    Condition labels are sanitised to valid Python identifiers
    (``ge_HIGH`` / ``ge_MEDIUM`` / ``ge_LOW`` / ``caller_gated``) so
    ``transitions`` accepts them as ``conditions``; :func:`render_fsm_mermaid`
    restores the display form (``≥HIGH`` / ``caller-gated``) via regex
    after rendering. No runtime FSM semantics are wired — the machine
    is a diagram-only construct.
    """
    states: list[str] = []
    for t in transitions_list:
        for name in (t.from_state.name, t.to_state.name):
            if name not in states:
                states.append(name)
    trans_defs = [
        {
            "trigger": t.name,
            "source": t.from_state.name,
            "dest":   t.to_state.name,
            "conditions": (
                f"ge_{t.min_confidence.name}"
                if t.min_confidence is not None
                else "caller_gated"
            ),
        }
        for t in transitions_list
    ]
    return GraphMachine(
        model=_SentinelModel(),
        states=states,
        initial=states[0],
        transitions=trans_defs,
        graph_engine="mermaid",
        show_conditions=True,
        show_auto_transitions=False,
    )


# Detection patterns for cai-rescue prevention findings (issue #1150).
# Findings carry a fingerprint comment of the form
# ``<!-- fingerprint: rescue-prev-<hex> -->`` written by
# :func:`cai_lib.cmd_rescue._stage_prevention_finding` and emitted
# verbatim by :func:`cai_lib.publish.create_issue` (publish.py ~line
# 579). The fingerprint prefix is the canonical structural signal
# that an issue is a rescue prevention finding rather than a normal
# implementation task; title-prefix matching is intentionally NOT
# used because admins may rename titles on park, but the fingerprint
# stays in the body forever.
_PREVENTION_FINDING_FINGERPRINT_PREFIX = "<!-- fingerprint: rescue-prev-"
_NO_STRUCTURAL_PREVENTION_PHRASE = "No structural prevention needed"


def _is_rescue_prevention_finding(body: str) -> bool:
    """True when *body* carries the canonical ``rescue-prev-``
    fingerprint comment written by ``cai_lib.publish.create_issue``."""
    return _PREVENTION_FINDING_FINGERPRINT_PREFIX in (body or "")


def _has_no_structural_prevention(body: str) -> bool:
    """True when *body* contains the literal phrase
    ``No structural prevention needed`` (case-insensitive). Used by
    :func:`backfill_silent_human_needed_comments` to auto-close
    prevention findings whose recommendation is explicitly "no code
    change required" — they would otherwise be parked indefinitely."""
    return _NO_STRUCTURAL_PREVENTION_PHRASE.lower() in (body or "").lower()


def backfill_silent_human_needed_comments(
    *,
    gh_json=None,
    post_issue_comment=None,
    post_pr_comment=None,
    close_issue=None,
    log_prefix: str = "cai cycle",
) -> list[tuple[str, int]]:
    """Scan open issues/PRs parked at HUMAN_NEEDED / PR_HUMAN_NEEDED and
    either post a retroactive MARKER-bearing backfill comment **or** —
    for cai-rescue prevention findings whose body says "No structural
    prevention needed" (issue #1150) — auto-close the issue as
    ``not planned`` so it stops cycling through the rescue agent on
    every tick.

    For non-auto-close paths, the generated comment now includes a
    suggested-action paragraph tuned to the issue type (prevention
    finding vs. implementation task), so admins triaging the queue
    see a concrete next step rather than a generic "review and
    signal".

    Self-healing counterpart to the fire_trigger divert-reason
    invariant added for issue #1009. The invariant guarantees *future*
    diverts carry a MARKER comment; the backfill sweep closes the gap
    for issues parked before the fix (e.g. #932) so the audit agent's
    ``human_needed_reason_missing`` finder and ``cai unblock`` have
    context on pre-existing silent diverts. Returns the list of
    ``(kind, number)`` tuples that were *handled* — either backfilled
    via comment OR auto-closed. The caller is responsible for logging
    the returned count; per-item lines are emitted inside this
    function so the two paths remain distinguishable in the log.

    All dependencies (``gh_json``, the two comment posters, and
    ``close_issue``) are injectable for tests; defaults read from
    :mod:`cai_lib.github`.
    """
    MARKER = "🙋 Human attention needed"
    from cai_lib.config import LABEL_HUMAN_NEEDED, LABEL_PR_HUMAN_NEEDED, REPO

    if gh_json is None:
        from cai_lib.github import _gh_json as gh_json
    if post_issue_comment is None:
        from cai_lib.github import _post_issue_comment as post_issue_comment
    if post_pr_comment is None:
        from cai_lib.github import _post_pr_comment as post_pr_comment
    if close_issue is None:
        from cai_lib.github import close_issue_not_planned as close_issue

    handled: list[tuple[str, int]] = []
    checks = [
        ("issue", LABEL_HUMAN_NEEDED, post_issue_comment),
        ("pr", LABEL_PR_HUMAN_NEEDED, post_pr_comment),
    ]
    for kind, label, poster in checks:
        try:
            items = gh_json([
                "issue", "list",
                "--repo", REPO,
                "--label", label,
                "--state", "open",
                "--json", "number,title,body,labels,comments",
                "--limit", "100",
            ]) or []
        except Exception as exc:
            print(
                f"[{log_prefix}] backfill: gh issue list --label {label} failed: {exc}",
                file=sys.stderr,
            )
            continue
        for it in items:
            number = it.get("number")
            if number is None:
                continue
            comments = it.get("comments") or []
            if any(MARKER in (c.get("body") or "") for c in comments):
                continue

            issue_body = it.get("body") or ""
            is_prevention = (
                kind == "issue"
                and _is_rescue_prevention_finding(issue_body)
            )

            # Auto-close prevention findings explicitly tagged
            # "No structural prevention needed" — they have no
            # remediation work and only burn rescue cycles when parked.
            if is_prevention and _has_no_structural_prevention(issue_body):
                close_body = (
                    f"**{MARKER}**\n\n"
                    f"This rescue prevention finding states **No "
                    f"structural prevention needed**, so there is no "
                    f"code change to perform. Closing as `not planned` "
                    f"to stop the rescue agent from re-attempting it "
                    f"on every cycle. The recommendation in the body "
                    f"is preserved for the audit trail; reopen if a "
                    f"structural prevention is identified later.\n"
                    f"\n"
                    f"_Auto-closed by `cai cycle` self-heal "
                    f"(issue #1150)._"
                )
                try:
                    close_issue(number, close_body, log_prefix=log_prefix)
                    handled.append((kind, number))
                    print(
                        f"[{log_prefix}] auto-closed prevention finding "
                        f"with no structural remediation on "
                        f"{kind} #{number}",
                        flush=True,
                    )
                except Exception as exc:
                    print(
                        f"[{log_prefix}] auto-close failed for {kind} "
                        f"#{number}: {exc}",
                        file=sys.stderr,
                    )
                continue

            # Choose a per-type suggested-action paragraph.
            if is_prevention:
                suggested_action = (
                    "Review the **rescue prevention finding** above "
                    "and choose one:\n"
                    "- adopt the recommendation (open a follow-up "
                    "issue or implement the change), then **close "
                    "this issue as completed**, or\n"
                    "- dismiss the recommendation as not actionable "
                    "and **close this issue as not planned**, or\n"
                    "- apply the `human:solved` label after leaving a "
                    "comment to signal further action and have the "
                    "FSM resume."
                )
            else:
                suggested_action = (
                    "Review the issue/PR body and recent logs to "
                    "decide next steps. Apply the `human:solved` "
                    "label after leaving a comment to signal the "
                    "divert is resolved and have the FSM resume."
                )

            body = (
                f"**{MARKER}**\n\n"
                f"Automation paused `(unknown)` — this {kind} was parked "
                f"at `{label}` without a divert-reason comment. The "
                f"original divert pre-dates the #1009 invariant, so the "
                f"failing transition and confidence values cannot be "
                f"recovered from the code path.\n\n"
                f"- Required confidence: `(unknown)`\n"
                f"- Reported confidence: `(unknown)`\n"
                f"\n"
                f"{suggested_action}\n"
                f"\n"
                f"_Retroactively posted by `cai cycle` self-heal "
                f"(issue #1009)._"
            )
            try:
                poster(number, body, log_prefix=log_prefix)
                handled.append((kind, number))
                print(
                    f"[{log_prefix}] backfilled silent divert on "
                    f"{kind} #{number}",
                    flush=True,
                )
            except Exception as exc:
                print(
                    f"[{log_prefix}] backfill failed for {kind} "
                    f"#{number}: {exc}",
                    file=sys.stderr,
                )
    return handled


def render_fsm_mermaid(transitions: list[Transition], title: str = "FSM") -> str:
    """Render *transitions* as a Mermaid stateDiagram-v2 block.

    Backed by :class:`transitions.extensions.GraphMachine` (the
    ``pytransitions/transitions`` library). The raw
    ``get_combined_graph().source`` string is post-processed to:

    * strip the library's ``---\\nState Machine\\n---`` YAML front
      matter (the wrapper page in ``docs/fsm.md`` supplies its own
      title);
    * restore the display form of the confidence-gate labels
      (``[ge_HIGH]`` → ``[≥HIGH]``, ``[caller_gated]`` → ``[caller-gated]``)
      that had to be sanitised to valid Python identifiers for the
      machine's ``conditions`` field.

    The ``title`` parameter is retained for backward compatibility
    with the pre-library signature but is not interpolated — the
    library's default header is stripped and the caller supplies its
    own heading in the surrounding Markdown page.
    """
    machine = _build_mermaid_machine(transitions)
    source = machine.get_combined_graph().source
    source = re.sub(r"^---.*?---\n", "", source, flags=re.DOTALL)
    source = re.sub(r"\[ge_(\w+)\]", lambda m: f"[≥{m.group(1)}]", source)
    source = source.replace("[caller_gated]", "[caller-gated]")
    return source.strip()
