"""FSM transition data and apply helpers for the auto-improve lifecycle."""
from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from typing import Optional, Sequence

from transitions.extensions import GraphMachine

from cai_lib.config import (
    LABEL_RAISED, LABEL_REFINING, LABEL_REFINED, LABEL_PLANNING,
    LABEL_PLANNED, LABEL_PLAN_APPROVED, LABEL_IN_PROGRESS, LABEL_PR_OPEN,
    LABEL_MERGED, LABEL_SOLVED, LABEL_NEEDS_EXPLORATION, LABEL_HUMAN_NEEDED,
    LABEL_PR_HUMAN_NEEDED, LABEL_TRIAGING, LABEL_APPLYING, LABEL_APPLIED,
    LABEL_PR_REVIEWING_CODE, LABEL_PR_REVISION_PENDING, LABEL_PR_REVIEWING_DOCS,
    LABEL_PR_APPROVED, LABEL_PR_REBASING, LABEL_PR_CI_FAILING,
)
from cai_lib.fsm_states import IssueState, PRState
from cai_lib.fsm_confidence import Confidence


@dataclass
class Transition:
    name: str
    from_state: IssueState | PRState
    to_state: IssueState | PRState
    labels_add: list[str] = field(default_factory=list)
    labels_remove: list[str] = field(default_factory=list)
    min_confidence: Optional[Confidence] = Confidence.HIGH
    human_label_if_below: str = LABEL_HUMAN_NEEDED

    def accepts(self, confidence: Optional[Confidence]) -> bool:
        if self.min_confidence is None:
            return True
        if confidence is None:
            return False
        return confidence >= self.min_confidence


_T = Transition
_I = IssueState
_P = PRState


def _PR(name: str, src: PRState, dst: PRState, add: Sequence[str] = (), remove: Sequence[str] = ()) -> Transition:
    return Transition(name, src, dst, list(add), list(remove), Confidence.HIGH, LABEL_PR_HUMAN_NEEDED)


ISSUE_TRANSITIONS: list[Transition] = [
    # raised
    _T("raise_to_refining",     _I.RAISED,   _I.REFINING,          [LABEL_REFINING],          [LABEL_RAISED]),
    _T("raise_to_human",        _I.RAISED,   _I.HUMAN_NEEDED,      [LABEL_HUMAN_NEEDED],       [LABEL_RAISED]),
    _T("raise_to_triaging",     _I.RAISED,   _I.TRIAGING,          [LABEL_TRIAGING],           [LABEL_RAISED]),
    # triaging
    _T("triaging_to_refining",  _I.TRIAGING, _I.REFINING,          [LABEL_REFINING],           [LABEL_TRIAGING]),
    _T("triaging_to_human",     _I.TRIAGING, _I.HUMAN_NEEDED,      [LABEL_HUMAN_NEEDED],       [LABEL_TRIAGING]),
    _T("triaging_to_plan_approved", _I.TRIAGING, _I.PLAN_APPROVED, [LABEL_PLAN_APPROVED],      [LABEL_TRIAGING], min_confidence=None),
    _T("triaging_to_applying",  _I.TRIAGING, _I.APPLYING,          [LABEL_APPLYING],           [LABEL_TRIAGING], min_confidence=None),
    # applying → applied → solved
    _T("applying_to_applied",   _I.APPLYING, _I.APPLIED,           [LABEL_APPLIED],            [LABEL_APPLYING]),
    _T("applying_to_applied_inferred_ops", _I.APPLYING, _I.APPLIED, [LABEL_APPLIED],            [LABEL_APPLYING], Confidence.MEDIUM),  # #986
    _T("applying_to_human",     _I.APPLYING, _I.HUMAN_NEEDED,      [LABEL_HUMAN_NEEDED],       [LABEL_APPLYING]),
    _T("applied_to_solved",     _I.APPLIED,  _I.SOLVED,            [LABEL_SOLVED],             [LABEL_APPLIED]),
    # refining
    _T("refining_to_refined",   _I.REFINING, _I.REFINED,           [LABEL_REFINED],            [LABEL_REFINING]),
    _T("refining_to_exploration", _I.REFINING, _I.NEEDS_EXPLORATION, [LABEL_NEEDS_EXPLORATION], [LABEL_REFINING]),
    _T("refining_to_human",     _I.REFINING, _I.HUMAN_NEEDED,      [LABEL_HUMAN_NEEDED],       [LABEL_REFINING]),
    _T("exploration_to_refining", _I.NEEDS_EXPLORATION, _I.REFINING, [LABEL_REFINING],         [LABEL_NEEDS_EXPLORATION]),
    # planning pipeline
    _T("refined_to_planning",   _I.REFINED,  _I.PLANNING,          [LABEL_PLANNING],           [LABEL_REFINED]),
    _T("planning_to_planned",   _I.PLANNING, _I.PLANNED,           [LABEL_PLANNED],            [LABEL_PLANNING]),
    _T("planning_to_human",     _I.PLANNING, _I.HUMAN_NEEDED,      [LABEL_HUMAN_NEEDED],       [LABEL_PLANNING]),
    _T("planned_to_plan_approved",            _I.PLANNED, _I.PLAN_APPROVED, [LABEL_PLAN_APPROVED], [LABEL_PLANNED]),
    _T("planned_to_plan_approved_mitigated",  _I.PLANNED, _I.PLAN_APPROVED, [LABEL_PLAN_APPROVED], [LABEL_PLANNED], Confidence.MEDIUM),  # #918
    _T("planned_to_plan_approved_docs_only",  _I.PLANNED, _I.PLAN_APPROVED, [LABEL_PLAN_APPROVED], [LABEL_PLANNED], Confidence.MEDIUM),  # #989
    _T("planned_to_plan_approved_approvable", _I.PLANNED, _I.PLAN_APPROVED, [LABEL_PLAN_APPROVED], [LABEL_PLANNED], Confidence.MEDIUM),  # #1008
    _T("planned_to_human",      _I.PLANNED,  _I.HUMAN_NEEDED,      [LABEL_HUMAN_NEEDED],       [LABEL_PLANNED]),
    # implement
    _T("approved_to_in_progress",  _I.PLAN_APPROVED, _I.IN_PROGRESS, [LABEL_IN_PROGRESS],      [LABEL_PLAN_APPROVED]),
    _T("in_progress_to_pr",     _I.IN_PROGRESS, _I.PR,             [LABEL_PR_OPEN],            [LABEL_IN_PROGRESS]),
    _T("in_progress_to_refining", _I.IN_PROGRESS, _I.REFINING,     [LABEL_REFINING],           [LABEL_IN_PROGRESS], min_confidence=None),
    _T("pr_to_merged",          _I.PR,       _I.MERGED,            [LABEL_MERGED],             [LABEL_PR_OPEN]),
    _T("pr_to_refined",         _I.PR,       _I.REFINED,           [LABEL_REFINED],            [LABEL_PR_OPEN]),
    _T("pr_to_human_needed",    _I.PR,       _I.HUMAN_NEEDED,      [LABEL_HUMAN_NEEDED],       [LABEL_PR_OPEN]),
    _T("merged_to_solved",      _I.MERGED,   _I.SOLVED,            [LABEL_SOLVED],             [LABEL_MERGED]),
    # human-needed resume
    _T("human_to_raised",        _I.HUMAN_NEEDED, _I.RAISED,            [LABEL_RAISED],            [LABEL_HUMAN_NEEDED]),
    _T("human_to_refining",      _I.HUMAN_NEEDED, _I.REFINING,          [LABEL_REFINING],          [LABEL_HUMAN_NEEDED]),
    _T("human_to_plan_approved", _I.HUMAN_NEEDED, _I.PLAN_APPROVED,    [LABEL_PLAN_APPROVED],     [LABEL_HUMAN_NEEDED]),
    _T("human_to_exploration",   _I.HUMAN_NEEDED, _I.NEEDS_EXPLORATION, [LABEL_NEEDS_EXPLORATION], [LABEL_HUMAN_NEEDED]),
    _T("human_to_solved",        _I.HUMAN_NEEDED, _I.SOLVED,            [LABEL_SOLVED],            [LABEL_HUMAN_NEEDED]),
]

PR_TRANSITIONS: list[Transition] = [
    _PR("open_to_reviewing_code",              _P.OPEN,             _P.REVIEWING_CODE,     [LABEL_PR_REVIEWING_CODE]),
    _PR("reviewing_code_to_revision_pending",  _P.REVIEWING_CODE,   _P.REVISION_PENDING,   [LABEL_PR_REVISION_PENDING],  [LABEL_PR_REVIEWING_CODE]),
    _PR("reviewing_code_to_reviewing_docs",    _P.REVIEWING_CODE,   _P.REVIEWING_DOCS,     [LABEL_PR_REVIEWING_DOCS],    [LABEL_PR_REVIEWING_CODE]),
    _PR("revision_pending_to_reviewing_code",  _P.REVISION_PENDING, _P.REVIEWING_CODE,     [LABEL_PR_REVIEWING_CODE],    [LABEL_PR_REVISION_PENDING]),
    _PR("reviewing_docs_to_reviewing_code",    _P.REVIEWING_DOCS,   _P.REVIEWING_CODE,     [LABEL_PR_REVIEWING_CODE],    [LABEL_PR_REVIEWING_DOCS]),
    _PR("reviewing_docs_to_approved",          _P.REVIEWING_DOCS,   _P.APPROVED,           [LABEL_PR_APPROVED],          [LABEL_PR_REVIEWING_DOCS]),
    _PR("approved_to_merged",                  _P.APPROVED,         _P.MERGED,             [],                            [LABEL_PR_APPROVED]),
    _PR("approved_to_reviewing_code",          _P.APPROVED,         _P.REVIEWING_CODE,     [LABEL_PR_REVIEWING_CODE],    [LABEL_PR_APPROVED]),
    # CI failing
    _PR("reviewing_code_to_ci_failing",        _P.REVIEWING_CODE,   _P.CI_FAILING,         [LABEL_PR_CI_FAILING],        [LABEL_PR_REVIEWING_CODE]),
    _PR("revision_pending_to_ci_failing",      _P.REVISION_PENDING, _P.CI_FAILING,         [LABEL_PR_CI_FAILING],        [LABEL_PR_REVISION_PENDING]),
    _PR("reviewing_docs_to_ci_failing",        _P.REVIEWING_DOCS,   _P.CI_FAILING,         [LABEL_PR_CI_FAILING],        [LABEL_PR_REVIEWING_DOCS]),
    _PR("approved_to_ci_failing",              _P.APPROVED,         _P.CI_FAILING,         [LABEL_PR_CI_FAILING],        [LABEL_PR_APPROVED]),
    _PR("ci_failing_to_reviewing_code",        _P.CI_FAILING,       _P.REVIEWING_CODE,     [LABEL_PR_REVIEWING_CODE],    [LABEL_PR_CI_FAILING]),
    # rebasing
    _PR("reviewing_code_to_rebasing",          _P.REVIEWING_CODE,   _P.REBASING,           [LABEL_PR_REBASING],          [LABEL_PR_REVIEWING_CODE]),
    _PR("revision_pending_to_rebasing",        _P.REVISION_PENDING, _P.REBASING,           [LABEL_PR_REBASING],          [LABEL_PR_REVISION_PENDING]),
    _PR("reviewing_docs_to_rebasing",          _P.REVIEWING_DOCS,   _P.REBASING,           [LABEL_PR_REBASING],          [LABEL_PR_REVIEWING_DOCS]),
    _PR("approved_to_rebasing",                _P.APPROVED,         _P.REBASING,           [LABEL_PR_REBASING],          [LABEL_PR_APPROVED]),
    _PR("ci_failing_to_rebasing",              _P.CI_FAILING,       _P.REBASING,           [LABEL_PR_REBASING],          [LABEL_PR_CI_FAILING]),
    _PR("rebasing_to_reviewing_code",          _P.REBASING,         _P.REVIEWING_CODE,     [LABEL_PR_REVIEWING_CODE],    [LABEL_PR_REBASING]),
    # human-needed
    _PR("reviewing_code_to_human",             _P.REVIEWING_CODE,   _P.PR_HUMAN_NEEDED,    [LABEL_PR_HUMAN_NEEDED],      [LABEL_PR_REVIEWING_CODE]),
    _PR("approved_to_human",                   _P.APPROVED,         _P.PR_HUMAN_NEEDED,    [LABEL_PR_HUMAN_NEEDED],      [LABEL_PR_APPROVED]),
    _PR("approved_to_revision_pending",        _P.APPROVED,         _P.REVISION_PENDING,   [LABEL_PR_REVISION_PENDING],  [LABEL_PR_APPROVED]),
    _PR("pr_human_to_reviewing_code",          _P.PR_HUMAN_NEEDED,  _P.REVIEWING_CODE,     [LABEL_PR_REVIEWING_CODE],    [LABEL_PR_HUMAN_NEEDED]),
    _PR("pr_human_to_revision_pending",        _P.PR_HUMAN_NEEDED,  _P.REVISION_PENDING,   [LABEL_PR_REVISION_PENDING],  [LABEL_PR_HUMAN_NEEDED]),
    _PR("pr_human_to_reviewing_docs",          _P.PR_HUMAN_NEEDED,  _P.REVIEWING_DOCS,     [LABEL_PR_REVIEWING_DOCS],    [LABEL_PR_HUMAN_NEEDED]),
    _PR("pr_human_to_approved",                _P.PR_HUMAN_NEEDED,  _P.APPROVED,           [LABEL_PR_APPROVED],          [LABEL_PR_HUMAN_NEEDED]),
]


def get_issue_state(labels: list[str]) -> Optional[IssueState]:
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
    if pr.get("mergedAt") or pr.get("state") == "MERGED":
        return PRState.MERGED
    labels_raw = pr.get("labels", [])
    label_set = {(lbl.get("name") if isinstance(lbl, dict) else lbl) for lbl in labels_raw}
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


def _apply_named(
    entity_number: int, transition: Transition, *,
    current: object = None, extra_remove: Sequence[str] = (),
    log_prefix: str = "cai", set_labels_fn,
) -> bool:
    if current is not None and current != transition.from_state:
        print(f"[{log_prefix}] refusing {transition.name!r} on #{entity_number}: "
              f"state {current} ≠ {transition.from_state}", file=sys.stderr)
        return False
    return set_labels_fn(entity_number, add=list(transition.labels_add),
                         remove=list(transition.labels_remove) + list(extra_remove),
                         log_prefix=log_prefix)


def _render_human_divert_reason(
    *, transition_name: str, transition: Transition,
    confidence: Optional[Confidence], extra: str = "",
) -> str:
    conf_name = confidence.name if confidence is not None else "MISSING"
    lines = [
        "**🙋 Human attention needed**", "",
        f"Automation paused `{transition_name}` because the confidence gate was not met.", "",
        f"- Required confidence: `{transition.min_confidence.name}`",
        f"- Reported confidence: `{conf_name}`",
    ]
    if extra:
        lines.extend(["", extra.rstrip()])
    lines.extend(["", "Apply the `human:solved` label after leaving a comment to signal "
                  "the divert is resolved and have the FSM resume."])
    return "\n".join(lines)


def _apply_named_with_confidence(
    entity_number: int, transition: Transition, confidence: Optional[Confidence], *,
    current: object = None, extra_remove: Sequence[str] = (), log_prefix: str = "cai",
    set_labels_fn, post_comment_fn, reason_extra: str = "",
) -> tuple[bool, bool]:
    if transition.accepts(confidence):
        return _apply_named(entity_number, transition, current=current,
                            extra_remove=extra_remove, log_prefix=log_prefix,
                            set_labels_fn=set_labels_fn), False
    if current is not None and current != transition.from_state:
        print(f"[{log_prefix}] refusing divert {transition.name!r} on #{entity_number}: "
              f"state {current} ≠ {transition.from_state}", file=sys.stderr)
        return False, False
    conf_name = confidence.name if confidence is not None else "MISSING"
    print(f"[{log_prefix}] diverting {transition.name!r} on #{entity_number} to "
          f"{transition.human_label_if_below} (confidence={conf_name}, "
          f"required={transition.min_confidence.name})", flush=True)
    ok = set_labels_fn(entity_number, add=[transition.human_label_if_below],
                       remove=list(transition.labels_remove) + list(extra_remove),
                       log_prefix=log_prefix)
    if ok:
        post_comment_fn(entity_number, _render_human_divert_reason(
            transition_name=transition.name, transition=transition,
            confidence=confidence, extra=reason_extra), log_prefix=log_prefix)
    return ok, True


def apply_transition(
    issue_number: int, transition_name: str, *,
    current_labels: Optional[list[str]] = None, extra_remove: Sequence[str] = (),
    log_prefix: str = "cai", set_labels=None, divert_reason: Optional[str] = None,
    post_comment=None,
) -> bool:
    """Apply a named issue FSM transition via ``_set_labels``.

    **HUMAN_NEEDED invariant (#1009).** When the transition target is
    :attr:`IssueState.HUMAN_NEEDED`, the caller MUST pass a non-empty
    *divert_reason*. On success, a MARKER-bearing comment rendered
    by :func:`_render_human_divert_reason` is posted on the issue,
    guaranteeing every park at ``:human-needed`` has an audit trail.
    Silent diverts (reason missing) are refused with a log and return False.
    *post_comment* is injectable for tests; defaults to
    ``cai_lib.github._post_issue_comment``.
    """
    t = find_transition(transition_name, ISSUE_TRANSITIONS)

    if t.to_state == IssueState.HUMAN_NEEDED and not (
        divert_reason and divert_reason.strip()
    ):
        print(
            f"[{log_prefix}] refusing silent HUMAN_NEEDED divert "
            f"{transition_name!r} on #{issue_number}: caller must pass "
            f"a non-empty divert_reason so the divert-reason comment "
            f"can be posted (see cai_lib.fsm_transitions invariant)",
            file=sys.stderr,
        )
        return False

    if set_labels is None:
        from cai_lib.github import _set_labels as set_labels
    current = get_issue_state(current_labels) if current_labels is not None else None
    ok = _apply_named(issue_number, t, current=current, extra_remove=extra_remove,
                      log_prefix=log_prefix, set_labels_fn=set_labels)

    if ok and t.to_state == IssueState.HUMAN_NEEDED:
        if post_comment is None:
            from cai_lib.github import _post_issue_comment as post_comment
        post_comment(
            issue_number,
            _render_human_divert_reason(
                transition_name=transition_name,
                transition=t,
                confidence=None,
                extra=divert_reason or "",
            ),
            log_prefix=log_prefix,
        )
    return ok


def apply_transition_with_confidence(
    issue_number: int, transition_name: str, confidence: Optional[Confidence], *,
    current_labels: Optional[list[str]] = None, extra_remove: Sequence[str] = (),
    log_prefix: str = "cai", set_labels=None, post_comment=None, reason_extra: str = "",
) -> tuple[bool, bool]:
    """Apply an issue FSM transition gated on *confidence*; returns (ok, diverted)."""
    t = find_transition(transition_name, ISSUE_TRANSITIONS)
    if set_labels is None:
        from cai_lib.github import _set_labels as set_labels
    if post_comment is None:
        from cai_lib.github import _post_issue_comment as post_comment
    current = get_issue_state(current_labels) if current_labels is not None else None
    return _apply_named_with_confidence(
        issue_number, t, confidence, current=current, extra_remove=extra_remove,
        log_prefix=log_prefix, set_labels_fn=set_labels, post_comment_fn=post_comment,
        reason_extra=reason_extra)


def resume_transition_for(target_state_name: str) -> Optional[Transition]:
    """Map a ResumeTo token to the matching human_to_<state> issue transition."""
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
    pr_number: int, transition_name: str, *,
    current_pr: Optional[dict] = None, log_prefix: str = "cai", set_pr_labels=None,
    divert_reason: Optional[str] = None, post_comment=None,
) -> bool:
    """Apply a named PR FSM transition via ``_set_pr_labels``.

    **PR_HUMAN_NEEDED invariant (#1009).** When the transition target is
    :attr:`PRState.PR_HUMAN_NEEDED`, the caller MUST pass a non-empty
    *divert_reason*. On success, a MARKER-bearing comment rendered
    by :func:`_render_human_divert_reason` is posted on the PR,
    guaranteeing every park at ``:pr-human-needed`` has an audit trail.
    Silent diverts (reason missing) are refused with a log and return False.
    *post_comment* is injectable for tests; defaults to
    ``cai_lib.github._post_pr_comment``.
    """
    t = find_transition(transition_name, PR_TRANSITIONS)

    if t.to_state == PRState.PR_HUMAN_NEEDED and not (
        divert_reason and divert_reason.strip()
    ):
        print(
            f"[{log_prefix}] refusing silent PR_HUMAN_NEEDED divert "
            f"{transition_name!r} on #{pr_number}: caller must pass "
            f"a non-empty divert_reason so the divert-reason comment "
            f"can be posted (see cai_lib.fsm_transitions invariant)",
            file=sys.stderr,
        )
        return False

    if set_pr_labels is None:
        from cai_lib.github import _set_pr_labels as set_pr_labels
    current = get_pr_state(current_pr) if current_pr is not None else None
    ok = _apply_named(pr_number, t, current=current, log_prefix=log_prefix,
                      set_labels_fn=set_pr_labels)

    if ok and t.to_state == PRState.PR_HUMAN_NEEDED:
        if post_comment is None:
            from cai_lib.github import _post_pr_comment as post_comment
        post_comment(
            pr_number,
            _render_human_divert_reason(
                transition_name=transition_name,
                transition=t,
                confidence=None,
                extra=divert_reason or "",
            ),
            log_prefix=log_prefix,
        )
    return ok


def apply_pr_transition_with_confidence(
    pr_number: int, transition_name: str, confidence: Optional[Confidence], *,
    current_pr: Optional[dict] = None, log_prefix: str = "cai",
    set_pr_labels=None, post_comment=None, reason_extra: str = "",
) -> tuple[bool, bool]:
    """Confidence-gated PR transition; mirrors apply_transition_with_confidence."""
    t = find_transition(transition_name, PR_TRANSITIONS)
    if set_pr_labels is None:
        from cai_lib.github import _set_pr_labels as set_pr_labels
    if post_comment is None:
        from cai_lib.github import _post_pr_comment as post_comment
    current = get_pr_state(current_pr) if current_pr is not None else None
    return _apply_named_with_confidence(
        pr_number, t, confidence, current=current, log_prefix=log_prefix,
        set_labels_fn=set_pr_labels, post_comment_fn=post_comment,
        reason_extra=reason_extra)


def resume_pr_transition_for(target_state_name: str) -> Optional[Transition]:
    """Map a ResumeTo token to the matching pr_human_to_<state> PR transition."""
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
    pass

def _build_mermaid_machine(transitions_list: list[Transition]) -> GraphMachine:
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
                f"ge_{t.min_confidence.name}" if t.min_confidence is not None else "caller_gated"
            ),
        }
        for t in transitions_list
    ]
    return GraphMachine(model=_SentinelModel(), states=states, initial=states[0],
                        transitions=trans_defs, graph_engine="mermaid",
                        show_conditions=True, show_auto_transitions=False)


def backfill_silent_human_needed_comments(
    *,
    gh_json=None,
    post_issue_comment=None,
    post_pr_comment=None,
    log_prefix: str = "cai cycle",
) -> list[tuple[str, int]]:
    """Scan open issues/PRs parked at HUMAN_NEEDED / PR_HUMAN_NEEDED and
    post a retroactive MARKER-bearing backfill comment on any entry that
    has no MARKER comment in its history.

    This is the self-healing counterpart to the ``apply_transition``
    invariant added for issue #1009. The invariant guarantees *future*
    diverts carry a MARKER comment; the backfill sweep closes the gap
    for issues parked before the fix (e.g. #932) so the audit agent's
    ``human_needed_reason_missing`` finder and ``cai unblock`` have
    context on pre-existing silent diverts. Returns the list of
    ``(kind, number)`` tuples that were backfilled (empty when nothing
    was missing). The caller is responsible for logging the result.

    All dependencies (``gh_json``, the two comment posters) are
    injectable for tests; defaults read from ``cai_lib.github``.
    """
    MARKER = "🙋 Human attention needed"
    from cai_lib.config import LABEL_HUMAN_NEEDED, LABEL_PR_HUMAN_NEEDED, REPO

    if gh_json is None:
        from cai_lib.github import _gh_json as gh_json
    if post_issue_comment is None:
        from cai_lib.github import _post_issue_comment as post_issue_comment
    if post_pr_comment is None:
        from cai_lib.github import _post_pr_comment as post_pr_comment

    backfilled: list[tuple[str, int]] = []
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
                "--json", "number,labels,comments",
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
                f"Review the issue/PR body and recent logs to decide "
                f"next steps. Apply the `human:solved` label after "
                f"leaving a comment to signal the divert is resolved "
                f"and have the FSM resume.\n"
                f"\n"
                f"_Retroactively posted by `cai cycle` self-heal "
                f"(issue #1009)._"
            )
            try:
                poster(number, body, log_prefix=log_prefix)
                backfilled.append((kind, number))
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
    return backfilled


def render_fsm_mermaid(transitions: list[Transition], title: str = "FSM") -> str:
    """Render *transitions* as a Mermaid stateDiagram-v2 block via GraphMachine."""
    machine = _build_mermaid_machine(transitions)
    source = machine.get_combined_graph().source
    source = re.sub(r"^---.*?---\n", "", source, flags=re.DOTALL)
    source = re.sub(r"\[ge_(\w+)\]", lambda m: f"[≥{m.group(1)}]", source)
    source = source.replace("[caller_gated]", "[caller-gated]")
    return source.strip()
