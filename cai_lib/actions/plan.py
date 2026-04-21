"""cai_lib.actions.plan — handlers for the planning phase of the FSM.

Two handlers:

- :func:`handle_plan` covers :class:`IssueState.REFINED` (entry) and
  :class:`IssueState.PLANNING` (resume). It runs the serial 2-plan →
  select pipeline and stores the chosen plan on the issue body, then
  transitions to :class:`IssueState.PLANNED`.
- :func:`handle_plan_gate` covers :class:`IssueState.PLANNED` and
  auto-advances the issue to ``PLAN_APPROVED`` via the confidence gate
  (below-threshold diverts to ``:human-needed`` for admin review).

Derived from ``cmd_plan`` in ``cai.py`` — behaviour is preserved as
closely as possible. The dispatcher is responsible for fetching the
issue; direct-invoke ``args.issue`` handling and pickup queries are
intentionally dropped here.
"""
from __future__ import annotations

import re
import shutil
import sys
import time
import uuid

from pathlib import Path

from cai_lib.config import (
    REPO,
    LABEL_IN_PROGRESS,
    LABEL_OPUS_ATTEMPTED,
    LABEL_PR_OPEN,
    LABEL_PLAN_NEEDS_REVIEW,
    LABEL_REFINED,
    LABEL_PLANNING,
    LABEL_PLANNED,
)
from cai_lib.github import (
    _gh_json,
    _build_issue_block,
    _post_issue_comment,
    _set_labels,
)
from cai_lib.subprocess_utils import _run, _run_claude_p
from cai_lib.logging_utils import log_run
from cai_lib.cmd_helpers import (
    _work_directory_block,
    _strip_stored_plan_block,
    _fetch_previous_fix_attempts,
    _build_attempt_history_block,
)
from cai_lib.fsm import (
    fire_trigger,
    IssueState,
    get_issue_state,
)


# ---------------------------------------------------------------------------
# Anchor-based risk-mitigation marker (#918).
#
# A plan that carries the phrase ``locate edits by anchor text ... not
# by line number`` tells the fix agent to Read each target file first
# and anchor edits on unique surrounding text rather than on absolute
# line numbers. Plans whose only residual risk is implementation-detail
# (line-number drift, fence escaping, cosmetic wording) can explicitly
# neutralise that risk with this marker, and :func:`handle_plan_gate`
# routes them through the MEDIUM-threshold
# ``planned_to_plan_approved_mitigated`` transition rather than the
# default HIGH-threshold ``planned_to_plan_approved`` — see
# :mod:`cai_lib.fsm_transitions`.
#
# Matching is case-insensitive and the two halves of the phrase may sit
# on separate lines (``re.DOTALL``).
# ---------------------------------------------------------------------------
_ANCHOR_MITIGATION_RE = re.compile(
    r"locate\s+edits?\s+by\s+anchor\s+text.*?not\s+by\s+line\s+number",
    re.IGNORECASE | re.DOTALL,
)


def _plan_has_anchor_mitigation(plan_text: str | None) -> bool:
    """Return ``True`` when *plan_text* carries the anchor-mitigation marker.

    The marker is the phrase ``locate edits by anchor text ... not by
    line number`` (case-insensitive, may span newlines). Returns
    ``False`` for an empty, ``None``, or non-matching input.

    Used by :func:`handle_plan_gate` to choose between the default
    HIGH-gated ``planned_to_plan_approved`` transition and the
    MEDIUM-gated ``planned_to_plan_approved_mitigated`` transition
    introduced in #918.
    """
    if not plan_text:
        return False
    return bool(_ANCHOR_MITIGATION_RE.search(plan_text))


# ---------------------------------------------------------------------------
# Docs-only structural relaxation (#989).
#
# A plan whose ``### Files to change`` section lists only paths under
# ``docs/`` can safely auto-approve at MEDIUM confidence because:
#
#   1. Blast radius is minimal — no Python, YAML, shell, workflow, or
#      test file is touched;
#   2. ``cai-review-docs`` owns the affected files on subsequent PRs
#      and can correct any residual drift;
#   3. Standard fix-agent behaviour (Grep before Read) already handles
#      stale-symbol references conservatively without requiring an
#      explicit in-plan guard phrase.
#
# :func:`handle_plan_gate` routes qualifying plans through
# ``planned_to_plan_approved_docs_only`` instead of the default
# HIGH-threshold transition — see :mod:`cai_lib.fsm_transitions`.
#
# Detection is deliberately **structural** rather than marker-based:
# the planner already declares its file targets in the canonical
# Files-to-change block, and that declaration is the trusted signal.
# Requiring an additional phrase would force a planner-prompt change
# and create a bypass channel if the phrase ever drifts. Matching the
# block the planner already emits is strictly stronger.
#
# The Files-to-change section must:
#
#   * Exist (parsed with a case-insensitive ``^### Files to change$``
#     header, stopping at the next ``^### `` heading or end-of-body);
#   * Contain at least one backticked ``path/with.ext`` token so a
#     plan with only free-form prose cannot accidentally trip the
#     relaxation;
#   * Have every such path begin with ``docs/`` (strict prefix — any
#     non-``docs/`` path disqualifies the plan).
# ---------------------------------------------------------------------------
_FILES_TO_CHANGE_SECTION_RE = re.compile(
    r"^###\s+Files\s+to\s+change\s*$\n(.*?)(?=^###\s|\Z)",
    re.IGNORECASE | re.DOTALL | re.MULTILINE,
)

# Match backticked path tokens of the form ``path/with.ext`` — requires
# at least one ``/`` and an extension, so free-standing symbol names
# (e.g. ``parse_config``) and extensionless bare names are ignored.
_FILES_TO_CHANGE_PATH_RE = re.compile(
    r"`([^`\s]+/[^`\s]*\.[A-Za-z0-9]+)`"
)


def _plan_targets_only_docs(plan_text: str | None) -> bool:
    """Return ``True`` when every path in *plan_text*'s Files-to-change
    section sits under ``docs/``.

    The section is located by the case-insensitive header
    ``### Files to change`` and bounded by the next ``### `` heading
    or end-of-body. Backticked ``path/with.ext`` tokens are extracted
    from the section body; bare prose bullets without any backticked
    path are ignored so a narrative-only block does not accidentally
    qualify.

    Returns ``False`` for empty or ``None`` input, when the section
    is missing, when the section contains no backticked paths, or
    when any extracted path fails the ``docs/`` prefix test.

    Used by :func:`handle_plan_gate` to route qualifying plans
    through the MEDIUM-threshold ``planned_to_plan_approved_docs_only``
    transition introduced in #989 — purely from the plan's structural
    declaration, without requiring any in-plan guard phrase.
    """
    if not plan_text:
        return False
    section = _FILES_TO_CHANGE_SECTION_RE.search(plan_text)
    if not section:
        return False
    paths = _FILES_TO_CHANGE_PATH_RE.findall(section.group(1))
    if not paths:
        return False
    return all(p.startswith("docs/") for p in paths)


# ---------------------------------------------------------------------------
# Admin-flagged scale / complexity persistence (#1131).
#
# Complements the cai-select ``requires_human_review=true`` flag
# (#982) with a deterministic Python-side auto-flag. The select
# agent's flag only fires when the selected plan knowingly diverges
# from an explicit refined-issue preference; it does NOT fire for
# the "plan is simply too large to auto-approve" case that motivates
# #1131 (issue #1124 took 22 files at LOW confidence — 4-hourly
# `cai rescue` passes kept burning a fresh autonomous-resume attempt
# each tick instead of respecting the earlier admin flag).
#
# ``handle_plan_gate`` therefore also promotes the human-review
# divert on two Python-detected signals at LOW / MISSING confidence:
#
#   (a) Large-scope cap — the issue body's first ``### Files to
#       change`` section lists >= ``_LARGE_SCOPE_FILE_THRESHOLD``
#       unique backticked ``path/with.ext`` tokens. 15 matches the
#       empirical scale of #1124 with headroom for smaller but
#       still-broad reworks. (In practice the first such section
#       in a PLANNED issue body is the refined-issue scope list;
#       the plan block appended below inside ``<!-- cai-plan-start
#       --> / <!-- cai-plan-end -->`` markers contains its own
#       section that ``re.search`` does NOT see first, so the
#       signal is effectively 'declared refined-issue scale'.)
#
#   (b) Sticky scale/complexity phrase — any earlier MARKER divert
#       comment on this issue (``🙋 Human attention needed``) carries
#       one of the ``_SCALE_COMPLEXITY_PHRASES`` tokens. Makes the
#       concern persist across subsequent plan iterations without
#       requiring cai-select to rediscover it from scratch.
#
# Both signals route through the same divert branch as the #982
# flag: ``fire_trigger("planned_to_human", divert_reason=...)`` + a
# follow-up ``_set_labels(add=[LABEL_PLAN_NEEDS_REVIEW])`` so
# ``cai rescue``'s ``_list_unresolved_human_needed_issues`` skips
# the issue until an admin explicitly resumes it via
# ``cai unblock`` or ``human:solved``. HIGH- and MEDIUM-confidence
# plans are unaffected — the triggers never fire above LOW,
# preserving the existing fast path for trusted plans.
# ---------------------------------------------------------------------------
_LARGE_SCOPE_FILE_THRESHOLD = 15

_SCALE_COMPLEXITY_PHRASES = (
    "scale alone warrants",
    "warrants admin review",
    "complexity warrants",
    "too large to approve autonomously",
    "warranting review beyond what",
    "scale/complexity",
)

_HUMAN_REVIEW_MARKER_PHRASE = "🙋 Human attention needed"


def _count_files_to_change(issue_body):
    """Return the count of unique backticked path tokens in *issue_body*'s
    first ``### Files to change`` section.

    Uses the same regexes as :func:`_plan_targets_only_docs`
    (``_FILES_TO_CHANGE_SECTION_RE`` bounds the section; only
    backticked ``path/with.ext`` tokens with at least one ``/`` and
    an extension count). Returns ``0`` when the section is missing,
    contains no backticked paths, or the input is empty / ``None``.
    """
    if not issue_body:
        return 0
    section = _FILES_TO_CHANGE_SECTION_RE.search(issue_body)
    if not section:
        return 0
    paths = set(_FILES_TO_CHANGE_PATH_RE.findall(section.group(1)))
    return len(paths)


def _prior_divert_cites_scale_complexity(comments):
    """Return ``True`` if any MARKER comment on the issue cites a
    scale/complexity phrase from ``_SCALE_COMPLEXITY_PHRASES``.

    Matches case-insensitively and only against comment bodies that
    also carry the ``🙋 Human attention needed`` marker, so an
    unrelated admin comment that happens to contain one of the
    phrases cannot trip the sticky signal. Returns ``False`` for
    empty or ``None`` input.
    """
    if not comments:
        return False
    marker_lc = _HUMAN_REVIEW_MARKER_PHRASE.lower()
    for c in comments:
        body = (c.get("body") or "").lower()
        if marker_lc not in body:
            continue
        if any(phrase in body for phrase in _SCALE_COMPLEXITY_PHRASES):
            return True
    return False


def _auto_flagged_human_review_reason(issue, plan_confidence):
    """Return a bespoke divert reason string when #1131's auto-flag
    triggers fire, else ``None``.

    Fires only when *plan_confidence* is ``Confidence.LOW`` or
    ``None`` (MISSING). HIGH- and MEDIUM-confidence plans continue
    through the ordinary confidence gate unaffected.

    Fires when either:

      (a) ``_count_files_to_change(issue["body"])`` >=
          ``_LARGE_SCOPE_FILE_THRESHOLD``; or
      (b) ``_prior_divert_cites_scale_complexity(issue["comments"])``
          is ``True``.

    The returned reason will be forwarded to
    ``fire_trigger("planned_to_human", divert_reason=...)`` and
    logged alongside the ``LABEL_PLAN_NEEDS_REVIEW`` application
    in :func:`handle_plan_gate`.
    """
    from cai_lib.fsm import Confidence
    if plan_confidence is not None and plan_confidence != Confidence.LOW:
        return None
    file_count = _count_files_to_change(issue.get("body", "") or "")
    prior_scale = _prior_divert_cites_scale_complexity(
        issue.get("comments") or []
    )
    if file_count < _LARGE_SCOPE_FILE_THRESHOLD and not prior_scale:
        return None
    signals = []
    if file_count >= _LARGE_SCOPE_FILE_THRESHOLD:
        signals.append(
            f"the stored plan lists {file_count} files to change "
            f"(\u2265 {_LARGE_SCOPE_FILE_THRESHOLD})"
        )
    if prior_scale:
        signals.append(
            "a prior divert on this issue flagged scale or complexity "
            "as warranting admin review"
        )
    joined = "; ".join(signals)
    return (
        "Auto-flagged scale/complexity checkpoint (#1131): "
        f"{joined}. Admin approval is required at each plan "
        "iteration; while `auto-improve:plan-needs-review` is "
        "applied, `cai rescue`'s autonomous resume pass will skip "
        "this issue."
    )


# ---------------------------------------------------------------------------
# Pre-emptive Opus-tier escalation for large mechanical refactors (#1139).
#
# A plan whose ``### Files to change`` section lists at least
# ``_LARGE_REFACTOR_FILE_THRESHOLD`` unique backticked path tokens AND
# whose body contains at least ``_LARGE_REFACTOR_EDIT_SITE_THRESHOLD``
# ``#### Step N — Edit/Write`` headers is treated as a large mechanical
# refactor. On successful ``planned_to_plan_approved*`` transition,
# :func:`handle_plan_gate` stamps :data:`LABEL_OPUS_ATTEMPTED` on the
# issue so :func:`cai_lib.actions.implement.handle_implement` reads
# ``opus_escalation = True`` on the next dispatch tick — skipping the
# Sonnet subagent entirely and running the implementation on Opus from
# the start. This prevents the three-Sonnet-retry loop observed on
# #1136 (the divert that motivated this issue) without requiring any
# handler-side changes: ``handle_implement`` already treats
# ``LABEL_OPUS_ATTEMPTED`` as the Opus-tier signal, and ``cai rescue``'s
# ``_issue_has_opus_attempted`` guard correctly refuses a second
# escalation if Opus also fails.
#
# Detection is deliberately **structural** — counting the planner's
# canonical ``### Files to change`` declarations and its ``#### Step N
# — Edit/Write`` step headers — matching the "Prefer structural
# detection over marker phrases" guidance in the shared-memory
# ``fsm-transition-threshold-relaxation.md`` entry and the
# :func:`_plan_targets_only_docs` pattern established in #989. The
# signal is stored as an FSM label (not a plan-body marker), keeping
# the information visible in the GitHub UI and consumable by the
# existing label-based ``opus_escalation`` path without introducing a
# new ``parse_*`` helper or a new plan-body field.
#
# The label is applied only when the gate has already successfully
# transitioned the issue to ``:plan-approved`` (non-diverted outcome of
# the ``planned_to_plan_approved*`` siblings). Divert paths
# (``planned_to_human`` via confidence gate, requires_human_review,
# or #1131 scale/complexity auto-flag) skip the label so the admin can
# choose the tier when resuming.
# ---------------------------------------------------------------------------
_LARGE_REFACTOR_FILE_THRESHOLD = 8

_LARGE_REFACTOR_EDIT_SITE_THRESHOLD = 50

# Count each ``#### Step N — Edit `<path>` `` / ``#### Step N — Write
# `<path>` `` header once. Accepts em-dash (\u2014), en-dash (\u2013),
# and plain hyphen between ``Step N`` and the verb to match the
# cai-plan template plus real-world drift. The separate
# ``_STEP_HEADER_RE`` in ``cai_lib/actions/implement.py`` captures the
# path (for scope enforcement) and is intentionally not reused here —
# we only need the header count, and a path-capturing regex would
# force an implement.py import from a plan-phase helper.
_STEP_EDIT_HEADER_RE = re.compile(
    r"^####\s+Step\s+\d+\s+[\u2014\u2013\-]\s+(?:Edit|Write)\s+`",
    re.MULTILINE,
)


def _count_edit_steps(plan_text):
    """Return the count of ``#### Step N — Edit/Write`` headers in
    *plan_text*.

    Uses :data:`_STEP_EDIT_HEADER_RE` — one increment per header
    regardless of the target path. Returns ``0`` on empty or ``None``
    input. Non-``Edit``/``Write`` step verbs (e.g. ``Read``, ``Verify``)
    are ignored so the count reflects *edit sites* only.
    """
    if not plan_text:
        return 0
    return len(_STEP_EDIT_HEADER_RE.findall(plan_text))


def _plan_is_large_mechanical_refactor(plan_text):
    """Return ``True`` when *plan_text* qualifies as a large mechanical
    refactor worth pre-emptively routing to the Opus implement tier
    (#1139).

    Both thresholds must be met:

      * ``_count_files_to_change(plan_text) >= _LARGE_REFACTOR_FILE_THRESHOLD``
        — at least 8 unique backticked ``path/with.ext`` tokens in the
        plan's ``### Files to change`` section.
      * ``_count_edit_steps(plan_text) >= _LARGE_REFACTOR_EDIT_SITE_THRESHOLD``
        — at least 50 ``#### Step N — Edit/Write`` headers.

    Returns ``False`` on empty or ``None`` input. Used by
    :func:`handle_plan_gate` to stamp :data:`LABEL_OPUS_ATTEMPTED`
    directly on the issue after a successful ``planned_to_plan_approved*``
    transition — so :func:`cai_lib.actions.implement.handle_implement`
    reads ``opus_escalation = True`` on the next dispatch tick and
    skips the Sonnet attempt entirely.
    """
    if not plan_text:
        return False
    if _count_files_to_change(plan_text) < _LARGE_REFACTOR_FILE_THRESHOLD:
        return False
    if _count_edit_steps(plan_text) < _LARGE_REFACTOR_EDIT_SITE_THRESHOLD:
        return False
    return True


# ---------------------------------------------------------------------------
# Helpers (moved from cai.py — only used by the plan phase).
# ---------------------------------------------------------------------------

def _select_plan_target(issue_number: int | None = None):
    """Return the oldest open :refined issue eligible for planning, or None.

    If *issue_number* is given, fetch that issue directly (validating it is
    open and not locked).  Otherwise query for the oldest :refined issue
    that is not :in-progress or :pr-open.
    """
    import subprocess  # local import — keeps module-level deps tight

    if issue_number is not None:
        try:
            issue = _gh_json([
                "issue", "view", str(issue_number),
                "--repo", REPO,
                "--json", "number,title,body,labels,state,createdAt,comments",
            ])
        except subprocess.CalledProcessError as e:
            print(f"[cai plan] gh issue view #{issue_number} failed:\n{e.stderr}",
                  file=sys.stderr)
            return None
        if issue.get("state", "").upper() != "OPEN":
            print(f"[cai plan] issue #{issue_number} is not open; nothing to do",
                  flush=True)
            return None
        label_names = {lbl["name"] for lbl in issue.get("labels", [])}
        if LABEL_IN_PROGRESS in label_names or LABEL_PR_OPEN in label_names:
            print(f"[cai plan] issue #{issue_number} is locked; skipping",
                  flush=True)
            return None
        return issue

    # Queue-based: oldest :refined issue not locked.
    try:
        candidates = _gh_json([
            "issue", "list",
            "--repo", REPO,
            "--label", LABEL_REFINED,
            "--state", "open",
            "--json", "number,title,body,labels,createdAt,comments",
            "--limit", "100",
        ]) or []
    except subprocess.CalledProcessError as e:
        print(f"[cai plan] gh issue list failed:\n{e.stderr}",
              file=sys.stderr)
        return None
    candidates = [
        c for c in candidates
        if not {lbl["name"] for lbl in c.get("labels", [])}
            & {LABEL_IN_PROGRESS, LABEL_PR_OPEN}
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda c: c.get("createdAt", ""))


def _run_plan_agent(issue: dict, plan_index: int, work_dir: Path, attempt_history_block: str = "", first_plan: str = "") -> str:
    """Run a single cai-plan agent and return its stdout.

    Called serially (2×) by _run_plan_select_pipeline — the second call
    receives the first plan to produce an alternative approach.

    Runs with `cwd=/app` and `--add-dir <work_dir>` so the agent
    reads its definition from the canonical location while
    operating on the clone via absolute paths (#342).
    """
    user_message = (
        _work_directory_block(work_dir)
        + "\n"
        + _build_issue_block(issue)
        + attempt_history_block
    )
    if first_plan:
        user_message += (
            "\n## First Plan (for reference)\n\n"
            "Another planning agent produced the following plan. "
            "Your job is to find an **alternative approach** that solves "
            "the same issue differently. Do NOT repeat the same strategy — "
            "propose a meaningfully different solution.\n\n"
            f"{first_plan}\n"
        )
    result = _run_claude_p(
        ["claude", "-p", "--agent", "cai-plan",
         "--dangerously-skip-permissions",
         "--add-dir", str(work_dir)],
        category="plan.plan",
        agent="cai-plan",
        input=user_message,
        cwd="/app",
    )
    if result.returncode != 0:
        stderr_preview = (result.stderr or "")[:400].rstrip()
        print(
            f"[cai plan] plan agent {plan_index} failed for "
            f"#{issue['number']} (exit {result.returncode})"
            + (f":\n{stderr_preview}" if stderr_preview else ""),
            file=sys.stderr,
            flush=True,
        )
        return f"(Plan {plan_index} failed: exit {result.returncode})"
    return result.stdout or ""


_SELECT_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "plan": {
            "type": "string",
            "description": "Full text of the chosen plan.",
        },
        "confidence": {
            "type": "string",
            "enum": ["HIGH", "MEDIUM", "LOW"],
            "description": "Confidence level for the selected plan.",
        },
        "confidence_reason": {
            "type": "string",
            "description": (
                "1-3 sentences explaining what makes the plan less than HIGH confidence: "
                "unverified assumptions, ambiguous scope, missing edge cases, etc. "
                "Required when confidence is MEDIUM or LOW; for HIGH confidence, "
                "provide a brief statement confirming why the plan is solid."
            ),
        },
        "note": {
            "type": "string",
            "description": "Optional note flagging critical weaknesses for the fix agent.",
        },
        "requires_human_review": {
            "type": "boolean",
            "description": (
                "Set to true ONLY when the selected plan knowingly diverges from an "
                "explicit stated preference in the refined issue (e.g. the refined "
                "issue says 'remove references' and the chosen plan keeps them via a "
                "different mechanism). This forces a :human-needed divert with a "
                "bespoke admin-approval message, independent of confidence. Omit or "
                "set to false for all other cases — routine confidence signals still "
                "flow through the confidence gate."
            ),
        },
        "approvable_at_medium": {
            "type": "boolean",
            "description": (
                "Set to true when the selected plan's reported confidence is MEDIUM "
                "but every residual concern falls into the soft / non-blocking bucket "
                "(line-number-verification-only risks, additive schema fields, "
                "soft length caps exceeded 'in spirit', divergence from a "
                "preferred-but-not-required path in the refined issue, etc.). "
                "When set, the FSM routes the issue through a MEDIUM-threshold "
                "sibling of planned_to_plan_approved so the plan auto-approves "
                "without admin review. Omit or set to false when the MEDIUM "
                "signal reflects genuine substantive uncertainty (ambiguous scope, "
                "unverified structural assumptions, missing edge cases, etc.) that "
                "warrants admin intervention. Ignored when confidence is HIGH "
                "(auto-approves via the default transition) or LOW (always diverts)."
            ),
        },
    },
    "required": ["plan", "confidence", "confidence_reason"],
}


def _run_select_agent(
    issue: dict, plans: list[str], work_dir: Path,
) -> "tuple[str, Confidence, str, bool, bool] | None":
    """Run the cai-select agent and return ``(plan_text, confidence, reason, requires_human_review, approvable_at_medium)``.

    Invokes the Claude Code subagent with ``--json-schema`` so the final
    output is a structured JSON object ({plan, confidence, confidence_reason,
    note?, requires_human_review?, approvable_at_medium?}) rather than
    free-form text. Removes the regex-based confidence extraction that
    previously diverted sound plans to ``:human-needed`` when the model
    drifted from the ``Confidence: …`` trailer format.

    Returns ``None`` on subprocess or parse failure; otherwise returns
    the cleaned plan text (with optional ``> **Note:** …`` blockquote
    prepended), the :class:`~cai_lib.fsm.Confidence` enum member, the
    confidence reason string, a boolean ``requires_human_review``
    flag (defaults to ``False`` when absent from the payload) that
    signals a knowing divergence from the refined-issue's stated
    preference (#982), and a boolean ``approvable_at_medium`` flag
    (defaults to ``False`` when absent) that signals the select agent
    judged the plan's residual risks soft / non-blocking and safe to
    auto-approve at MEDIUM confidence (#1008).
    """
    import json

    from cai_lib.fsm import Confidence

    user_message = _work_directory_block(work_dir) + "\n"
    user_message += _build_issue_block(issue)
    user_message += "\n---\n\n# Candidate Plans\n\n"
    for i, plan in enumerate(plans, 1):
        user_message += f"## Plan {i}\n\n{plan}\n\n---\n\n"

    result = _run_claude_p(
        ["claude", "-p", "--agent", "cai-select",
         "--dangerously-skip-permissions",
         "--json-schema", json.dumps(_SELECT_JSON_SCHEMA),
         "--add-dir", str(work_dir)],
        category="plan.select",
        agent="cai-select",
        input=user_message,
        cwd="/app",
    )
    if result.returncode != 0 or not (result.stdout or "").strip():
        stderr_preview = (result.stderr or "")[:400].rstrip()
        stdout_preview = (result.stdout or "")[:200].rstrip()
        print(
            f"[cai plan] cai-select produced no output for #{issue['number']} "
            f"(exit {result.returncode})"
            + (f"\n  stderr: {stderr_preview}" if stderr_preview else "")
            + (f"\n  stdout: {stdout_preview!r}" if stdout_preview else ""),
            file=sys.stderr,
            flush=True,
        )
        return None

    # Defensive: strip a surrounding ```json ... ``` fence if the model
    # wrapped its schema-validated output in markdown. --json-schema
    # normally prevents this, but fall back gracefully rather than
    # diverting an otherwise-valid plan to :human-needed.
    stdout_raw = (result.stdout or "").strip()
    if stdout_raw.startswith("```"):
        lines = stdout_raw.splitlines()
        if lines[0].startswith("```") and lines[-1].startswith("```"):
            stdout_raw = "\n".join(lines[1:-1]).strip()

    try:
        payload = json.loads(stdout_raw)
    except (json.JSONDecodeError, ValueError) as exc:
        print(
            f"[cai plan] cai-select output was not valid JSON: {exc}; "
            f"stdout starts with: {(result.stdout or '')[:200]!r}",
            file=sys.stderr,
            flush=True,
        )
        return None

    plan_text = payload.get("plan", "") or ""
    confidence_str = (payload.get("confidence") or "").upper()
    confidence_reason = (payload.get("confidence_reason") or "").strip()
    note = payload.get("note", "") or ""
    requires_human_review = bool(payload.get("requires_human_review", False))
    approvable_at_medium = bool(payload.get("approvable_at_medium", False))

    if note:
        plan_text = f"> **Note:** {note}\n\n{plan_text}"

    try:
        confidence = Confidence[confidence_str]
    except KeyError:
        print(
            f"[cai plan] cai-select returned invalid confidence: {confidence_str!r}",
            file=sys.stderr,
        )
        return None

    return (
        plan_text.rstrip() + "\n",
        confidence,
        confidence_reason,
        requires_human_review,
        approvable_at_medium,
    )


def _run_plan_select_pipeline(
    issue: dict, work_dir: Path, attempt_history_block: str = "",
) -> "tuple[str, Confidence | None, str, bool, bool] | None":
    """Run the serial 2-plan → select pipeline.

    Plan 1 runs first; Plan 2 receives Plan 1's output and is asked
    to find an alternative approach. The select agent then picks the
    best and emits a trailing ``Confidence: HIGH|MEDIUM|LOW`` line
    indicating how sure it is that the chosen plan will succeed.

    Returns ``(plan_text, confidence, confidence_reason, requires_human_review, approvable_at_medium)``
    — plan text, confidence, reason, an explicit human-review flag, and
    an explicit approvable-at-medium flag all arrive as separate
    structured fields from the select agent's forced tool-use call.
    ``confidence`` is a :class:`~cai_lib.fsm.Confidence` enum member, or
    ``None`` when the select agent fails (treated as below-threshold by
    the caller). ``requires_human_review`` is ``True`` only when
    cai-select flagged a knowing divergence from the refined-issue's
    stated preference (#982); handle_plan_gate uses it to surface a
    bespoke admin-approval divert message. ``approvable_at_medium`` is
    ``True`` only when cai-select judged the residual risks soft /
    non-blocking and safe to auto-approve at MEDIUM confidence (#1008);
    handle_plan_gate routes the issue through a MEDIUM-threshold
    sibling transition in that case. Returns ``None`` if the pipeline
    fails to produce any output.
    """
    issue_number = issue["number"]

    # Step 1: Run Plan 1.
    print(f"[cai plan] running plan agent 1/2 for #{issue_number}", flush=True)
    plan1 = _run_plan_agent(issue, 1, work_dir, attempt_history_block)
    print(f"[cai plan] plan 1: {len(plan1)} chars", flush=True)

    # Step 2: Run Plan 2 with knowledge of Plan 1, asking for an alternative.
    print(f"[cai plan] running plan agent 2/2 for #{issue_number}", flush=True)
    plan2 = _run_plan_agent(issue, 2, work_dir, attempt_history_block, first_plan=plan1)
    print(f"[cai plan] plan 2: {len(plan2)} chars", flush=True)

    plans = [plan1, plan2]

    # Step 3: Run the select agent to pick the best plan.
    print(f"[cai plan] running select agent for #{issue_number}", flush=True)
    select_result = _run_select_agent(issue, plans, work_dir)
    if select_result is None:
        print("[cai plan] select agent produced no output; skipping pipeline", flush=True)
        return None

    plan_text, confidence, confidence_reason, requires_human_review, approvable_at_medium = select_result
    conf_name = confidence.name if confidence else "MISSING"
    print(
        f"[cai plan] select agent produced {len(plan_text)} chars "
        f"(confidence={conf_name}, requires_human_review={requires_human_review}, "
        f"approvable_at_medium={approvable_at_medium})",
        flush=True,
    )
    return plan_text, confidence, confidence_reason, requires_human_review, approvable_at_medium


# ---------------------------------------------------------------------------
# Handlers.
# ---------------------------------------------------------------------------

def handle_plan(issue: dict) -> int:
    """Drive a :refined or :planning issue through the plan-select pipeline.

    The dispatcher supplies an issue whose state is either
    :class:`IssueState.REFINED` (fresh entry — we apply
    ``refined_to_planning`` first) or :class:`IssueState.PLANNING`
    (resume — we skip the entry transition). On success the issue is
    transitioned to :class:`IssueState.PLANNED`; on pipeline/edit
    failure we divert to :human-needed via ``planning_to_human`` so the
    issue does not stay stuck mid-planning.

    ``handle_plan_gate`` performs the subsequent confidence-gated
    auto-advance to ``PLAN_APPROVED``.
    """
    t0 = time.monotonic()

    issue_number = issue["number"]
    title = issue["title"]
    label_names = [l["name"] for l in issue.get("labels", [])]  # noqa: E741
    state = get_issue_state(label_names)

    print(f"[cai plan] picked #{issue_number}: {title}", flush=True)

    # 1. Entry transition :refined → :planning (only on fresh entry).
    if state == IssueState.REFINED:
        fire_trigger(
            issue_number, "refined_to_planning",
            current_labels=label_names,
            log_prefix="cai plan",
        )
    elif state == IssueState.PLANNING:
        print(
            f"[cai plan] resuming #{issue_number} already at :planning",
            flush=True,
        )
    else:
        print(
            f"[cai plan] #{issue_number} unexpected state {state!r} "
            f"— aborting to prevent label corruption",
            file=sys.stderr, flush=True,
        )
        log_run("plan", repo=REPO, issue=issue_number,
                result="unexpected_state", exit=1)
        return 1

    # 2. Clone repo (plan agents need to read the codebase).
    _uid = uuid.uuid4().hex[:8]
    work_dir = Path(f"/tmp/cai-plan-{issue_number}-{_uid}")
    try:
        if work_dir.exists():
            shutil.rmtree(work_dir)
        clone = _run(
            ["git", "clone", "--depth", "1",
             f"https://github.com/{REPO}.git", str(work_dir)],
            capture_output=True,
        )
        if clone.returncode != 0:
            print(f"[cai plan] git clone failed:\n{clone.stderr}",
                  file=sys.stderr)
            fire_trigger(
                issue_number, "planning_to_human",
                current_labels=[LABEL_PLANNING],
                log_prefix="cai plan",
                divert_reason=(
                    "`cai plan` could not clone the repository to run "
                    "the plan-select pipeline. Inspect the container's "
                    "network or gh auth state and resume once the clone "
                    "can succeed."
                ),
            )
            log_run("plan", repo=REPO, issue=issue_number,
                    result="clone_failed", exit=1)
            return 1

        # 3. Fetch previous fix attempts for context.
        attempts = _fetch_previous_fix_attempts(issue_number)
        attempt_history_block = _build_attempt_history_block(attempts)
        if attempt_history_block:
            print(
                f"[cai plan] injecting {len(attempts)} previous fix "
                f"attempt(s) for #{issue_number}",
                flush=True,
            )

        # 4. Run plan-select pipeline.
        pipeline_result = _run_plan_select_pipeline(
            issue, work_dir, attempt_history_block,
        )
        if pipeline_result is None:
            print(f"[cai plan] plan pipeline failed for #{issue_number}",
                  file=sys.stderr)
            fire_trigger(
                issue_number, "planning_to_human",
                current_labels=[LABEL_PLANNING],
                log_prefix="cai plan",
                divert_reason=(
                    "The plan → select pipeline produced no usable "
                    "output (one of the plan agents or cai-select "
                    "failed). No plan was stored; re-run the planning "
                    "step after resolving the underlying failure."
                ),
            )
            dur = f"{int(time.monotonic() - t0)}s"
            log_run("plan", repo=REPO, issue=issue_number,
                    duration=dur, result="pipeline_failed", exit=1)
            return 1
        (
            selected_plan,
            plan_confidence,
            plan_confidence_reason,
            plan_requires_human_review,
            plan_approvable_at_medium,
        ) = pipeline_result

        # 5. Store plan in issue body (strip any old plan block first).
        current_body = _strip_stored_plan_block(issue.get("body", "") or "")
        conf_name = plan_confidence.name if plan_confidence else "MISSING"
        plan_block = (
            "<!-- cai-plan-start -->\n"
            "## Selected Implementation Plan\n\n"
            f"{selected_plan}\n"
            f"Confidence: {conf_name}\n"
        )
        if plan_confidence_reason:
            plan_block += f"Confidence reason: {plan_confidence_reason}\n"
        if plan_requires_human_review:
            plan_block += "Requires human review: true\n"
        if plan_approvable_at_medium:
            plan_block += "Approvable at medium: true\n"
        plan_block += "<!-- cai-plan-end -->"
        new_body = f"{plan_block}\n\n{current_body}"
        update = _run(
            ["gh", "issue", "edit", str(issue_number),
             "--repo", REPO, "--body", new_body],
            capture_output=True,
        )
        if update.returncode != 0:
            print(f"[cai plan] gh issue edit failed:\n{update.stderr}",
                  file=sys.stderr)
            fire_trigger(
                issue_number, "planning_to_human",
                current_labels=[LABEL_PLANNING],
                log_prefix="cai plan",
                divert_reason=(
                    "`gh issue edit` refused to persist the newly-"
                    "generated plan block into this issue body. "
                    "Inspect gh auth / rate limits and resume planning "
                    "once writes succeed."
                ),
            )
            dur = f"{int(time.monotonic() - t0)}s"
            log_run("plan", repo=REPO, issue=issue_number,
                    duration=dur, result="edit_failed", exit=1)
            return 1

        # Stash the confidence, reason, plan text, human-review flag, and
        # approvable-at-medium flag on the issue dict so the gate handler
        # (run as a separate dispatcher step) can read them. This is
        # belt-and-braces — the gate also reparses from the body if we
        # ever split the two calls across processes. The plan text is
        # consulted by handle_plan_gate to pick between the default
        # HIGH-gated and the MEDIUM-gated mitigated transition (see #918
        # and _plan_has_anchor_mitigation). The human-review flag (#982)
        # forces a divert with a bespoke admin-approval message when
        # cai-select flagged a knowing divergence from the refined-issue's
        # stated preference. The approvable-at-medium flag (#1008) routes
        # MEDIUM-confidence plans with only soft / non-blocking residual
        # risks through the planned_to_plan_approved_approvable MEDIUM-
        # threshold sibling transition.
        issue["_cai_plan_confidence"] = plan_confidence
        issue["_cai_plan_confidence_reason"] = plan_confidence_reason
        issue["_cai_plan_text"] = selected_plan
        issue["_cai_plan_requires_human_review"] = plan_requires_human_review
        issue["_cai_plan_approvable_at_medium"] = plan_approvable_at_medium

        # 6. Transition labels: :planning → :planned (waypoint).
        ok, _ = fire_trigger(
            issue_number, "planning_to_planned",
            current_labels=[LABEL_PLANNING],
            log_prefix="cai plan",
        )
        if not ok:
            dur = f"{int(time.monotonic() - t0)}s"
            log_run("plan", repo=REPO, issue=issue_number,
                    duration=dur, result="label_update_failed", exit=1)
            return 1

        dur = f"{int(time.monotonic() - t0)}s"
        conf_name = plan_confidence.name if plan_confidence else "MISSING"
        print(
            f"[cai plan] #{issue_number} planned :planning → :planned in {dur} "
            f"(confidence={conf_name}); running confidence gate inline",
            flush=True,
        )
        # Run the confidence gate inline so it is atomic with planning.
        # (The dispatcher also has PLANNED → handle_plan_gate as a safety net
        # for issues already stuck at :planned.)
        return handle_plan_gate(issue)

    finally:
        if work_dir.exists():
            shutil.rmtree(work_dir, ignore_errors=True)


def handle_plan_gate(issue: dict) -> int:
    """Confidence-gated auto-advance :planned → :plan-approved.

    Expects an issue at :class:`IssueState.PLANNED` with a stored plan
    block and a confidence marker (either stashed under
    ``_cai_plan_confidence`` by :func:`handle_plan` within the same
    process, or re-parsed from the issue body). HIGH-confidence plans
    auto-promote via ``planned_to_plan_approved``; anything below
    diverts to :human-needed (`planned_to_human`) so an admin can
    review.

    **Anchor-mitigation relaxation (#918):** when the selected plan
    text contains the marker phrase ``locate edits by anchor text ...
    not by line number``, the gate routes through
    ``planned_to_plan_approved_mitigated`` instead — a sibling
    transition that performs the same label move but accepts
    :attr:`Confidence.MEDIUM`. This lets plans whose only residual
    risk is implementation-detail (line-number drift, fence escaping,
    cosmetic wording) auto-approve because the fix agent is
    explicitly instructed to anchor on surrounding text. Plans
    without the marker continue to require HIGH via the default
    transition.

    **Human-review override (#982):** when ``cai-select`` set
    ``requires_human_review=true`` in its structured output (stashed
    under ``_cai_plan_requires_human_review`` or re-parsed from the
    ``Requires human review: true`` marker in the stored plan block),
    the gate diverts directly to ``:human-needed`` via
    ``planned_to_human`` with a bespoke admin-approval message —
    independent of the reported confidence and of the anchor-mitigation
    marker. This surfaces a clearer divert reason than the generic
    confidence-gate trip when the selected plan knowingly diverges
    from the refined-issue's stated preference.
    """
    from cai_lib.fsm import (
        parse_confidence,
        parse_confidence_reason,
        parse_requires_human_review,
        parse_approvable_at_medium,
    )
    from cai_lib.cmd_helpers import _extract_stored_plan

    t0 = time.monotonic()
    issue_number = issue["number"]

    # Recover the confidence marker and reason. Prefer the in-process stash
    # from handle_plan; otherwise parse from the stored plan block in the
    # issue body (for dispatchers that run the two handlers across
    # separate invocations).
    plan_confidence = issue.get("_cai_plan_confidence")
    plan_confidence_reason = issue.get("_cai_plan_confidence_reason")
    if plan_confidence is None:
        body = issue.get("body", "") or ""
        plan_confidence = parse_confidence(body)
    if plan_confidence_reason is None:
        body = issue.get("body", "") or ""
        plan_confidence_reason = parse_confidence_reason(body)

    # Recover the selected plan text for the anchor-mitigation check.
    # Prefer the in-process stash from handle_plan (exact text just
    # chosen by cai-select); fall back to the stored plan block when
    # this gate runs as a separate dispatcher step with a stale issue
    # dict. Missing / empty plan text falls through to the default
    # transition because _plan_has_anchor_mitigation returns False on
    # None or empty input.
    plan_text = issue.get("_cai_plan_text")
    if plan_text is None:
        plan_text = _extract_stored_plan(issue.get("body", "") or "") or ""

    # Recover the explicit human-review flag (#982). Prefer the
    # in-process stash; fall back to re-parsing the stored plan block.
    requires_human_review = issue.get("_cai_plan_requires_human_review")
    if requires_human_review is None:
        body = issue.get("body", "") or ""
        requires_human_review = parse_requires_human_review(body)

    # Recover the explicit approvable-at-medium flag (#1008). Prefer the
    # in-process stash; fall back to re-parsing the stored plan block.
    approvable_at_medium = issue.get("_cai_plan_approvable_at_medium")
    if approvable_at_medium is None:
        body = issue.get("body", "") or ""
        approvable_at_medium = parse_approvable_at_medium(body)

    # Deterministic scale/complexity auto-flag (#1131). Promotes
    # requires_human_review to True when the stored plan is LOW
    # confidence AND either (a) lists >= _LARGE_SCOPE_FILE_THRESHOLD
    # files to change or (b) a prior MARKER divert on this issue
    # already cited scale/complexity. Only runs when cai-select did
    # NOT already set requires_human_review=true (#982) — that
    # branch owns its own bespoke reason. The promoted signal
    # reuses the existing divert code path below (fire_trigger +
    # _set_labels + log_run) so only the reason text differs.
    auto_flagged_reason = None
    if not requires_human_review:
        auto_flagged_reason = _auto_flagged_human_review_reason(
            issue, plan_confidence,
        )
        if auto_flagged_reason is not None:
            requires_human_review = True

    # Human-review override path (#982 + #1131): cai-select flagged
    # the plan OR the Python-side scale/complexity auto-flag fired.
    # Divert unconditionally with a bespoke message rather than
    # letting the confidence gate emit a generic "gate not met"
    # comment. Independent of confidence level and of the
    # anchor-mitigation marker.
    if requires_human_review:
        divert_signal = (
            "scale_complexity" if auto_flagged_reason is not None
            else "refined_preference"
        )
        print(
            f"[cai plan] #{issue_number} human-review divert "
            f"({divert_signal}) — routing via planned_to_human",
            flush=True,
        )
        if auto_flagged_reason is not None:
            reason_lines = [auto_flagged_reason]
        else:
            reason_lines = [
                "Plan diverges from refined-issue preference \u2014 admin "
                "approval required before the fix agent proceeds.",
            ]
        if plan_confidence_reason:
            reason_lines.extend(["", plan_confidence_reason.rstrip()])
        ok, _ = fire_trigger(
            issue_number, "planned_to_human",
            current_labels=[LABEL_PLANNED],
            log_prefix="cai plan",
            divert_reason="\n".join(reason_lines),
        )
        dur = f"{int(time.monotonic() - t0)}s"
        conf_name = plan_confidence.name if plan_confidence else "MISSING"
        if not ok:
            print(
                f"[cai plan] #{issue_number} human-review divert refused — "
                f"state did not advance",
                file=sys.stderr,
                flush=True,
            )
            log_run("plan", repo=REPO, issue=issue_number,
                    duration=dur, result="gate_refused",
                    confidence=conf_name, diverted=1,
                    transition="planned_to_human",
                    requires_human_review=1,
                    exit=1)
            return 1
        # Surface the planner's explicit admin-review request as a
        # supplementary label on top of :human-needed (#1128). Makes
        # the human checkpoint visible in the FSM label trail rather
        # than buried in the plan body, and lets `cai rescue` skip
        # this issue instead of burning autonomous-resume cycles on a
        # park the planner itself flagged as needing admin sign-off.
        # Failure to apply the label is non-fatal — the FSM divert
        # has already succeeded; log and continue so the caller still
        # sees a 0 return code.
        if not _set_labels(
            issue_number,
            add=[LABEL_PLAN_NEEDS_REVIEW],
            log_prefix="cai plan",
        ):
            print(
                f"[cai plan] #{issue_number} failed to apply "
                f"{LABEL_PLAN_NEEDS_REVIEW}; divert already logged — "
                f"continuing",
                file=sys.stderr,
                flush=True,
            )
        print(
            f"[cai plan] #{issue_number} gate → :human-needed in {dur} "
            f"(requires_human_review=true, confidence={conf_name})",
            flush=True,
        )
        log_run("plan", repo=REPO, issue=issue_number,
                duration=dur, result="gate_ok",
                confidence=conf_name, diverted=1,
                transition="planned_to_human",
                requires_human_review=1,
                exit=0)
        return 0

    # Pick the transition. All four siblings perform the identical
    # label move PLANNED → PLAN_APPROVED; only the confidence threshold
    # differs (HIGH for the default, MEDIUM for the three relaxations).
    # The divert target (planned_to_human) is inherited from the
    # chosen transition's human_label_if_below, so a below-threshold
    # LOW/MISSING plan still ends up at :human-needed regardless of
    # which transition was selected.
    #
    # Precedence (most specific first):
    #
    #   1. Docs-only structural relaxation (#989) — every listed path
    #      is under docs/; blast radius is bounded absolutely.
    #   2. Anchor-mitigation marker relaxation (#918) — the fix agent
    #      is instructed to anchor edits on surrounding text.
    #   3. Approvable-at-medium flag relaxation (#1008) — cai-select
    #      explicitly judged the plan's residual risks soft /
    #      non-blocking (most general of the three).
    #
    # If a plan qualifies for several, the most specific route wins
    # so the divert-reason log cites the most specific relaxation.
    if _plan_targets_only_docs(plan_text):
        transition_name = "planned_to_plan_approved_docs_only"
        print(
            f"[cai plan] #{issue_number} plan targets only docs/ — "
            f"routing through {transition_name} (#989)",
            flush=True,
        )
    elif _plan_has_anchor_mitigation(plan_text):
        transition_name = "planned_to_plan_approved_mitigated"
        print(
            f"[cai plan] #{issue_number} anchor-mitigation marker present — "
            f"routing through {transition_name} (#918)",
            flush=True,
        )
    elif approvable_at_medium:
        transition_name = "planned_to_plan_approved_approvable"
        print(
            f"[cai plan] #{issue_number} cai-select flagged approvable_at_medium — "
            f"routing through {transition_name} (#1008)",
            flush=True,
        )
    else:
        transition_name = "planned_to_plan_approved"

    # Apply the gate. Threshold met → :plan-approved; below → :human-needed
    # via the configured divert target.
    ok, diverted = fire_trigger(
        issue_number, transition_name,
        confidence=plan_confidence,
        _confidence_gated=True,
        current_labels=[LABEL_PLANNED],
        log_prefix="cai plan",
        reason_extra=plan_confidence_reason or "",
    )
    if not ok:
        # Transition or divert refused (e.g. state drift, label-edit failure).
        # Returning 0 here would leave the issue at :planned and cause the
        # dispatcher to re-pick the same target every cycle (#657). Report
        # failure so the cycle's worst_rc reflects the stall.
        dur = f"{int(time.monotonic() - t0)}s"
        conf_name = plan_confidence.name if plan_confidence else "MISSING"
        print(
            f"[cai plan] #{issue_number} gate refused — state did not advance",
            file=sys.stderr,
            flush=True,
        )
        log_run("plan", repo=REPO, issue=issue_number,
                duration=dur, result="gate_refused",
                confidence=conf_name, diverted=int(diverted),
                transition=transition_name,
                exit=1)
        return 1

    dur = f"{int(time.monotonic() - t0)}s"
    conf_name = plan_confidence.name if plan_confidence else "MISSING"
    final_state = "human-needed" if diverted else "plan-approved"
    print(
        f"[cai plan] #{issue_number} gate → :{final_state} in {dur} "
        f"(confidence={conf_name}, transition={transition_name})",
        flush=True,
    )

    # Pre-emptive Opus-tier escalation for large mechanical refactors
    # (#1139). When the plan was just approved (non-diverted) AND the
    # plan body qualifies as a large mechanical refactor (>= 8 files in
    # ### Files to change AND >= 50 #### Step N — Edit/Write headers),
    # apply LABEL_OPUS_ATTEMPTED directly. handle_implement reads this
    # label via ``opus_escalation = LABEL_OPUS_ATTEMPTED in label_names``
    # and will run the subagent with ``--model claude-opus-4-7`` from
    # the start — skipping the Sonnet retry loop that normally consumes
    # 2–3 slots on a large-refactor plan (see #1136).
    #
    # Guarded by ``not diverted`` so we never stamp the label on a plan
    # the gate just parked at :human-needed — the admin can then choose
    # the implement tier when resuming. Also short-circuits if the label
    # is already present (e.g. the plan was re-run after a rescue
    # escalation) so we don't spam duplicate comments.
    if not diverted and _plan_is_large_mechanical_refactor(plan_text):
        current_labels = {
            lbl["name"] for lbl in issue.get("labels", [])
        }
        if LABEL_OPUS_ATTEMPTED not in current_labels:
            file_count = _count_files_to_change(plan_text)
            step_count = _count_edit_steps(plan_text)
            print(
                f"[cai plan] #{issue_number} plan is a large mechanical "
                f"refactor "
                f"(files={file_count} "
                f">= {_LARGE_REFACTOR_FILE_THRESHOLD}, "
                f"edit_steps={step_count} "
                f">= {_LARGE_REFACTOR_EDIT_SITE_THRESHOLD}); "
                f"applying {LABEL_OPUS_ATTEMPTED} to pre-empt the "
                f"Sonnet attempt and route `cai implement` to Opus "
                f"(#1139)",
                flush=True,
            )
            if _set_labels(
                issue_number,
                add=[LABEL_OPUS_ATTEMPTED],
                log_prefix="cai plan",
            ):
                _post_issue_comment(
                    issue_number,
                    (
                        "## Pre-emptive Opus-tier escalation (#1139)\n\n"
                        f"This plan lists **{file_count} files** in "
                        f"`### Files to change` and **{step_count} "
                        f"`#### Step N — Edit/Write` headers**, both "
                        f"at or above the large-mechanical-refactor "
                        f"thresholds "
                        f"({_LARGE_REFACTOR_FILE_THRESHOLD} files / "
                        f"{_LARGE_REFACTOR_EDIT_SITE_THRESHOLD} edit "
                        f"sites). `cai implement` will therefore run "
                        f"the Opus tier from the start instead of "
                        f"attempting Sonnet first — avoiding the "
                        f"three-Sonnet-retry loop observed on similar "
                        f"plans (see #1136).\n\n"
                        f"---\n"
                        f"_Applied by `cai plan` structural detector. "
                        f"If Opus also fails, `cai rescue` will not "
                        f"re-escalate; remove `"
                        f"{LABEL_OPUS_ATTEMPTED}` manually to force a "
                        f"Sonnet attempt._"
                    ),
                    log_prefix="cai plan",
                )
            else:
                print(
                    f"[cai plan] #{issue_number} failed to apply "
                    f"{LABEL_OPUS_ATTEMPTED}; implement will run at "
                    f"the Sonnet tier as a fallback",
                    file=sys.stderr,
                    flush=True,
                )

    log_run("plan", repo=REPO, issue=issue_number,
            duration=dur, result="gate_ok",
            confidence=conf_name, diverted=int(diverted),
            transition=transition_name,
            exit=0)
    return 0
