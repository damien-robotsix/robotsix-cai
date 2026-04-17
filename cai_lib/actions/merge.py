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

    # Safety filter 1: only bot PRs.
    m = _BOT_BRANCH_RE.match(branch)
    if not m:
        print(
            f"[cai merge] PR #{pr_number}: non-bot branch {branch!r}; skipping",
            flush=True,
        )
        log_run("merge", repo=REPO, pr=pr_number,
                result="not_bot_branch", exit=0)
        return 0
    issue_number = int(m.group(1))

    # Safety filter 4: unmergeable PRs (conflicts).
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

    # Safety filter 2: linked issue must be in :pr-open state.
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

    # Safety filter 3: unaddressed review comments → let revise handle.
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

    user_message = (
        f"## Linked issue\n\n"
        f"### #{issue_full.get('number', issue_number)} \u2014 "
        f"{issue_full.get('title', '')}\n\n"
        f"{issue_full.get('body') or '(no body)'}\n\n"
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
