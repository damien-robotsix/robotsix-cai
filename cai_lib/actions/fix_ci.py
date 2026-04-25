"""cai_lib.actions.fix_ci — handler for PRState.CI_FAILING.

Invoked by the FSM dispatcher after it has fetched an open PR and
verified its state is ``PRState.CI_FAILING``. Fetches the latest
failed check logs, runs the ``cai-fix-ci`` agent, pushes any fix
commits, and transitions the PR back to ``REVIEWING_CODE``.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

from cai_lib.config import (
    REPO,
    LABEL_REVISING,
)
from cai_lib.dispatcher import HandlerResult
from cai_lib.fsm import PRState
from cai_lib.github import _gh_json, _set_labels
from cai_lib.claude_argv import _run_claude_p
from cai_lib.subprocess_utils import _run
from cai_lib.utils.log import log_run
from cai_lib.cmd_helpers import (
    _gh_user_identity,
    _git,
    _work_directory_block,
    _setup_agent_edit_staging,
    _apply_agent_edit_staging,
    _fetch_previous_fix_attempts,
)


_CI_FIX_ATTEMPT_MARKER = "## CI-fix subagent: fix attempt"


def _fetch_ci_failure_log(detail_url: str) -> str:
    """Fetch the failed job log tail from a GitHub Actions check run.

    Extracts run_id from detailsUrl (format: .../runs/<run_id>/...)
    and calls `gh run view <run_id> --log-failed --repo REPO`.
    Returns the last 200 lines to stay within token limits.
    """
    m = re.search(r"/runs/(\d+)", detail_url)
    if not m:
        return f"(could not extract run ID from URL: {detail_url})"
    run_id = m.group(1)
    result = _run(
        ["gh", "run", "view", run_id, "--log-failed", "--repo", REPO],
        capture_output=True,
    )
    if result.returncode != 0:
        return f"(failed to fetch log: {result.stderr})"
    lines = (result.stdout or "").splitlines()
    return "\n".join(lines[-200:])


def handle_fix_ci(pr: dict) -> HandlerResult:
    """Auto-diagnose and fix CI failures on an auto-improve PR.

    The dispatcher has already verified the PR is at ``PRState.CI_FAILING``
    and hands this handler a single PR dict. We rebuild the full target
    (branch, issue, failing checks, head sha) by fetching fresh PR detail,
    then run the fix-ci subagent, push any fix commits, and transition
    back to ``REVIEWING_CODE``.
    """
    print("[cai fix-ci] checking for PRs with failing CI", flush=True)

    pr_number = pr.get("number")
    if pr_number is None:
        print("[cai fix-ci] handler called without pr['number']", file=sys.stderr)
        log_run("fix-ci", repo=REPO, result="missing_pr_number", exit=1)
        return HandlerResult(trigger="")

    try:
        pr_detail = _gh_json([
            "pr", "view", str(pr_number),
            "--repo", REPO,
            "--json", "number,headRefName,comments,statusCheckRollup,commits,labels",
        ])
    except subprocess.CalledProcessError as e:
        print(f"[cai fix-ci] gh pr view #{pr_number} failed:\n{e.stderr}", file=sys.stderr)
        log_run("fix-ci", repo=REPO, pr=pr_number, result="pr_lookup_failed", exit=1)
        return HandlerResult(trigger="")
    branch = pr_detail.get("headRefName", "")
    m = re.match(r"^auto-improve/(\d+)-", branch)
    if not m:
        print(
            f"[cai fix-ci] PR #{pr_number} branch '{branch}' "
            "is not an auto-improve branch",
            file=sys.stderr,
        )
        log_run("fix-ci", repo=REPO, pr=pr_number, result="not_auto_improve", exit=1)
        return HandlerResult(trigger="")
    issue_number = int(m.group(1))
    checks = pr_detail.get("statusCheckRollup") or []
    failing = [
        {"name": c.get("name", ""), "detailsUrl": c.get("detailsUrl") or c.get("url", "")}
        for c in checks
        if (c.get("conclusion") or c.get("status") or "").upper() == "FAILURE"
    ]
    commits = pr_detail.get("commits") or []
    head_sha = commits[-1].get("oid", "") if commits else ""
    target = {
        "pr_number": pr_detail["number"],
        "issue_number": issue_number,
        "branch": branch,
        "head_sha": head_sha,
        "failing_checks": failing,
    }

    print("[cai fix-ci] found 1 PR(s) with failing CI", flush=True)

    had_failure = False
    pending_transition = ""
    pr_number = target["pr_number"]
    issue_number = target["issue_number"]
    branch = target["branch"]
    failing_checks = target["failing_checks"]

    print(
        f"[cai fix-ci] fixing PR #{pr_number} (issue #{issue_number}, "
        f"{len(failing_checks)} failing check(s))",
        flush=True,
    )

    # 1. Lock — add :revising label.
    if not _set_labels(issue_number, add=[LABEL_REVISING], log_prefix="cai fix-ci"):
        print(f"[cai fix-ci] could not lock #{issue_number}", file=sys.stderr)
        log_run("fix-ci", repo=REPO, pr=pr_number, result="lock_failed", exit=1)
        return HandlerResult(trigger="")

    # 1a. CI_FAILING entry is now fired by ``drive_pr`` before this handler
    # runs (see ``cai_lib/dispatcher.py``), which inspects the pre-call
    # state and fires the matching ``*_to_ci_failing`` transition. By the
    # time we get here the PR is at :ci-failing.

    _run(["gh", "auth", "setup-git"], capture_output=True)

    _uid = uuid.uuid4().hex[:8]
    work_dir = Path(f"/tmp/cai-fix-ci-{issue_number}-{_uid}")

    try:
        if work_dir.exists():
            shutil.rmtree(work_dir)

        # 2. Clone and check out the PR branch.
        clone = _run(
            ["gh", "repo", "clone", REPO, str(work_dir)],
            capture_output=True,
        )
        if clone.returncode != 0:
            print(f"[cai fix-ci] clone failed:\n{clone.stderr}", file=sys.stderr)
            _set_labels(issue_number, remove=[LABEL_REVISING], log_prefix="cai fix-ci")
            log_run("fix-ci", repo=REPO, pr=pr_number, result="clone_failed", exit=1)
            return HandlerResult(trigger="")

        _git(work_dir, "fetch", "origin", branch)
        _git(work_dir, "checkout", branch)

        # 3. Configure git identity.
        name, email = _gh_user_identity()
        _git(work_dir, "config", "user.name", name)
        _git(work_dir, "config", "user.email", email)

        # 4. Freshen-up rebase onto main (non-conflicting only).
        _git(work_dir, "fetch", "origin", "main")
        pre_agent_head = _git(
            work_dir, "rev-parse", "HEAD", check=False,
        ).stdout.strip()
        _git(work_dir, "rebase", "origin/main", check=False)

        rebase_merge_dir = work_dir / ".git" / "rebase-merge"
        rebase_apply_dir = work_dir / ".git" / "rebase-apply"
        rebase_in_progress = (
            rebase_merge_dir.exists() or rebase_apply_dir.exists()
        )

        if rebase_in_progress:
            # Abort and skip — let cai revise handle conflicts.
            _git(work_dir, "rebase", "--abort", check=False)
            print(
                f"[cai fix-ci] PR #{pr_number}: rebase conflicts; "
                "skipping (leave for cai revise)",
                flush=True,
            )
            _set_labels(issue_number, remove=[LABEL_REVISING], log_prefix="cai fix-ci")
            log_run("fix-ci", repo=REPO, pr=pr_number, result="rebase_conflict", exit=0)
            return HandlerResult(trigger="")

        # 5. Fetch CI failure logs (first two failing checks).
        ci_log_section = "## CI failure log\n\n"
        for check in failing_checks[:2]:
            log_text = _fetch_ci_failure_log(check.get("detailsUrl", ""))
            ci_log_section += (
                f"### Check: {check.get('name', 'unknown')}\n\n"
                f"```\n{log_text}\n```\n\n"
            )

        # 6. Fetch original issue body.
        try:
            issue_data = _gh_json([
                "issue", "view", str(issue_number),
                "--repo", REPO,
                "--json", "number,title,body",
            ])
        except subprocess.CalledProcessError:
            issue_data = {"number": issue_number, "title": "(unknown)", "body": ""}

        # 7. Build PR state block.
        stat_result = _git(
            work_dir, "diff", "origin/main..HEAD", "--stat", check=False,
        )
        pr_stat = (stat_result.stdout or "").strip() or "(no changes vs origin/main)"
        pr_state_block = (
            f"## Current PR state\n\n"
            f"```\n{pr_stat}\n```\n\n"
        )

        # 8. Build the user message.
        _issue_num = issue_data["number"]
        user_message = (
            _work_directory_block(work_dir)
            + "\n"
            + "## Original issue\n\n"
            + f"### #{_issue_num} — {issue_data.get('title', '')}\n\n"
            + f"{issue_data.get('body') or '(no body)'}\n\n"
            + pr_state_block
            + ci_log_section
        )

        # 9. Pre-create the staging directory for agent self-edits.
        _setup_agent_edit_staging(work_dir)

        # 10. Invoke the cai-fix-ci subagent.
        print(
            f"[cai fix-ci] running cai-fix-ci subagent for {work_dir}",
            flush=True,
        )
        agent = _run_claude_p(
            ["claude", "-p", "--agent", "cai-fix-ci",
             "--dangerously-skip-permissions",
             "--add-dir", str(work_dir)],
            category="fix-ci",
            agent="cai-fix-ci",
            input=user_message,
            cwd="/app",
            target_kind="pr",
            target_number=pr_number,
            fix_attempt_count=len(_fetch_previous_fix_attempts(issue_number)),
        )
        if agent.stdout:
            print(agent.stdout, flush=True)

        # 10b. Apply any staged .claude/agents/**/*.md updates.
        applied = _apply_agent_edit_staging(work_dir)
        if applied:
            print(
                f"[cai fix-ci] applied {applied} staged "
                f".claude/agents/**/*.md update(s)",
                flush=True,
            )

        agent_summary = (agent.stdout or "").strip()[:4000]

        # 11. Commit any uncommitted changes the agent left behind.
        status = _git(work_dir, "status", "--porcelain", check=False)
        has_uncommitted = bool(status.stdout.strip())
        post_agent_head = _git(
            work_dir, "rev-parse", "HEAD", check=False,
        ).stdout.strip()
        head_changed = pre_agent_head != post_agent_head

        if has_uncommitted:
            _git(work_dir, "add", "-A")
            _git(work_dir, "commit", "-m",
                 "fix: address CI failure\n\n"
                 f"Refs {REPO}#{issue_number}\n\n"
                 "Co-Authored-By: Claude <noreply@anthropic.com>",
                 check=False)
            post_agent_head = _git(
                work_dir, "rev-parse", "HEAD", check=False,
            ).stdout.strip()
            head_changed = True

        if head_changed:
            push = _run(
                ["git", "-C", str(work_dir), "push",
                 "--force-with-lease", "origin", branch],
                capture_output=True,
            )
            if push.returncode != 0:
                print(
                    f"[cai fix-ci] push failed:\n{push.stderr}",
                    file=sys.stderr,
                )
            else:
                print(f"[cai fix-ci] pushed fix for PR #{pr_number}", flush=True)
                # Exit CI_FAILING now that new commits are up; the
                # dispatcher's next tick re-evaluates check status
                # and re-enters CI_FAILING via PRState selection if
                # checks are still red.
                pending_transition = "ci_failing_to_reviewing_code"
        else:
            print(
                f"[cai fix-ci] no changes produced by agent for PR #{pr_number}",
                flush=True,
            )

        # 12. Post marker comment (always — prevents retry on same SHA).
        marker_body = (
            f"{_CI_FIX_ATTEMPT_MARKER} — "
            f"{post_agent_head or pre_agent_head}"
        )
        if agent_summary:
            marker_body += f"\n\n{agent_summary}"
        _run(
            ["gh", "pr", "comment", str(pr_number),
             "--repo", REPO, "--body", marker_body],
            capture_output=True,
        )

        _set_labels(issue_number, remove=[LABEL_REVISING], log_prefix="cai fix-ci")
        log_run(
            "fix-ci", repo=REPO, pr=pr_number,
            result="fix_pushed" if head_changed else "no_changes",
            exit=0,
        )

    except Exception as exc:
        print(f"[cai fix-ci] unexpected failure: {exc!r}", file=sys.stderr)
        _set_labels(issue_number, remove=[LABEL_REVISING], log_prefix="cai fix-ci")
        log_run("fix-ci", repo=REPO, pr=pr_number, result="unexpected_error", exit=1)
        had_failure = True
    finally:
        if work_dir.exists():
            shutil.rmtree(work_dir, ignore_errors=True)

    return HandlerResult(trigger="" if had_failure else pending_transition)
