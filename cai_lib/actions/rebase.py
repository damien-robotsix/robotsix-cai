"""cai_lib.actions.rebase — handler for PRState.REBASING.

Invoked by the FSM dispatcher when a PR is at PRState.REBASING (the
dispatcher routes any pre-merge PR with ``mergeable == "CONFLICTING"``
or ``mergeStateStatus == "DIRTY"`` here regardless of the PR's existing
pipeline label, applying the corresponding ``*_to_rebasing`` transition
first).

The handler clones the PR branch, attempts ``git rebase origin/main``,
hands any remaining conflicts to the ``cai-rebase`` agent, force-pushes
the result (or aborts cleanly), then ALWAYS transitions to
``PRState.REVIEWING_CODE`` regardless of outcome. The rebase outcome is
posted as a PR comment so the next reviewer (handle_review_pr) sees:

  - On success: ``## cai rebase attempt — <new SHA>: success`` plus a
    short summary of what changed.
  - On failure: ``## cai rebase attempt — <SHA>: failed`` plus the
    conflict-file list and any agent stderr. The reviewer then decides
    whether to leave findings (forcing revise) or escalate to human.

This handler does NOT decide between "approve / human-needed" — it is a
pre-step. The dispatch loop owns the decision via the next tick.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

from cai_lib.config import REPO
from cai_lib.dispatcher import HandlerResult
from cai_lib.subagent import _run_claude_p
from cai_lib.subprocess_utils import _run
from cai_lib.cmd_helpers import _git, _gh_user_identity, _work_directory_block
from cai_lib.github import _fetch_linked_issue_block
from cai_lib.logging_utils import log_run


_REBASE_COMMENT_HEADING_PREFIX  = "## cai rebase attempt"
_REBASE_COMMENT_HEADING_SUCCESS = "## cai rebase attempt — {sha}: success"
_REBASE_COMMENT_HEADING_FAILED  = "## cai rebase attempt — {sha}: failed"


def _rebase_conflict_files(work_dir: Path) -> list[str]:
    """Return the list of files with unresolved merge conflicts."""
    res = _git(work_dir, "diff", "--name-only", "--diff-filter=U", check=False)
    return [line.strip() for line in (res.stdout or "").splitlines() if line.strip()]


def _post_pr_comment(pr_number: int, body: str) -> None:
    """Best-effort comment post; never raises."""
    try:
        _run(
            ["gh", "pr", "comment", str(pr_number),
             "--repo", REPO, "--body", body],
            capture_output=True,
        )
    except Exception as exc:
        print(f"[cai rebase] PR #{pr_number}: comment post failed: {exc}",
              file=sys.stderr)


_REBASE_EXIT_RESULT = HandlerResult(trigger="rebasing_to_reviewing_code")


def handle_rebase(pr: dict) -> HandlerResult:
    """Attempt a rebase against origin/main and bounce back to REVIEWING_CODE.

    Always returns ``HandlerResult(trigger="rebasing_to_reviewing_code")``
    so the next tick re-reviews the (possibly updated) branch. The
    success-vs-failure of the rebase itself is captured in the PR
    comment for the reviewer to consume.
    """
    t0 = time.monotonic()

    pr_number = pr["number"]
    branch = pr.get("headRefName", "")
    title = pr.get("title", "")

    print(f"[cai rebase] targeting PR #{pr_number}: {title}", flush=True)

    if not branch:
        print(f"[cai rebase] PR #{pr_number}: missing headRefName; bouncing",
              file=sys.stderr)
        log_run("rebase", repo=REPO, pr=pr_number, result="missing_branch", exit=0)
        return _REBASE_EXIT_RESULT

    _uid = uuid.uuid4().hex[:8]
    work_dir = Path(f"/tmp/cai-rebase-{pr_number}-{_uid}")

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
                f"[cai rebase] clone failed for PR #{pr_number}:\n{clone.stderr}",
                file=sys.stderr,
            )
            log_run("rebase", repo=REPO, pr=pr_number,
                    result="clone_failed", exit=0)
            return _REBASE_EXIT_RESULT

        _git(work_dir, "fetch", "origin", "main")
        _git(work_dir, "fetch", "origin", branch)
        _git(work_dir, "checkout", branch)

        name, email = _gh_user_identity()
        _git(work_dir, "config", "user.name", name)
        _git(work_dir, "config", "user.email", email)

        # First-pass deterministic rebase. If it succeeds outright, push
        # and exit — no agent needed.
        rebase = _git(work_dir, "rebase", "origin/main", check=False)
        rebase_in_progress = (
            (work_dir / ".git" / "rebase-merge").exists()
            or (work_dir / ".git" / "rebase-apply").exists()
        )

        if rebase.returncode == 0 and not rebase_in_progress:
            new_sha = _git(work_dir, "rev-parse", "HEAD").stdout.strip()
            push = _run(
                ["git", "-C", str(work_dir),
                 "push", "--force-with-lease", "origin", branch],
                capture_output=True,
            )
            if push.returncode != 0:
                print(
                    f"[cai rebase] push failed for PR #{pr_number}:\n{push.stderr}",
                    file=sys.stderr,
                )
                _post_pr_comment(
                    pr_number,
                    _REBASE_COMMENT_HEADING_FAILED.format(sha=new_sha[:8])
                    + "\n\nDeterministic rebase succeeded but the force-push "
                    "was rejected.\n\n```\n" + (push.stderr or "")
                    + "\n```\n\n---\n_Posted by `cai rebase`._",
                )
                log_run("rebase", repo=REPO, pr=pr_number,
                        result="push_failed", exit=0)
                return _REBASE_EXIT_RESULT

            _post_pr_comment(
                pr_number,
                _REBASE_COMMENT_HEADING_SUCCESS.format(sha=new_sha[:8])
                + f"\n\nDeterministic rebase against `origin/main` succeeded; "
                f"new HEAD is `{new_sha}`.\n\n---\n_Posted by `cai rebase`._",
            )
            dur = f"{int(time.monotonic() - t0)}s"
            log_run("rebase", repo=REPO, pr=pr_number, duration=dur,
                    result="auto_success", exit=0)
            return _REBASE_EXIT_RESULT

        # Conflicts exist (or rebase aborted). Hand to cai-rebase agent.
        conflict_files = _rebase_conflict_files(work_dir)
        _linked_issue_block = _fetch_linked_issue_block(pr.get("body", ""))
        user_message = (
            _work_directory_block(work_dir, _linked_issue_block)
            + "\n"
            + "## Rebase conflict\n\n"
            + f"PR #{pr_number} (`{branch}`) cannot be merged onto `origin/main` "
              "without conflict resolution.\n\n"
            + "A `git rebase origin/main` was attempted in the work directory. "
              "It left the following files in conflict (or aborted):\n\n"
            + "```\n" + ("\n".join(conflict_files) or "(rebase aborted; no conflict files reported)") + "\n```\n\n"
            + "Resolve the conflicts file by file, then complete the rebase "
              "with `git rebase --continue`. If the conflicts are not safely "
              "resolvable, run `git rebase --abort` and exit non-zero.\n"
        )

        agent = _run_claude_p(
            ["claude", "-p", "--agent", "cai-rebase",
             "--dangerously-skip-permissions",
             "--add-dir", str(work_dir)],
            category="rebase",
            agent="cai-rebase",
            input=user_message,
            cwd="/app",
            target_kind="pr",
            target_number=pr_number,
        )
        if agent.stdout:
            print(agent.stdout, flush=True)

        # Re-check rebase state after agent run.
        rebase_in_progress = (
            (work_dir / ".git" / "rebase-merge").exists()
            or (work_dir / ".git" / "rebase-apply").exists()
        )
        remaining_conflicts = _rebase_conflict_files(work_dir)
        agent_failed = (agent.returncode != 0)

        if rebase_in_progress or remaining_conflicts or agent_failed:
            # Aborted or unresolved. Make sure the work dir is clean and
            # do NOT push anything; the branch on origin is unchanged.
            if rebase_in_progress:
                _git(work_dir, "rebase", "--abort", check=False)
            head_sha = _git(work_dir, "rev-parse", "HEAD",
                            check=False).stdout.strip() or "unknown"
            stderr_excerpt = (agent.stderr or "")[:2000]
            _post_pr_comment(
                pr_number,
                _REBASE_COMMENT_HEADING_FAILED.format(sha=head_sha[:8])
                + "\n\ncai-rebase could not resolve the conflicts cleanly; "
                "the branch on origin is unchanged.\n\n"
                + "**Conflict files seen during rebase:**\n```\n"
                + ("\n".join(conflict_files) or "(none reported)")
                + "\n```\n\n"
                + ("**Agent stderr (truncated):**\n```\n"
                   + stderr_excerpt + "\n```\n\n" if stderr_excerpt else "")
                + "---\n_Posted by `cai rebase`. The next review tick will "
                "see this comment; expect findings or a human-needed divert._",
            )
            dur = f"{int(time.monotonic() - t0)}s"
            log_run("rebase", repo=REPO, pr=pr_number, duration=dur,
                    result="agent_failed", conflict_count=len(conflict_files),
                    exit=0)
            return _REBASE_EXIT_RESULT

        # Agent claims success — sanity check by confirming origin/main is
        # an ancestor of HEAD, then push.
        ancestry = _git(work_dir, "merge-base",
                        "--is-ancestor", "origin/main", "HEAD", check=False)
        if ancestry.returncode != 0:
            head_sha = _git(work_dir, "rev-parse", "HEAD",
                            check=False).stdout.strip() or "unknown"
            _post_pr_comment(
                pr_number,
                _REBASE_COMMENT_HEADING_FAILED.format(sha=head_sha[:8])
                + "\n\ncai-rebase exited cleanly but `origin/main` is not an "
                "ancestor of HEAD. Refusing to push a non-rebased branch.\n\n"
                "---\n_Posted by `cai rebase`._",
            )
            dur = f"{int(time.monotonic() - t0)}s"
            log_run("rebase", repo=REPO, pr=pr_number, duration=dur,
                    result="ancestry_check_failed", exit=0)
            return _REBASE_EXIT_RESULT

        new_sha = _git(work_dir, "rev-parse", "HEAD").stdout.strip()
        push = _run(
            ["git", "-C", str(work_dir),
             "push", "--force-with-lease", "origin", branch],
            capture_output=True,
        )
        if push.returncode != 0:
            _post_pr_comment(
                pr_number,
                _REBASE_COMMENT_HEADING_FAILED.format(sha=new_sha[:8])
                + "\n\nRebase succeeded locally but force-push was rejected.\n\n"
                + "```\n" + (push.stderr or "") + "\n```\n\n"
                + "---\n_Posted by `cai rebase`._",
            )
            dur = f"{int(time.monotonic() - t0)}s"
            log_run("rebase", repo=REPO, pr=pr_number, duration=dur,
                    result="push_failed", exit=0)
            return _REBASE_EXIT_RESULT

        _post_pr_comment(
            pr_number,
            _REBASE_COMMENT_HEADING_SUCCESS.format(sha=new_sha[:8])
            + f"\n\ncai-rebase resolved the conflicts and rebased onto "
            f"`origin/main`. New HEAD is `{new_sha}`.\n\n"
            + "---\n_Posted by `cai rebase`._",
        )
        dur = f"{int(time.monotonic() - t0)}s"
        log_run("rebase", repo=REPO, pr=pr_number, duration=dur,
                result="agent_success", exit=0)
        return _REBASE_EXIT_RESULT

    except Exception as exc:
        print(f"[cai rebase] unexpected error on PR #{pr_number}: {exc}",
              file=sys.stderr)
        log_run("rebase", repo=REPO, pr=pr_number,
                result="unexpected_error", exit=1)
        return _REBASE_EXIT_RESULT
    finally:
        if work_dir.exists():
            shutil.rmtree(work_dir, ignore_errors=True)
