"""FSM state enums for the auto-improve lifecycle.

Defines :class:`IssueState` and :class:`PRState` — the two enum classes that
represent the explicit states in the auto-improve pipeline. Transition data
lives in :mod:`cai_lib.fsm_transitions`.
"""
from __future__ import annotations

from enum import Enum

from cai_lib.config import (
    LABEL_RAISED, LABEL_REFINING, LABEL_REFINED, LABEL_SPLITTING,
    LABEL_PLANNING, LABEL_PLANNED, LABEL_PLAN_APPROVED,
    LABEL_IN_PROGRESS, LABEL_PR_OPEN, LABEL_MERGED, LABEL_SOLVED,
    LABEL_NEEDS_EXPLORATION, LABEL_HUMAN_NEEDED, LABEL_PR_HUMAN_NEEDED,
    LABEL_TRIAGING, LABEL_APPLYING, LABEL_APPLIED,
    LABEL_PR_REVIEWING_CODE, LABEL_PR_REVISION_PENDING,
    LABEL_PR_REVIEWING_DOCS, LABEL_PR_APPROVED, LABEL_PR_REBASING,
    LABEL_PR_CI_FAILING,
)


class IssueState(str, Enum):
    RAISED            = LABEL_RAISED
    TRIAGING          = LABEL_TRIAGING     # cai-triage is actively running
    APPLYING          = LABEL_APPLYING     # cai-maintain is actively applying ops
    APPLIED           = LABEL_APPLIED      # ops applied; awaiting verification
    REFINING          = LABEL_REFINING     # cai-refine is actively running
    REFINED           = LABEL_REFINED      # refine done, awaiting split pickup
    SPLITTING         = LABEL_SPLITTING    # cai-split is actively running (scope evaluation)
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
