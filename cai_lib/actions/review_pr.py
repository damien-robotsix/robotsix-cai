"""cai_lib.actions.review_pr — handler for PRState.REVIEWING_CODE.

Invoked by the FSM dispatcher after it has fetched an open PR and
verified its state is ``PRState.REVIEWING_CODE``. Runs the
``cai-review-pr`` agent against the PR branch, posts findings as a PR
comment (creating GitHub issues for out-of-scope findings), and
transitions the PR to ``REVISION_PENDING`` (if findings) or
``REVIEWING_DOCS`` (if clean).

Derived from ``cmd_review_pr`` in ``cai.py``. Byte-identical behavior
for the single-PR path; the multi-PR discovery loop and direct
``--pr`` targeting branch are dropped — the dispatcher hands a single
PR dict.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from cai_lib.cmd_helpers import (
    _git,
    _work_directory_block,
    _parse_oob_issues,
    _create_oob_issues,
)
from cai_lib.config import (
    REPO,
    REVIEW_PR_PATTERN_LOG,
)
from cai_lib.actions.merge import _BOT_BRANCH_RE
from cai_lib.dispatcher import HandlerResult
from cai_lib.fsm import (
    PRState,
    fire_trigger,
    get_pr_state,
)
from cai_lib.github import _fetch_linked_issue_block
from cai_lib.logging_utils import log_run
from cai_lib.subprocess_utils import _run, _run_claude_p


# ---------------------------------------------------------------------------
# Comment heading constants (mirrored from cai.py; kept in-sync manually —
# cai.py still owns the definitions because other cai.py call sites depend on
# them).
# ---------------------------------------------------------------------------

_REVIEW_COMMENT_HEADING_FINDINGS = "## cai pre-merge review"
_REVIEW_COMMENT_HEADING_CLEAN = "## cai pre-merge review (clean)"


def _log_review_pr_findings(pr_number: int, head_sha: str, agent_output: str) -> None:
    """Append one JSON line recording the finding categories for a PR review.

    Silently no-ops on any I/O error so logging failures never break the
    review workflow.
    """
    try:
        categories = re.findall(r"### Finding:\s*(\w+)", agent_output)
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        entry = {
            "ts": ts,
            "pr": pr_number,
            "sha": head_sha[:8],
            "categories": categories,
        }
        REVIEW_PR_PATTERN_LOG.parent.mkdir(parents=True, exist_ok=True)
        with REVIEW_PR_PATTERN_LOG.open("a") as fh:
            fh.write(json.dumps(entry) + "\n")
            fh.flush()
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------


def handle_review_pr(pr: dict) -> HandlerResult:
    """Review a single PR handed by the dispatcher (state REVIEWING_CODE).

    Runs the ``cai-review-pr`` agent against a fresh clone of the PR
    branch, posts findings (or a clean comment) on the PR, creates
    GitHub issues for out-of-scope findings, and transitions the PR
    via the FSM:

    - findings → ``reviewing_code_to_revision_pending``
    - clean    → ``reviewing_code_to_reviewing_docs``

    Returns an integer exit code (0 on success; non-zero only on
    unrecoverable PR-lookup-style failures — the single-PR path in
    the original ``cmd_review_pr`` always returned 0 after the loop).
    """
    print("[cai review-pr] checking open PRs against main", flush=True)
    t0 = time.monotonic()

    reviewed = 0
    skipped = 0

    pr_number = pr["number"]
    head_sha = pr["headRefOid"]
    branch = pr.get("headRefName", "")
    title = pr["title"]
    pending_transition = ""

    # Check if we already posted a review for this SHA. Match
    # either heading variant (findings or clean) — both include
    # the head SHA after the em-dash, so a substring check on
    # `head_sha` against the comment's first line is enough.
    already_reviewed = False
    for comment in pr.get("comments", []):
        body = (comment.get("body") or "")
        first_line = body.split("\n", 1)[0]
        if (
            first_line.startswith(_REVIEW_COMMENT_HEADING_FINDINGS)
            and head_sha in first_line
        ):
            already_reviewed = True
            break
    if already_reviewed:
        print(
            f"[cai review-pr] PR #{pr_number}: already reviewed at {head_sha[:8]}; skipping",
            flush=True,
        )
        skipped += 1
        dur = f"{int(time.monotonic() - t0)}s"
        print(
            f"[cai review-pr] reviewed={reviewed} skipped={skipped}",
            flush=True,
        )
        log_run("review_pr", repo=REPO, reviewed=reviewed, skipped=skipped,
                duration=dur, exit=0)
        return HandlerResult(trigger="")

    print(f"[cai review-pr] reviewing PR #{pr_number}: {title}", flush=True)

    # Clone the repo and check out the PR branch so the agent can
    # explore changed files directly via Read/Grep/Glob.
    _uid = uuid.uuid4().hex[:8]
    work_dir = Path(f"/tmp/cai-review-{pr_number}-{_uid}")
    try:
        if work_dir.exists():
            shutil.rmtree(work_dir)

        _run(["gh", "auth", "setup-git"], capture_output=True)
        clone = _run(
            ["gh", "repo", "clone", REPO, str(work_dir)],
            capture_output=True,
        )
        if clone.returncode != 0:
            print(
                f"[cai review-pr] clone failed for PR #{pr_number}:\n{clone.stderr}",
                file=sys.stderr,
            )
            dur = f"{int(time.monotonic() - t0)}s"
            print(
                f"[cai review-pr] reviewed={reviewed} skipped={skipped}",
                flush=True,
            )
            log_run("review_pr", repo=REPO, reviewed=reviewed, skipped=skipped,
                    duration=dur, exit=0)
            return HandlerResult(trigger="")
        if branch:
            _git(work_dir, "fetch", "origin", branch)
            _git(work_dir, "checkout", branch)

        # Compute a --stat summary as a file-level map for the agent.
        # The full unified diff is intentionally omitted — it is a
        # large token sink and the agent can read changed files
        # directly from the clone via Read/Grep/Glob.
        stat_result = _git(
            work_dir, "diff", "origin/main..HEAD", "--stat",
            check=False,
        )
        pr_stat = (stat_result.stdout or "").strip() or (
            "(no changes vs origin/main)"
        )

        # Build the user message. The system prompt, tool
        # allowlist (Read/Grep/Glob), and hard rules all
        # live in `.claude/agents/cai-review-pr.md`. The wrapper
        # passes the work-directory block (so the agent knows
        # where the cloned PR is) plus the dynamic per-run
        # context via stdin (#342).
        author_login = pr.get("author", {}).get("login", "unknown")
        issue_block = _fetch_linked_issue_block(pr.get("body", ""))
        user_message = (
            _work_directory_block(work_dir)
            + "\n"
            + "## PR metadata\n\n"
            + f"- **Number:** #{pr_number}\n"
            + f"- **Title:** {title}\n"
            + f"- **Author:** @{author_login}\n"
            + "- **Base:** main\n"
            + f"- **HEAD SHA:** {head_sha}\n\n"
            + issue_block
            + "## PR changes (stat summary)\n\n"
            + f"```\n{pr_stat}\n```\n\n"
            + "The full unified diff is **not** included — it is a "
            + "large token sink. The PR branch is checked out in the "
            + f"work directory at `{work_dir}`. Use `Read`, `Grep`, "
            + "and `Glob` to explore changed files directly.\n"
        )

        # Invoke the declared cai-review-pr subagent.
        # Runs with `cwd=/app` and `--add-dir <work_dir>` (#342)
        # so it reads its definition + memory from the canonical
        # /app paths while reviewing the cloned PR via absolute
        # paths.
        agent = _run_claude_p(
            ["claude", "-p", "--agent", "cai-review-pr",
             "--permission-mode", "acceptEdits",
             "--max-budget-usd", "0.50",
             "--allowedTools", "Read,Grep,Glob",
             "--add-dir", str(work_dir)],
            category="review-pr",
            agent="cai-review-pr",
            input=user_message,
            cwd="/app",
        )
        if agent.stdout:
            print(agent.stdout, flush=True)

        agent_output = (agent.stdout or "").strip()

        # A nonzero exit combined with a usable verdict in stdout
        # happens when the run hits --max-budget-usd after the
        # agent already produced its final answer (the result
        # envelope arrives with subtype=error_max_budget_usd and
        # no `result` field, so _run_claude_p salvages the last
        # assistant text). Post the salvaged verdict instead of
        # discarding the work.
        has_verdict = (
            "### Finding:" in agent_output
            or "No ripple effects found" in agent_output
        )
        if agent.returncode != 0 and not has_verdict:
            print(
                f"[cai review-pr] agent failed for PR #{pr_number} "
                f"(exit {agent.returncode}):\n{agent.stderr}",
                file=sys.stderr,
            )
            dur = f"{int(time.monotonic() - t0)}s"
            print(
                f"[cai review-pr] reviewed={reviewed} skipped={skipped}",
                flush=True,
            )
            log_run("review_pr", repo=REPO, reviewed=reviewed, skipped=skipped,
                    duration=dur, exit=0)
            return HandlerResult(trigger="")
        if agent.returncode != 0 and has_verdict:
            print(
                f"[cai review-pr] agent exited {agent.returncode} "
                f"for PR #{pr_number} but produced a verdict; salvaging",
                flush=True,
            )

        # Parse and create any out-of-scope issues emitted by the agent,
        # then strip them from agent_output so they don't appear in the
        # PR comment.
        oob_issues = _parse_oob_issues(agent_output)
        if oob_issues:
            _create_oob_issues(oob_issues, pr_number, "cai review-pr")
            agent_output = re.sub(
                r"^## Out-of-scope Issue\s*\n.*?(?=^## Out-of-scope Issue|\Z)",
                "",
                agent_output,
                flags=re.MULTILINE | re.DOTALL,
            ).strip()

        # Determine if there are findings.
        has_findings = (
            "### Finding:" in agent_output
            and "No ripple effects found" not in agent_output
        )

        if has_findings:
            # Findings comments use the actionable heading form
            # so the revise subagent picks them up on its next
            # tick (`_BOT_COMMENT_MARKERS` does NOT match this).
            comment_body = (
                f"{_REVIEW_COMMENT_HEADING_FINDINGS} \u2014 {head_sha}\n\n"
                f"{agent_output}\n\n"
                f"---\n"
                f"_Pre-merge consistency review by `cai review-pr`. "
                f"Address the findings above or explain why they don't "
                f"apply, then push a new commit to trigger a re-review._"
            )
        else:
            # Clean comments use the (clean) heading variant so
            # `_BOT_COMMENT_MARKERS` filters them out — no need
            # for revise to act on a "no findings" report.
            comment_body = (
                f"{_REVIEW_COMMENT_HEADING_CLEAN} \u2014 {head_sha}\n\n"
                f"No ripple effects found.\n\n"
                f"---\n"
                f"_Pre-merge consistency review by `cai review-pr`._"
            )

        _run(
            ["gh", "pr", "comment", str(pr_number),
             "--repo", REPO, "--body", comment_body],
            capture_output=True,
        )

        # Advance FSM state based on review outcome. PRs entering
        # review without a pipeline label (OPEN) are first bumped
        # to REVIEWING_CODE so the outgoing transition's from_state
        # check passes.  Apply the same branch-name guard as
        # handle_open_to_review so that a non-bot-branch PR that
        # somehow reaches review (e.g. handle_open_to_review failed
        # to transition) is parked correctly instead of being moved
        # to REVIEWING_CODE.
        current_state = get_pr_state(pr)
        if current_state == PRState.OPEN:
            branch = pr.get("headRefName", "") or ""
            if not _BOT_BRANCH_RE.match(branch):
                return HandlerResult(
                    trigger="open_to_human",
                    divert_reason=(
                        f"Non-bot-branch PR (branch={branch!r}) cannot "
                        f"be auto-merged; requires manual review."
                    ),
                )
            # Normalize OPEN → REVIEWING_CODE so the subsequent
            # transition's from_state check passes. Fired here
            # directly because the driver translates the handler's
            # single returned HandlerResult into one fire_trigger
            # call — the second transition below is the one that
            # rides on that return.
            fire_trigger(
                pr_number, "open_to_reviewing_code",
                is_pr=True,
                log_prefix="cai review-pr",
            )
        pending_transition = (
            "reviewing_code_to_revision_pending"
            if has_findings
            else "reviewing_code_to_reviewing_docs"
        )

        _log_review_pr_findings(pr_number, head_sha, agent_output)

        finding_word = "with findings" if has_findings else "clean"
        print(
            f"[cai review-pr] posted review on PR #{pr_number} ({finding_word})",
            flush=True,
        )
        reviewed += 1

    except Exception as e:
        print(
            f"[cai review-pr] unexpected failure for PR #{pr_number}: {e!r}",
            file=sys.stderr,
        )
    finally:
        if work_dir.exists():
            shutil.rmtree(work_dir, ignore_errors=True)

    dur = f"{int(time.monotonic() - t0)}s"
    print(
        f"[cai review-pr] reviewed={reviewed} skipped={skipped}",
        flush=True,
    )
    log_run("review_pr", repo=REPO, reviewed=reviewed, skipped=skipped,
            duration=dur, exit=0)
    return HandlerResult(trigger=pending_transition)
