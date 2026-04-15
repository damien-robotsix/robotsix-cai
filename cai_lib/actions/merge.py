"""cai_lib.actions.merge — handler for PRState.APPROVED.

Invoked by the FSM dispatcher after it has fetched an open PR and
verified its state is ``PRState.APPROVED``. Runs the ``cai-merge``
agent to obtain a confidence-gated verdict and, on a high-enough
merge verdict, squash-merges via ``gh pr merge``. On new commits
arriving since the APPROVED label was set, diverts back to code
review. On low-confidence / refusal / merge failure, tags the PR
with ``needs-human-review``.
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
    LABEL_NO_ACTION,
    LABEL_MERGE_BLOCKED,
    LABEL_REVISING,
    LABEL_PLAN_APPROVED,
    LABEL_PR_NEEDS_HUMAN,
)
from cai_lib.fsm import apply_pr_transition, get_pr_state, PRState
from cai_lib.github import _gh_json, _set_labels, _issue_has_label
from cai_lib.subprocess_utils import _run, _run_claude_p
from cai_lib.cmd_helpers import (
    _parse_merge_verdict,
    _pr_set_needs_human,
    _parse_iso_ts,
    _is_bot_comment,
    _filter_unaddressed_comments,
    _fetch_review_comments,
)
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

# Heading of the docs-review "clean" marker used to verify the docs
# review that earned APPROVED is still valid at the current HEAD.
_DOCS_REVIEW_COMMENT_HEADING_CLEAN = "## cai docs review (clean)"

# Bot-PR branch prefix regex. Only PRs on ``auto-improve/<issue>-…``
# branches are eligible for auto-merge.
_BOT_BRANCH_RE = re.compile(r"^auto-improve/(\d+)-")

# Truncate very large diffs before feeding the merge agent to bound
# token cost per PR.
_MERGE_MAX_DIFF_LEN = 40_000


def handle_merge(pr: dict) -> int:
    """Confidence-gated auto-merge for a single APPROVED bot PR.

    The dispatcher has already resolved *pr* to state
    ``PRState.APPROVED``. This handler either:

    * merges the PR (``approved_to_merged``), or
    * diverts back to code review when new commits have arrived
      (``approved_to_reviewing_code``), or
    * tags the PR ``needs-human-review`` when the merge agent
      refuses / yields low confidence / merge itself fails.
    """
    print("[cai merge] evaluating APPROVED PR", flush=True)
    t0 = time.monotonic()

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

    # New-commits-arrived check: the clean docs-review comment that
    # earned APPROVED was pinned to a specific HEAD SHA. If no clean
    # comment exists for the *current* HEAD, the branch has advanced
    # since APPROVED was applied — divert back to code review so the
    # pipeline re-enters review on the new SHA.
    docs_clean_at_head = False
    for comment in pr.get("comments", []):
        body_line = (comment.get("body") or "").split("\n", 1)[0]
        if (
            body_line.startswith(_DOCS_REVIEW_COMMENT_HEADING_CLEAN)
            and head_sha in body_line
        ):
            docs_clean_at_head = True
            break
    if not docs_clean_at_head:
        print(
            f"[cai merge] PR #{pr_number}: new commits since APPROVED "
            f"(HEAD {head_sha[:8]} has no clean docs review); "
            f"diverting to reviewing-code",
            flush=True,
        )
        apply_pr_transition(
            pr_number, "approved_to_reviewing_code",
            log_prefix="cai merge",
        )
        log_run("merge", repo=REPO, pr=pr_number,
                result="new_commits_divert", exit=0)
        return 0

    # Safety filter 3: unaddressed review comments → let revise handle.
    # Mirror the revise subcommand's filter logic via the shared helper
    # so a "no additional changes" reply correctly suppresses the loop.
    all_comments = list(pr.get("comments", []))
    try:
        all_comments.extend(_fetch_review_comments(pr_number))
    except Exception:
        pass

    # Fetch the most recent commit timestamp on the branch.
    try:
        commits = _gh_json([
            "pr", "view", str(pr_number),
            "--repo", REPO,
            "--json", "commits",
        ])
        commit_list = commits.get("commits", [])
        last_commit_date = (
            commit_list[-1].get("committedDate", "") if commit_list else ""
        )
    except (subprocess.CalledProcessError, KeyError):
        last_commit_date = ""

    commit_ts = _parse_iso_ts(last_commit_date)
    unaddressed = (
        _filter_unaddressed_comments(all_comments, commit_ts)
        if commit_ts is not None
        else []
    )
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
    pr_diff = diff_result.stdout
    if len(pr_diff) > _MERGE_MAX_DIFF_LEN:
        pr_diff = (
            pr_diff[:_MERGE_MAX_DIFF_LEN]
            + "\n... (truncated — diff exceeds size limit)"
        )

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

    agent = _run_claude_p(
        ["claude", "-p", "--agent", "cai-merge"],
        category="merge",
        agent="cai-merge",
        input=user_message,
    )
    if agent.returncode != 0:
        print(
            f"[cai merge] model failed for PR #{pr_number} "
            f"(exit {agent.returncode}):\n{agent.stderr}",
            file=sys.stderr,
        )
        log_run("merge", repo=REPO, pr=pr_number,
                result="agent_failed", exit=agent.returncode)
        return agent.returncode

    agent_output = (agent.stdout or "").strip()
    verdict = _parse_merge_verdict(agent_output)

    if not verdict:
        print(
            f"[cai merge] PR #{pr_number}: could not parse verdict; "
            f"skipping",
            flush=True,
        )
        log_run("merge", repo=REPO, pr=pr_number,
                result="verdict_unparseable", exit=0)
        return 0

    confidence = verdict["confidence"]
    action = verdict["action"]

    # Post the verdict as a PR comment.
    comment_body = (
        f"{_MERGE_COMMENT_HEADING} \u2014 {head_sha}\n\n"
        f"{agent_output}\n\n"
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
            if not _set_labels(
                issue_number,
                add=[LABEL_NO_ACTION],
                remove=[LABEL_PR_OPEN, LABEL_MERGE_BLOCKED,
                        LABEL_REVISING, LABEL_PLAN_APPROVED],
                log_prefix="cai merge",
            ):
                print(
                    f"[cai merge] WARNING: label transition to "
                    f":no-action failed for #{issue_number} after "
                    f"closing PR #{pr_number}; retrying",
                    flush=True,
                )
                if not _set_labels(
                    issue_number,
                    add=[LABEL_NO_ACTION],
                    remove=[LABEL_PR_OPEN, LABEL_MERGE_BLOCKED,
                            LABEL_REVISING, LABEL_PLAN_APPROVED],
                    log_prefix="cai merge",
                ):
                    print(
                        f"[cai merge] WARNING: label transition to "
                        f":no-action failed twice for #{issue_number} "
                        f"— issue may be stuck without a lifecycle label",
                        file=sys.stderr, flush=True,
                    )
                    _pr_set_needs_human(pr_number, True)
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
            _pr_set_needs_human(pr_number, True)
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
            _pr_set_needs_human(pr_number, True)
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
        _pr_set_needs_human(pr_number, True)
        log_run("merge", repo=REPO, pr=pr_number,
                duration=dur(), result="held", exit=0)
        return 0
