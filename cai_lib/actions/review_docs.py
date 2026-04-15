"""cai_lib.actions.review_docs — handler for PRState.REVIEWING_DOCS.

Invoked by the FSM dispatcher after it has fetched an open PR and
verified its state is ``PRState.REVIEWING_DOCS``. Runs the
``cai-review-docs`` agent against a clone of the PR branch; either
commits + pushes doc fixes (and re-enters code review) or posts a
"clean" review and advances the PR to ``PRState.APPROVED``. The
final ``approved_to_merged`` step is owned by
``cai_lib.actions.merge``.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

from cai_lib.config import REPO
from cai_lib.fsm import apply_pr_transition, get_pr_state, PRState
from cai_lib.github import _gh_json, _fetch_linked_issue_block
from cai_lib.subprocess_utils import _run, _run_claude_p
from cai_lib.cmd_helpers import _git, _gh_user_identity, _work_directory_block
from cai_lib.logging_utils import log_run


# Docs-review comment headings. Duplicated from cai.py because cmd_merge
# still reads the same constants there — keep the strings in sync until
# cmd_merge is extracted.
_DOCS_REVIEW_COMMENT_HEADING_PREFIX = "## cai docs review"
_DOCS_REVIEW_COMMENT_HEADING_CLEAN = "## cai docs review (clean)"
_DOCS_REVIEW_COMMENT_HEADING_APPLIED = "## cai docs review (applied)"


def handle_review_docs(pr: dict) -> int:
    """Run cai-review-docs on *pr* (already at PRState.REVIEWING_DOCS)."""
    t0 = time.monotonic()

    pr_number = pr["number"]
    head_sha = pr["headRefOid"]
    branch = pr.get("headRefName", "")
    title = pr["title"]

    print(f"[cai review-docs] targeting PR #{pr_number}: {title}", flush=True)

    # Idempotency: skip if we already posted a docs review for this SHA.
    for comment in pr.get("comments", []):
        body = (comment.get("body") or "")
        first_line = body.split("\n", 1)[0]
        if (
            first_line.startswith(_DOCS_REVIEW_COMMENT_HEADING_PREFIX)
            and head_sha in first_line
        ):
            print(
                f"[cai review-docs] PR #{pr_number}: already reviewed at "
                f"{head_sha[:8]}; skipping",
                flush=True,
            )
            log_run("review_docs", repo=REPO, pr=pr_number,
                    result="already_reviewed", exit=0)
            return 0

    # State gate (defensive — dispatcher should already have verified).
    if get_pr_state(pr) != PRState.REVIEWING_DOCS:
        print(
            f"[cai review-docs] PR #{pr_number}: not in REVIEWING_DOCS "
            f"state; waiting",
            flush=True,
        )
        log_run("review_docs", repo=REPO, pr=pr_number,
                result="wrong_state", exit=0)
        return 0

    _uid = uuid.uuid4().hex[:8]
    work_dir = Path(f"/tmp/cai-review-docs-{pr_number}-{_uid}")
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
                f"[cai review-docs] clone failed for PR #{pr_number}:\n"
                f"{clone.stderr}",
                file=sys.stderr,
            )
            dur = f"{int(time.monotonic() - t0)}s"
            log_run("review_docs", repo=REPO, pr=pr_number,
                    duration=dur, result="clone_failed", exit=1)
            return 1

        _git(work_dir, "fetch", "origin", branch)
        _git(work_dir, "checkout", branch)

        # Configure git identity so the agent can commit.
        name, email = _gh_user_identity()
        _git(work_dir, "config", "user.name", name)
        _git(work_dir, "config", "user.email", email)

        # --stat summary serves as the file-level map for the agent;
        # the full diff is intentionally omitted (token sink).
        stat_result = _git(
            work_dir, "diff", "origin/main..HEAD", "--stat",
            check=False,
        )
        pr_stat = (stat_result.stdout or "").strip() or (
            "(no changes vs origin/main)"
        )

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
            + "`Glob`, `Edit`, and `Write` to inspect and fix files "
            + "directly.\n"
        )

        agent = _run_claude_p(
            ["claude", "-p", "--agent", "cai-review-docs",
             "--permission-mode", "acceptEdits",
             "--max-budget-usd", "0.50",
             "--add-dir", str(work_dir)],
            category="review-docs",
            agent="cai-review-docs",
            input=user_message,
            cwd="/app",
        )
        if agent.stdout:
            print(agent.stdout, flush=True)
        if agent.returncode != 0:
            print(
                f"[cai review-docs] agent failed for PR #{pr_number} "
                f"(exit {agent.returncode}):\n{agent.stderr}",
                file=sys.stderr,
            )
            dur = f"{int(time.monotonic() - t0)}s"
            log_run("review_docs", repo=REPO, pr=pr_number,
                    duration=dur, result="agent_failed",
                    exit=agent.returncode)
            return agent.returncode

        agent_output = (agent.stdout or "").strip()

        # Did the agent make any doc changes?
        status_result = _git(work_dir, "status", "--porcelain", check=False)
        has_doc_changes = bool(status_result.stdout.strip())

        if has_doc_changes:
            _git(work_dir, "add", "-A")
            _git(work_dir, "commit", "-m",
                 "docs: update documentation per review-docs\n\n"
                 "Applied by cai review-docs.")
            push = _run(
                ["git", "-C", str(work_dir), "push", "origin", branch],
                capture_output=True,
            )
            if push.returncode != 0:
                print(
                    f"[cai review-docs] push failed for PR #{pr_number}:\n"
                    f"{push.stderr}",
                    file=sys.stderr,
                )
                dur = f"{int(time.monotonic() - t0)}s"
                log_run("review_docs", repo=REPO, pr=pr_number,
                        duration=dur, result="push_failed", exit=1)
                return 1
            new_sha = _git(work_dir, "rev-parse", "HEAD").stdout.strip()
            comment_body = (
                f"{_DOCS_REVIEW_COMMENT_HEADING_APPLIED} \u2014 {new_sha}\n\n"
                f"{agent_output}\n\n"
                f"---\n"
                f"_Documentation updated automatically by `cai review-docs`._"
            )
            print(
                f"[cai review-docs] pushed doc fixes to PR #{pr_number}",
                flush=True,
            )
        else:
            comment_body = (
                f"{_DOCS_REVIEW_COMMENT_HEADING_CLEAN} \u2014 {head_sha}\n\n"
                f"No documentation updates needed.\n\n"
                f"---\n"
                f"_Pre-merge documentation review by `cai review-docs`._"
            )

        _run(
            ["gh", "pr", "comment", str(pr_number),
             "--repo", REPO, "--body", comment_body],
            capture_output=True,
        )

        # Advance FSM state based on docs review outcome.
        if has_doc_changes:
            # Docs fix pushed — re-enter code review on the new HEAD.
            apply_pr_transition(
                pr_number, "reviewing_docs_to_reviewing_code",
                log_prefix="cai review-docs",
            )
            result_word = "fixes pushed"
            result_tag = "fixes_pushed"
        else:
            # Clean — mark the PR :approved so actions/merge can own
            # the final approved_to_merged step. (Replaces the old
            # terminal reviewing_docs_to_merged transition.)
            apply_pr_transition(
                pr_number, "reviewing_docs_to_approved",
                log_prefix="cai review-docs",
            )
            result_word = "clean"
            result_tag = "clean"

        print(
            f"[cai review-docs] posted review on PR #{pr_number} "
            f"({result_word})",
            flush=True,
        )
        dur = f"{int(time.monotonic() - t0)}s"
        log_run("review_docs", repo=REPO, pr=pr_number,
                duration=dur, result=result_tag, exit=0)
        return 0

    except subprocess.CalledProcessError as e:
        print(
            f"[cai review-docs] subprocess failure for PR #{pr_number}: "
            f"{e!r}",
            file=sys.stderr,
        )
        dur = f"{int(time.monotonic() - t0)}s"
        log_run("review_docs", repo=REPO, pr=pr_number,
                duration=dur, result="subprocess_error", exit=1)
        return 1
    except Exception as e:
        print(
            f"[cai review-docs] unexpected failure for PR #{pr_number}: "
            f"{e!r}",
            file=sys.stderr,
        )
        dur = f"{int(time.monotonic() - t0)}s"
        log_run("review_docs", repo=REPO, pr=pr_number,
                duration=dur, result="unexpected_error", exit=1)
        return 1
    finally:
        if work_dir.exists():
            shutil.rmtree(work_dir, ignore_errors=True)
