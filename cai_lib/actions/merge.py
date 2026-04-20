"""cai_lib.actions.merge — handler for PRState.APPROVED.

Invoked by the FSM dispatcher after it has fetched an open PR and
verified its state is ``PRState.APPROVED``. Runs the ``cai-merge``
agent to obtain a confidence-gated verdict and, on a high-enough
merge verdict, squash-merges via ``gh pr merge``. On new commits
arriving since the APPROVED label was set, diverts back to code
review. On low-confidence / refusal / merge failure on a still-open
PR, transitions the PR out of APPROVED into ``PR_HUMAN_NEEDED``
(``approved_to_human``) so the dispatcher parks it instead of
re-routing back to ``handle_merge`` every drain tick.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time

from cai_lib.config import (
    REPO,
    LABEL_PR_OPEN,
    LABEL_MERGED,
    LABEL_MERGE_BLOCKED,
    LABEL_REVISING,
    LABEL_PLAN_APPROVED,
    LABEL_PR_NEEDS_HUMAN,
)
from cai_lib.fsm import apply_pr_transition, get_pr_state, PRState
from cai_lib.github import _gh_json, _set_labels, _issue_has_label, close_issue_not_planned
from cai_lib.subprocess_utils import _run, _run_claude_p
from cai_lib.cmd_helpers import (
    _pr_set_needs_human,
    _parse_iso_ts,
    _is_bot_comment,
    _fetch_review_comments,
    _extract_stored_plan,
)
from cai_lib.actions.plan import (
    _FILES_TO_CHANGE_SECTION_RE,
    _FILES_TO_CHANGE_PATH_RE,
)
from cai_lib.actions.revise import _filter_comments_with_haiku
from cai_lib.logging_utils import log_run


# ---------------------------------------------------------------------------
# Merge-local constants (moved from cai.py; sole owner).
# ---------------------------------------------------------------------------

_MERGE_COMMENT_HEADING = "## cai merge verdict"

# Confidence threshold: only verdicts at or above this level trigger a merge.
# "high" = only high merges, "medium" = high + medium merge,
# "disabled" = never merge.
_MERGE_THRESHOLD = os.environ.get("CAI_MERGE_CONFIDENCE_THRESHOLD", "high").lower()

_CONFIDENCE_RANKS = {"high": 3, "medium": 2, "low": 1}

# Bot-PR branch prefix regex. Only PRs on ``auto-improve/<issue>-…``
# branches are eligible for auto-merge.
_BOT_BRANCH_RE = re.compile(r"^auto-improve/(\d+)-")

# Truncate very large diffs before feeding the merge agent to bound
# token cost per PR.  Configurable via env var so the ceiling can be
# raised without a code change.
_MERGE_MAX_DIFF_LEN = int(os.environ.get("CAI_MERGE_MAX_DIFF_LEN", "200000"))

# JSON schema for structured merge verdict (forced tool-use via --json-schema).
_MERGE_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "confidence": {
            "type": "string",
            "enum": ["high", "medium", "low"],
        },
        "action": {
            "type": "string",
            "enum": ["merge", "hold", "reject"],
        },
        "reasoning": {
            "type": "string",
        },
    },
    "required": ["confidence", "action", "reasoning"],
}


# ---------------------------------------------------------------------------
# Re-queue scope-expansion exemption (wrapper-driven).
#
# When ``cai-confirm`` judges a prior merged PR unsolved it appends a
# ``## Confirm re-queue (attempt N)`` block to the issue body and
# re-routes the issue back through refine/plan. The replacement plan
# is expected to be meaningfully different from the prior attempt —
# often broader (new helpers, new files, new tests) — precisely
# because the narrower previous attempt failed. Without an exemption
# the merge agent would downgrade the new PR on scope grounds even
# though the plan-selection gate already approved the broader scope.
#
# We detect the condition **structurally, in Python**, rather than
# teaching the agent to parse markers at inference time:
#
#   1. Search the issue body for the ``## Confirm re-queue (attempt
#      N)`` producer marker (deterministically emitted by
#      ``cai_lib/actions/confirm.py`` — see ``_requeue_block``).
#   2. Extract the stored plan body via ``_extract_stored_plan``.
#   3. Pull the backticked ``path/with.ext`` tokens out of the plan's
#      ``### Files to change`` section using ``_FILES_TO_CHANGE_SECTION_RE``
#      and ``_FILES_TO_CHANGE_PATH_RE`` imported from
#      ``cai_lib/actions/plan.py`` (shared with ``_plan_targets_only_docs``).
#   4. Emit a ``## Pre-authorized scope expansion`` markdown block
#      naming those files. The block is injected into the merge
#      agent's ``user_message`` between the issue body and the PR
#      diff, so the agent receives an explicit authoritative list of
#      pre-approved files instead of having to detect a marker or
#      parse a plan itself.
#
# When any precondition fails (no marker, no plan, no section, no
# paths) the helper returns the empty string, and ``user_message``
# is assembled exactly as before — backward-compatible for every
# ordinary first-attempt PR. See ``cai-merge.md`` for the
# complementary agent-side rule.
# ---------------------------------------------------------------------------
_REQUEUE_MARKER_RE = re.compile(
    r"^## Confirm re-queue \(attempt \d+\)", re.MULTILINE
)

def _build_requeue_exemption_block(issue_body: str) -> str:
    """Return a pre-authorized-scope markdown block, or ``""`` when
    the re-queue exemption does not apply to *issue_body*.

    Returns ``""`` when:

    * the ``## Confirm re-queue (attempt N)`` marker is absent
      (ordinary first-attempt PR),
    * the issue body has no extractable stored plan block,
    * the stored plan has no ``### Files to change`` section, or
    * that section contains no backticked ``path/with.ext`` tokens.

    Otherwise returns a markdown block of the form::

        ## Pre-authorized scope expansion

        This issue was re-queued by `cai-confirm` ...

        - `path/to/file_a.py`
        - `path/to/file_b.py`

        **Treat every file in this list as in-scope for this PR.** ...

    Extracted paths are deduplicated while preserving first-seen
    order so the injected block stays stable across repeated runs.
    """
    if not issue_body or not _REQUEUE_MARKER_RE.search(issue_body):
        return ""

    plan_text = _extract_stored_plan(issue_body)
    if not plan_text:
        return ""

    section = _FILES_TO_CHANGE_SECTION_RE.search(plan_text)
    if not section:
        return ""

    paths = _FILES_TO_CHANGE_PATH_RE.findall(section.group(1))
    if not paths:
        return ""

    seen: set[str] = set()
    unique_paths: list[str] = []
    for p in paths:
        if p not in seen:
            seen.add(p)
            unique_paths.append(p)

    bullet_list = "\n".join(f"- `{p}`" for p in unique_paths)
    return (
        "## Pre-authorized scope expansion\n\n"
        "This issue was re-queued by `cai-confirm` after a prior "
        "merged PR was judged **unsolved**. The plan-selection gate "
        "has already approved the following expanded file scope for "
        "this attempt:\n\n"
        f"{bullet_list}\n\n"
        "**Treat every file in this list as in-scope for this PR.** "
        "Do not downgrade confidence for scope-only reasons on these "
        "files (e.g. \"scope broader than the issue asks\", \"new "
        "files not mentioned in the issue\", \"PR adds new test "
        "files or docstrings\"). Correctness, completeness, "
        "unaddressed review comments, and workflow-file "
        "(`.github/workflows/`) rules still apply in full.\n\n"
    )


# ---------------------------------------------------------------------------
# Pipeline co-edits scope exemption (wrapper-driven, issue #990).
#
# When the pre-merge pipeline runs, ``cai-review-pr`` and
# ``cai-review-docs`` may identify files outside the issue's stated
# scope that nonetheless need to be edited as ripple effects (e.g.
# ``README.md`` help text, ``cai.py`` docstrings,
# ``scripts/generate-index.sh``). The pipeline either commits those
# edits directly (``cai-review-docs``) or asks the revise agent to
# apply them (``cai-review-pr``). Either way, by the time the merge
# agent runs, those files appear in the PR diff and would naively be
# flagged as scope creep.
#
# The agent prompt has soft exemptions for both cases (see
# ``cai-merge.md``: ``Exemption: reviewer-recommended co-changes``
# and ``Exemption: docs-reviewer co-edits``), but the agent applied
# them inconsistently — most visibly on issue #928, which parked
# three times in a row at MEDIUM despite the merger's own reasoning
# calling the changes "legitimate pipeline work" each time. Each
# park burnt a rescue cycle and a re-evaluation Opus call.
#
# We detect the condition **structurally, in Python**, mirroring the
# requeue exemption above:
#
#   1. Walk every PR comment looking for the two pipeline review
#      headings: ``## cai docs review (applied) - <sha>`` and
#      ``## cai pre-merge review - <sha>`` (the non-clean variant —
#      ``(clean)`` headings have no findings to extract).
#   2. Inside each matching comment body, find every ``**File(s):**``
#      line and split its value on commas, stripping ``(lines X-Y)``
#      decorations and surrounding backticks.
#   3. Emit a ``## Pre-authorized pipeline co-edits`` markdown block
#      naming the deduplicated path list (first-seen order) so the
#      merge agent receives an explicit authoritative list of
#      pipeline-approved files instead of having to scan comment
#      history itself.
#
# When no qualifying comments or paths are found the helper returns
# the empty string, leaving ``user_message`` byte-identical to the
# pre-#990 form for ordinary PRs. See the ``Exemption: wrapper-
# injected pre-authorized pipeline co-edits`` section in
# ``cai-merge.md`` for the complementary agent-side rule.
# ---------------------------------------------------------------------------
_PIPELINE_REVIEW_HEADING_DOCS_APPLIED = "## cai docs review (applied)"
_PIPELINE_REVIEW_HEADING_PRE_MERGE = "## cai pre-merge review"
_PIPELINE_REVIEW_HEADING_PRE_MERGE_CLEAN = "## cai pre-merge review (clean)"

_FILES_LINE_RE = re.compile(
    r"^\*\*File\(s\):\*\*\s+(.+)$", re.MULTILINE
)
_FILES_LINE_PARENTHETICAL_TAIL_RE = re.compile(r"\s*\([^)]*\)\s*$")


def _is_pipeline_coedit_comment_body(body: str) -> bool:
    """Return ``True`` when *body*'s first line is a pipeline review
    heading whose contents may carry ``**File(s):**`` paths.

    Matches::

        ## cai docs review (applied) - <sha>
        ## cai pre-merge review - <sha>

    Excludes::

        ## cai docs review (clean) - <sha>
        ## cai pre-merge review (clean) - <sha>

    Clean review comments have no ``### Finding:`` or ``### Fixed:``
    blocks so they cannot contribute paths.
    """
    if not body:
        return False
    first_line = body.lstrip().split("\n", 1)[0]
    if first_line.startswith(_PIPELINE_REVIEW_HEADING_DOCS_APPLIED):
        return True
    if (
        first_line.startswith(_PIPELINE_REVIEW_HEADING_PRE_MERGE)
        and not first_line.startswith(
            _PIPELINE_REVIEW_HEADING_PRE_MERGE_CLEAN
        )
    ):
        return True
    return False


def _extract_paths_from_files_line(value: str) -> list[str]:
    """Parse a ``**File(s):**`` value into a list of clean paths.

    Splits *value* on commas, strips trailing parenthetical
    decorations like ``(lines 35-37)``, removes surrounding
    backticks, and discards empty tokens. Order is preserved.
    """
    out: list[str] = []
    for raw in value.split(","):
        token = raw.strip()
        token = _FILES_LINE_PARENTHETICAL_TAIL_RE.sub("", token).strip()
        token = token.strip("`").strip()
        if token:
            out.append(token)
    return out


def _build_pipeline_coedits_exemption_block(
    all_comments: list[dict],
) -> str:
    """Return a pre-authorized pipeline-coedits markdown block, or
    ``""`` when no qualifying paths are found in *all_comments*.

    Walks every comment in *all_comments*, keeps only those whose
    body starts with a qualifying pipeline review heading (see
    :func:`_is_pipeline_coedit_comment_body`), extracts every
    ``**File(s):**`` line, and concatenates the per-line path
    lists. The combined list is deduplicated while preserving
    first-seen order so repeated runs produce a stable block.

    Returns ``""`` when *all_comments* is empty, no comment
    qualifies, or no qualifying comment yields any path. In that
    case the merge ``user_message`` is byte-identical to the
    pre-#990 form for ordinary first-attempt PRs.
    """
    if not all_comments:
        return ""

    seen: set[str] = set()
    unique_paths: list[str] = []
    for comment in all_comments:
        body = comment.get("body") or ""
        if not _is_pipeline_coedit_comment_body(body):
            continue
        for match in _FILES_LINE_RE.finditer(body):
            for path in _extract_paths_from_files_line(match.group(1)):
                if path not in seen:
                    seen.add(path)
                    unique_paths.append(path)

    if not unique_paths:
        return ""

    bullet_list = "\n".join(f"- `{p}`" for p in unique_paths)
    return (
        "## Pre-authorized pipeline co-edits\n\n"
        "The pre-merge pipeline (`cai-review-pr` and "
        "`cai-review-docs`) has already cited the following files "
        "as in-scope for this PR — either as `### Finding:` blocks "
        "asking the revise agent to update them, or as "
        "`### Fixed: stale_docs` blocks where `cai-review-docs` "
        "directly committed the edit:\n\n"
        f"{bullet_list}\n\n"
        "**Treat every file in this list as in-scope for this PR.** "
        "Do not downgrade confidence for scope-only reasons on these "
        "files (e.g. \"scope broader than the issue asks\", \"new "
        "files not mentioned in the issue\", \"PR adds new test "
        "files or docstrings\"). Correctness, completeness, "
        "unaddressed review comments, and workflow-file "
        "(`.github/workflows/`) rules still apply in full.\n\n"
    )


# ---------------------------------------------------------------------------
# Unauthorized agent-file deletion guard (issue #1024).
#
# When ``cai-review-docs`` co-edits follow a rename/consolidation it
# sometimes tombstones an ``.claude/agents/<name>.md`` file that was
# never listed in the stored plan's ``### Files to change`` section.
# PR #1017 demonstrated this failure mode: a rescue-prevention edit
# to ``cai-rescue.md`` led the docs-reviewer to also delete
# ``cai-select.md`` because the narrative no longer referenced it,
# even though the plan only authorized edits to ``cai-rescue.md`` and
# ``cai_lib/cmd_rescue.py``. The merge agent caught it after the fact
# with a ``low`` verdict, but the scope creep had already consumed an
# Opus merge-agent call.
#
# This guard detects the condition **structurally, in Python**, from
# the untruncated PR diff and the stored plan's file list:
#
#   1. Parse ``diff --git a/<path> b/<path>`` headers followed by a
#      ``deleted file mode`` line to find files the PR removes.
#   2. Filter to paths under ``.claude/agents/`` with an ``.md``
#      suffix.
#   3. Extract the stored plan's ``### Files to change`` paths via
#      ``_FILES_TO_CHANGE_SECTION_RE`` / ``_FILES_TO_CHANGE_PATH_RE``.
#      Canonicalize each path: the plan expresses agent-file
#      deletions via ``.cai-staging/agents-delete/<rel>.md``
#      tombstones; only the tombstone form (or a direct
#      ``.claude/agents/<rel>.md`` reference) authorizes a live
#      deletion. ``.cai-staging/agents/<rel>.md`` edit-intent entries
#      do NOT authorize a deletion.
#   4. Return the deletion paths that lack such authorization.
#
# The merge handler uses the returned list to short-circuit the PR
# to ``approved_to_human`` without invoking the merge agent. Failing
# closed is safe: a return of ``[]`` (no deletions, or all deletions
# authorized) leaves ordinary PRs completely unaffected.
# ---------------------------------------------------------------------------
_DELETED_AGENT_FILE_RE = re.compile(
    r"^diff --git a/(\.claude/agents/\S+\.md) b/\S+\s*$\n"
    r"deleted file mode ",
    re.MULTILINE,
)


def _parse_deleted_agent_files_from_diff(raw_diff: str) -> list[str]:
    """Return the list of ``.claude/agents/**/*.md`` paths this diff
    deletes.

    Scans *raw_diff* for ``diff --git a/<path> b/<path>`` headers
    immediately followed by ``deleted file mode ...``. Only paths
    under ``.claude/agents/`` with an ``.md`` suffix are returned.
    Order is preserved and duplicates are removed. Returns ``[]`` for
    an empty or mismatched diff.
    """
    if not raw_diff:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for m in _DELETED_AGENT_FILE_RE.finditer(raw_diff):
        path = m.group(1)
        if path not in seen:
            seen.add(path)
            out.append(path)
    return out


def _canonicalize_agent_plan_path(path: str) -> str | None:
    """Map a plan-listed path onto the ``.claude/agents/<rel>.md``
    form it authorizes a deletion on, or return ``None`` when the
    path does not authorize a live agent-file deletion.

    The planner expresses agent-file deletions with
    ``.cai-staging/agents-delete/<rel>.md`` tombstone entries; direct
    ``.claude/agents/<rel>.md`` entries are also honored as deletion
    authorizations. ``.cai-staging/agents/<rel>.md`` edit-intent
    entries are **not** authorizations — an edit plan does not grant
    the implementer permission to delete the file.
    """
    if not path:
        return None
    if path.startswith(".cai-staging/agents-delete/"):
        return ".claude/agents/" + path[len(".cai-staging/agents-delete/"):]
    if path.startswith(".claude/agents/"):
        return path
    return None


def _detect_unauthorized_agent_deletions(
    raw_diff: str, issue_body: str,
) -> list[str]:
    """Return the list of ``.claude/agents/**/*.md`` deletions in
    *raw_diff* that are NOT authorized by *issue_body*'s stored plan.

    A deletion is authorized when the stored plan's
    ``### Files to change`` section lists either the live path
    ``.claude/agents/<rel>.md`` or the tombstone path
    ``.cai-staging/agents-delete/<rel>.md`` (see
    :func:`_canonicalize_agent_plan_path`). Edit-intent paths
    (``.cai-staging/agents/<rel>.md``) do not authorize a deletion.

    Returns ``[]`` when the diff contains no agent-file deletions.
    When the issue body has no stored plan or no ``### Files to
    change`` section, **every** agent-file deletion is treated as
    unauthorized so the guard still fires — an agent-file deletion
    with no plan record is exactly the failure mode this guard
    exists to catch.

    Order and de-duplication follow
    :func:`_parse_deleted_agent_files_from_diff`.
    """
    deletions = _parse_deleted_agent_files_from_diff(raw_diff)
    if not deletions:
        return []

    plan_text = _extract_stored_plan(issue_body or "")
    if not plan_text:
        return deletions

    section = _FILES_TO_CHANGE_SECTION_RE.search(plan_text)
    if not section:
        return deletions

    authorized: set[str] = set()
    for p in _FILES_TO_CHANGE_PATH_RE.findall(section.group(1)):
        canonical = _canonicalize_agent_plan_path(p)
        if canonical:
            authorized.add(canonical)

    return [p for p in deletions if p not in authorized]


def _assemble_diff(raw_diff: str, max_len: int) -> str:
    """Assemble a diff string within *max_len* characters.

    Splits *raw_diff* into per-file chunks (each starts with a
    ``diff --git `` header line) and concatenates them in their natural
    order until the budget is exhausted. Omitted files are listed in a
    trailing note so the agent knows the diff is incomplete.
    """
    if len(raw_diff) <= max_len:
        return raw_diff

    _DIFF_HEADER = re.compile(r"^diff --git ", re.MULTILINE)
    parts = _DIFF_HEADER.split(raw_diff)
    preamble = parts[0]
    chunks = ["diff --git " + part for part in parts[1:]]

    assembled = preamble
    omitted: list[str] = []
    for chunk in chunks:
        if len(assembled) + len(chunk) <= max_len:
            assembled += chunk
        else:
            first_line = chunk.split("\n", 1)[0]
            m = re.search(r"b/(\S+)$", first_line)
            omitted.append(m.group(1) if m else first_line[:60])

    if omitted:
        assembled += f"\n... ({len(omitted)} file(s) omitted: {', '.join(omitted)})"

    return assembled


def handle_merge(pr: dict) -> int:
    """Confidence-gated auto-merge for a single APPROVED bot PR.

    The dispatcher has already resolved *pr* to state
    ``PRState.APPROVED``. This handler either:

    * merges the PR (``approved_to_merged``), or
    * applies ``approved_to_human`` (clears ``pr:approved``, sets
      ``pr:human-needed``) when the merge agent refuses / yields low
      confidence / merge itself fails on a still-open PR. Parking is
      done via FSM transition so the PR has exactly one state — the
      old behavior of layering a ``needs-human-review`` flag on top of
      ``pr:approved`` made the dispatcher loop on the same PR every
      drain tick.
    """
    print("[cai merge] evaluating APPROVED PR", flush=True)
    t0 = time.monotonic()

    # Legacy self-heal: an older code path tagged held / failed PRs with
    # the orthogonal ``needs-human-review`` label while leaving them at
    # ``pr:approved``. The dispatcher would re-route to handle_merge
    # every drain tick only to short-circuit on "already evaluated at
    # this SHA". Transition such PRs into the proper PR_HUMAN_NEEDED
    # state so they park cleanly.
    pr_label_names = [
        (lb.get("name") if isinstance(lb, dict) else lb)
        for lb in pr.get("labels", [])
    ]
    if LABEL_PR_NEEDS_HUMAN in pr_label_names:
        print(
            f"[cai merge] PR #{pr['number']}: legacy "
            f"`{LABEL_PR_NEEDS_HUMAN}` flag while at :pr-approved — "
            f"migrating to PR_HUMAN_NEEDED",
            flush=True,
        )
        apply_pr_transition(
            pr["number"], "approved_to_human",
            log_prefix="cai merge",
        )
        log_run("merge", repo=REPO, pr=pr["number"],
                result="legacy_park_migration", exit=0)
        return 0

    if _MERGE_THRESHOLD == "disabled":
        print(
            "[cai merge] CAI_MERGE_CONFIDENCE_THRESHOLD=disabled; skipping",
            flush=True,
        )
        log_run("merge", repo=REPO, result="disabled", exit=0)
        return 0

    if _MERGE_THRESHOLD not in ("high", "medium"):
        print(
            f"[cai merge] unknown threshold '{_MERGE_THRESHOLD}'; "
            f"defaulting to 'high'",
            flush=True,
        )

    threshold_rank = _CONFIDENCE_RANKS.get(
        _MERGE_THRESHOLD, _CONFIDENCE_RANKS["high"]
    )

    pr_number = pr["number"]
    head_sha = pr["headRefOid"]
    branch = pr.get("headRefName", "")
    title = pr["title"]

    # Safety filter 1: only bot PRs. Park non-bot branches as
    # PR_HUMAN_NEEDED (via ``approved_to_human``) so the dispatcher
    # stops re-routing them to ``handle_merge`` every drain tick —
    # a human admin must merge manually. Without this transition a
    # human-authored PR carrying ``pr:approved`` would be picked up
    # forever, logging ``result=not_bot_branch`` once per tick
    # (see issue #1013).
    m = _BOT_BRANCH_RE.match(branch)
    if not m:
        print(
            f"[cai merge] PR #{pr_number}: non-bot branch {branch!r}; "
            f"parking as PR_HUMAN_NEEDED",
            flush=True,
        )
        _run(
            ["gh", "pr", "comment", str(pr_number),
             "--repo", REPO, "--body",
             f"This PR is on branch `{branch}`, which is not an "
             f"`auto-improve/<issue>-…` bot branch, so the `cai merge` "
             f"worker cannot auto-merge it. Moving to "
             f"`pr:human-needed` — a human admin must merge this PR "
             f"manually. Re-applying `pr:approved` will just re-enter "
             f"this state."],
            capture_output=True,
        )
        apply_pr_transition(
            pr_number, "approved_to_human",
            log_prefix="cai merge",
        )
        log_run("merge", repo=REPO, pr=pr_number,
                result="not_bot_branch", exit=0)
        return 0
    issue_number = int(m.group(1))

    # Safety filter 2: unmergeable PRs (conflicts).
    mergeable = pr.get("mergeable", "")
    if mergeable == "CONFLICTING":
        print(
            f"[cai merge] PR #{pr_number}: unmergeable (conflicts); "
            f"skipping",
            flush=True,
        )
        log_run("merge", repo=REPO, pr=pr_number,
                result="conflicting", exit=0)
        return 0

    # Safety filter 3: linked issue must be in :pr-open state.
    try:
        issue = _gh_json([
            "issue", "view", str(issue_number),
            "--repo", REPO,
            "--json", "labels,state",
        ])
    except subprocess.CalledProcessError:
        print(
            f"[cai merge] PR #{pr_number}: could not fetch issue "
            f"#{issue_number}; skipping",
            flush=True,
        )
        log_run("merge", repo=REPO, pr=pr_number,
                result="issue_fetch_failed", exit=0)
        return 0

    issue_labels = [l["name"] for l in issue.get("labels", [])]  # noqa: E741
    if LABEL_PR_OPEN not in issue_labels:
        print(
            f"[cai merge] PR #{pr_number}: issue #{issue_number} not in "
            f":pr-open; skipping",
            flush=True,
        )
        log_run("merge", repo=REPO, pr=pr_number,
                result="issue_not_pr_open", exit=0)
        return 0

    # State gate (defensive — dispatcher should already have verified).
    if get_pr_state(pr) != PRState.APPROVED:
        print(
            f"[cai merge] PR #{pr_number}: not in APPROVED state; waiting",
            flush=True,
        )
        log_run("merge", repo=REPO, pr=pr_number,
                result="wrong_state", exit=0)
        return 0

    # NOTE: the previous SHA gate that diverted APPROVED → REVIEWING_CODE
    # when the current HEAD had no `(clean)` docs-review comment caused
    # ping-pong with the docs-review push path: every doc fix advanced
    # HEAD, the gate fired, code review re-ran on doc-only changes, and
    # docs-review ran again. Docs-review now always advances to APPROVED
    # (push or no push), so this handler trusts the FSM label and lets
    # the unaddressed-comments / CI / merge-agent gates below catch
    # anything that genuinely needs another look.

    # Safety filter 4: unaddressed review comments → let revise handle.
    # Mirror the revise subcommand's filter logic via the shared helper
    # so a "no additional changes" reply correctly suppresses the loop.
    all_comments = list(pr.get("comments", []))
    try:
        all_comments.extend(_fetch_review_comments(pr_number))
    except Exception:
        pass

    unaddressed = _filter_comments_with_haiku(all_comments, pr_number)
    has_unaddressed = bool(unaddressed)

    if has_unaddressed:
        print(
            f"[cai merge] PR #{pr_number}: has unaddressed review "
            f"comments; skipping",
            flush=True,
        )
        log_run("merge", repo=REPO, pr=pr_number,
                result="unaddressed_comments", exit=0)
        return 0

    # Safety filter 5: failed CI checks.
    try:
        pr_detail = _gh_json([
            "pr", "view", str(pr_number),
            "--repo", REPO,
            "--json", "statusCheckRollup",
        ])
        for check in pr_detail.get("statusCheckRollup", []):
            conclusion = (check.get("conclusion") or "").upper()
            status = (check.get("status") or "").upper()
            if conclusion == "FAILURE" or status == "FAILURE":
                print(
                    f"[cai merge] PR #{pr_number}: has failed CI "
                    f"checks; skipping",
                    flush=True,
                )
                log_run("merge", repo=REPO, pr=pr_number,
                        result="ci_failing", exit=0)
                return 0
    except (subprocess.CalledProcessError, json.JSONDecodeError, TypeError):
        pass  # no CI checks is fine

    # Safety filter 6: already evaluated at this SHA, AND no new
    # human comment has been posted since the most recent verdict.
    latest_verdict_ts = None
    for comment in pr.get("comments", []):
        body = (comment.get("body") or "")
        if not body.startswith(f"{_MERGE_COMMENT_HEADING} \u2014 {head_sha}"):
            continue
        v_ts = _parse_iso_ts(comment.get("createdAt"))
        if v_ts is None:
            continue
        if latest_verdict_ts is None or v_ts > latest_verdict_ts:
            latest_verdict_ts = v_ts

    if latest_verdict_ts is not None:
        has_newer_human_comment = False
        for c in all_comments:
            if _is_bot_comment(c):
                continue
            c_ts = _parse_iso_ts(c.get("createdAt"))
            if c_ts is not None and c_ts > latest_verdict_ts:
                has_newer_human_comment = True
                break
        if not has_newer_human_comment:
            print(
                f"[cai merge] PR #{pr_number}: already evaluated at "
                f"{head_sha[:8]}; skipping",
                flush=True,
            )
            log_run("merge", repo=REPO, pr=pr_number,
                    result="already_evaluated", exit=0)
            return 0
        print(
            f"[cai merge] PR #{pr_number}: re-evaluating — new human "
            f"comment since last verdict",
            flush=True,
        )

    # All filters passed — evaluate with the model.
    print(f"[cai merge] evaluating PR #{pr_number}: {title}", flush=True)

    # Fetch issue body.
    try:
        issue_full = _gh_json([
            "issue", "view", str(issue_number),
            "--repo", REPO,
            "--json", "number,title,body",
        ])
    except subprocess.CalledProcessError:
        issue_full = {"number": issue_number, "title": "(unknown)", "body": ""}

    # Fetch PR diff.
    diff_result = _run(
        ["gh", "pr", "diff", str(pr_number), "--repo", REPO],
        capture_output=True,
    )
    if diff_result.returncode != 0:
        print(
            f"[cai merge] could not fetch diff for PR #{pr_number}; "
            f"skipping",
            file=sys.stderr,
        )
        log_run("merge", repo=REPO, pr=pr_number,
                result="diff_failed", exit=0)
        return 0

    # Safety filter 7: unauthorized agent-file deletions (issue #1024).
    # If the PR deletes any .claude/agents/**/*.md file that is NOT
    # authorized by the stored plan's `### Files to change` section
    # (either as a direct `.claude/agents/<rel>.md` entry or as a
    # `.cai-staging/agents-delete/<rel>.md` tombstone entry), park the
    # PR to human-needed without invoking the merge agent. This
    # catches cai-review-docs co-edits that silently delete agent
    # definitions as collateral damage of a rename/consolidation,
    # as observed on PR #1017. Runs on the untruncated diff_result.stdout
    # so a large PR cannot evade the guard via _assemble_diff truncation.
    unauthorized_deletions = _detect_unauthorized_agent_deletions(
        diff_result.stdout, issue_full.get("body") or ""
    )
    if unauthorized_deletions:
        bullets = "\n".join(f"- `{p}`" for p in unauthorized_deletions)
        comment_body = (
            f"## cai merge pre-gate \u2014 unauthorized agent-file "
            f"deletions \u2014 {head_sha}\n\n"
            f"This PR deletes the following `.claude/agents/**/*.md` "
            f"file(s) that are **not** listed in the stored plan's "
            f"`### Files to change` section (neither as a direct "
            f"`.claude/agents/...` entry nor as a "
            f"`.cai-staging/agents-delete/...` tombstone):\n\n"
            f"{bullets}\n\n"
            f"Deleting agent definitions is a significant architectural "
            f"change and must be explicitly authorized by the plan. "
            f"Parking for human review. Admin: either update the plan "
            f"to list the deletion, revert the deletion on the PR "
            f"branch, or close the PR.\n\n"
            f"---\n"
            f"_Pre-merge guard by `cai merge`. Wrapper-side filter \u2014 "
            f"the merge agent was not invoked._"
        )
        _run(
            ["gh", "pr", "comment", str(pr_number),
             "--repo", REPO, "--body", comment_body],
            capture_output=True,
        )
        print(
            f"[cai merge] PR #{pr_number}: unauthorized agent-file "
            f"deletions ({len(unauthorized_deletions)}); parking to "
            f"human-needed",
            flush=True,
        )
        apply_pr_transition(
            pr_number, "approved_to_human",
            log_prefix="cai merge",
        )
        dur_tag = f"{int(time.monotonic() - t0)}s"
        log_run("merge", repo=REPO, pr=pr_number,
                duration=dur_tag,
                result="unauthorized_agent_deletions", exit=0)
        return 0

    pr_diff = _assemble_diff(diff_result.stdout, _MERGE_MAX_DIFF_LEN)

    # Gather PR comments for context.
    comment_texts = []
    for c in all_comments:
        body = (c.get("body") or "").strip()
        if body:
            comment_texts.append(body)
    comments_section = (
        "\n\n---\n\n".join(comment_texts) if comment_texts else "(no comments)"
    )

    requeue_block = _build_requeue_exemption_block(
        issue_full.get("body") or ""
    )
    pipeline_coedits_block = _build_pipeline_coedits_exemption_block(
        all_comments
    )

    user_message = (
        f"## Linked issue\n\n"
        f"### #{issue_full.get('number', issue_number)} \u2014 "
        f"{issue_full.get('title', '')}\n\n"
        f"{issue_full.get('body') or '(no body)'}\n\n"
        f"{requeue_block}"
        f"{pipeline_coedits_block}"
        f"## PR changes\n\n"
        f"```diff\n{pr_diff}\n```\n\n"
        f"## PR comments\n\n"
        f"{comments_section}\n"
    )

    result = _run_claude_p(
        ["claude", "-p", "--agent", "cai-merge",
         "--dangerously-skip-permissions",
         "--json-schema", json.dumps(_MERGE_JSON_SCHEMA)],
        category="merge",
        agent="cai-merge",
        input=user_message,
    )
    if result.returncode != 0:
        print(
            f"[cai merge] model failed for PR #{pr_number} "
            f"(exit {result.returncode}):\n{result.stderr}",
            file=sys.stderr,
        )
        log_run("merge", repo=REPO, pr=pr_number,
                result="agent_failed", exit=result.returncode)
        return result.returncode

    try:
        tool_input = json.loads(result.stdout)
    except (json.JSONDecodeError, ValueError) as exc:
        print(
            f"[cai merge] failed to parse JSON verdict: {exc}; "
            f"stdout starts with: {(result.stdout or '')[:120]!r}",
            file=sys.stderr,
            flush=True,
        )
        tool_input = {}

    confidence = tool_input.get("confidence", "")
    action = tool_input.get("action", "")
    reasoning = tool_input.get("reasoning", "(no reasoning provided)")

    print(
        f"[cai merge] verdict: confidence={confidence} action={action} "
        f"reasoning={reasoning}",
        flush=True,
    )

    if not confidence or not action:
        print(
            f"[cai merge] PR #{pr_number}: could not parse verdict; "
            f"skipping",
            flush=True,
        )
        log_run("merge", repo=REPO, pr=pr_number,
                result="verdict_unparseable", exit=0)
        return 0

    # Post the verdict as a PR comment.
    comment_body = (
        f"{_MERGE_COMMENT_HEADING} \u2014 {head_sha}\n\n"
        f"**Confidence:** {confidence}\n"
        f"**Action:** {action}\n"
        f"**Reasoning:** {reasoning}\n\n"
        f"---\n"
        f"_Auto-merge review by `cai merge`. "
        f"Threshold: `{_MERGE_THRESHOLD}`, verdict: `{confidence}`, "
        f"action: `{action}`._"
    )
    _run(
        ["gh", "pr", "comment", str(pr_number),
         "--repo", REPO, "--body", comment_body],
        capture_output=True,
    )

    verdict_rank = _CONFIDENCE_RANKS.get(confidence, 0)
    dur = lambda: f"{int(time.monotonic() - t0)}s"  # noqa: E731

    if action == "reject" and verdict_rank >= threshold_rank:
        # High-confidence reject: close the PR, mark issue no-action.
        print(
            f"[cai merge] PR #{pr_number}: verdict={confidence} reject "
            f">= threshold={_MERGE_THRESHOLD}; closing",
            flush=True,
        )
        close_result = _run(
            ["gh", "pr", "close", str(pr_number),
             "--repo", REPO, "--delete-branch"],
            capture_output=True,
        )
        if close_result.returncode == 0:
            print(
                f"[cai merge] PR #{pr_number}: closed successfully",
                flush=True,
            )
            closed_ok = close_issue_not_planned(
                issue_number,
                "Closing as **not planned** — the merge subagent reviewed the PR "
                "and determined it should be rejected (high-confidence reject).",
                log_prefix="cai merge",
            )
            if not closed_ok:
                apply_pr_transition(
                    pr_number, "approved_to_human",
                    log_prefix="cai merge",
                )
                log_run("merge", repo=REPO, pr=pr_number,
                        duration=dur(), result="close_label_failed",
                        exit=0)
                return 0
            log_run("merge", repo=REPO, pr=pr_number,
                    duration=dur(), result="closed", exit=0)
            return 0
        else:
            print(
                f"[cai merge] PR #{pr_number}: close failed:\n"
                f"{close_result.stderr}",
                file=sys.stderr,
            )
            if not _issue_has_label(issue_number, LABEL_MERGED):
                if not _set_labels(
                    issue_number,
                    add=[LABEL_MERGE_BLOCKED],
                    log_prefix="cai merge",
                ):
                    print(
                        f"[cai merge] WARNING: failed to add "
                        f":merge-blocked label to #{issue_number} "
                        f"after close failure on PR #{pr_number}",
                        file=sys.stderr, flush=True,
                    )
            apply_pr_transition(
                pr_number, "approved_to_human",
                log_prefix="cai merge",
            )
            log_run("merge", repo=REPO, pr=pr_number,
                    duration=dur(), result="close_failed", exit=0)
            return 0
    elif action == "merge" and verdict_rank >= threshold_rank:
        print(
            f"[cai merge] PR #{pr_number}: verdict={confidence} "
            f">= threshold={_MERGE_THRESHOLD}; merging",
            flush=True,
        )
        merge_result = _run(
            ["gh", "pr", "merge", str(pr_number),
             "--repo", REPO, "--merge", "--delete-branch"],
            capture_output=True,
        )
        if merge_result.returncode == 0:
            print(
                f"[cai merge] PR #{pr_number}: merged successfully",
                flush=True,
            )
            if not _set_labels(
                issue_number,
                add=[LABEL_MERGED],
                remove=[LABEL_PR_OPEN, LABEL_MERGE_BLOCKED, LABEL_REVISING],
                log_prefix="cai merge",
            ):
                print(
                    f"[cai merge] WARNING: label transition to :merged "
                    f"failed for #{issue_number} after merging PR "
                    f"#{pr_number}; retrying",
                    flush=True,
                )
                if not _set_labels(
                    issue_number,
                    add=[LABEL_MERGED],
                    remove=[LABEL_PR_OPEN, LABEL_MERGE_BLOCKED,
                            LABEL_REVISING],
                    log_prefix="cai merge",
                ):
                    print(
                        f"[cai merge] WARNING: label transition to "
                        f":merged failed twice for #{issue_number} — "
                        f"issue may be stuck without a lifecycle label",
                        file=sys.stderr, flush=True,
                    )
                    # PR is already merged on GitHub side (gh merge
                    # returned 0). State has moved to MERGED — no loop
                    # risk, but flag for human attention so the
                    # orphaned issue label is fixed up.
                    _pr_set_needs_human(pr_number, True)
                    log_run("merge", repo=REPO, pr=pr_number,
                            duration=dur(),
                            result="merge_label_failed", exit=0)
                    return 0
            # Apply the APPROVED → MERGED transition on the PR itself.
            apply_pr_transition(
                pr_number, "approved_to_merged",
                log_prefix="cai merge",
            )
            log_run("merge", repo=REPO, pr=pr_number,
                    duration=dur(), result="merged", exit=0)
            return 0
        else:
            print(
                f"[cai merge] PR #{pr_number}: merge failed:\n"
                f"{merge_result.stderr}",
                file=sys.stderr,
            )
            apply_pr_transition(
                pr_number, "approved_to_human",
                log_prefix="cai merge",
            )
            log_run("merge", repo=REPO, pr=pr_number,
                    duration=dur(), result="merge_failed", exit=0)
            return 0
    else:
        print(
            f"[cai merge] PR #{pr_number}: verdict={confidence} "
            f"< threshold={_MERGE_THRESHOLD}; holding",
            flush=True,
        )
        if not _issue_has_label(issue_number, LABEL_MERGED):
            if not _set_labels(
                issue_number,
                add=[LABEL_MERGE_BLOCKED],
                log_prefix="cai merge",
            ):
                print(
                    f"[cai merge] WARNING: failed to add :merge-blocked "
                    f"label to #{issue_number} for held PR #{pr_number}",
                    file=sys.stderr, flush=True,
                )
        apply_pr_transition(
            pr_number, "approved_to_human",
            log_prefix="cai merge",
        )
        log_run("merge", repo=REPO, pr=pr_number,
                duration=dur(), result="held", exit=0)
        return 0
