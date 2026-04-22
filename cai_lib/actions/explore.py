"""handle_explore — run the cai-explore agent on a :needs-exploration issue.

Lifted from ``cmd_explore`` in ``cai.py``. The dispatcher picks the
issue (oldest :needs-exploration) and hands it in; this handler only
needs to lock, clone, invoke the agent, and route the outcome.
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
    LABEL_NEEDS_EXPLORATION,
    LABEL_IN_PROGRESS,
    LABEL_RAISED,
    LABEL_REFINED,
    LABEL_PR_NEEDS_HUMAN,
)
from cai_lib.github import _set_labels, _build_issue_block
from cai_lib.subprocess_utils import _run, _run_claude_p
from cai_lib.logging_utils import log_run
from cai_lib.cmd_helpers import _work_directory_block, _strip_stored_plan_block


def handle_explore(issue: dict) -> int:
    """Run the cai-explore agent on the dispatcher-supplied issue.

    Outcomes:
    - ## Exploration Findings + ### Recommendation close_documented/close_wont_do → close
    - ## Exploration Findings + ### Recommendation refine_and_retry → :raised
    - ## Refined Issue → :refined (direct hand-off to fix)
    - ## Exploration Blocked → :needs-human-review
    - No marker → rollback to :needs-exploration
    """
    t0 = time.monotonic()
    issue_number = issue["number"]
    title = issue["title"]
    print(f"[cai explore] picked #{issue_number}: {title}", flush=True)

    # Lock: :needs-exploration → :in-progress.
    if not _set_labels(
        issue_number,
        add=[LABEL_IN_PROGRESS],
        remove=[LABEL_NEEDS_EXPLORATION],
        log_prefix="cai explore",
    ):
        print(f"[cai explore] could not lock #{issue_number}", file=sys.stderr)
        log_run("explore", repo=REPO, issue=issue_number, result="lock_failed", exit=1)
        return 1

    _uid = uuid.uuid4().hex[:8]
    work_dir = Path(f"/tmp/cai-explore-{issue_number}-{_uid}")

    def rollback() -> None:
        _set_labels(
            issue_number,
            add=[LABEL_NEEDS_EXPLORATION],
            remove=[LABEL_IN_PROGRESS],
            log_prefix="cai explore",
        )

    try:
        if work_dir.exists():
            shutil.rmtree(work_dir)

        _run(["gh", "auth", "setup-git"], capture_output=True)
        clone = _run(
            ["git", "clone", "--depth", "1",
             f"https://github.com/{REPO}.git", str(work_dir)],
            capture_output=True,
        )
        if clone.returncode != 0:
            print(f"[cai explore] git clone failed:\n{clone.stderr}", file=sys.stderr)
            rollback()
            log_run("explore", repo=REPO, issue=issue_number, result="clone_failed", exit=1)
            return 1

        user_message = (
            _work_directory_block(work_dir)
            + "\n"
            + _build_issue_block(issue)
        )
        print(f"[cai explore] running cai-explore subagent for {work_dir}", flush=True)
        result = _run_claude_p(
            ["claude", "-p", "--agent", "cai-explore",
             "--dangerously-skip-permissions",
             "--add-dir", str(work_dir)],
            category="explore",
            agent="cai-explore",
            input=user_message,
            cwd="/app",
            timeout=1800,  # 30 min cap
            target_kind="issue",
            target_number=issue_number,
        )
        if result.stdout:
            print(result.stdout, flush=True)

        if result.returncode != 0:
            print(
                f"[cai explore] subagent failed (exit {result.returncode}):\n"
                f"{result.stderr}",
                file=sys.stderr,
            )
            rollback()
            dur = f"{int(time.monotonic() - t0)}s"
            log_run("explore", repo=REPO, issue=issue_number,
                    duration=dur, result="agent_failed", exit=result.returncode)
            return result.returncode

        stdout = result.stdout or ""

        # Outcome 1: Exploration Findings
        findings_pos = stdout.find("## Exploration Findings")
        if findings_pos != -1:
            findings_block = stdout[findings_pos:].strip()
            rec_match = re.search(
                r"###\s*Recommendation\s*\n+\s*(\S+)",
                findings_block,
            )
            recommendation = rec_match.group(1).strip() if rec_match else ""

            if recommendation in ("close_documented", "close_wont_do"):
                _run(
                    ["gh", "issue", "comment", str(issue_number),
                     "--repo", REPO,
                     "--body", f"## Exploration findings\n\n{findings_block}\n\n---\n_Closed by `cai explore`._"],
                    capture_output=True,
                )
                _run(
                    ["gh", "issue", "close", str(issue_number),
                     "--repo", REPO],
                    capture_output=True,
                )
                _set_labels(issue_number, remove=[LABEL_IN_PROGRESS], log_prefix="cai explore")
                dur = f"{int(time.monotonic() - t0)}s"
                print(f"[cai explore] #{issue_number} closed ({recommendation}) in {dur}", flush=True)
                log_run("explore", repo=REPO, issue=issue_number,
                        duration=dur, result=recommendation, exit=0)
                return 0

            elif recommendation == "refine_and_retry":
                original_body = _strip_stored_plan_block(issue.get("body") or "(no body)")
                quoted_original = "\n".join(f"> {line}" for line in original_body.splitlines())
                new_body = (
                    f"{findings_block}\n\n"
                    f"---\n\n"
                    f"> **Original issue text:**\n>\n"
                    f"{quoted_original}\n"
                )
                _run(
                    ["gh", "issue", "edit", str(issue_number),
                     "--repo", REPO, "--body", new_body],
                    capture_output=True,
                )
                _set_labels(
                    issue_number,
                    add=[LABEL_RAISED],
                    remove=[LABEL_IN_PROGRESS],
                    log_prefix="cai explore",
                )
                dur = f"{int(time.monotonic() - t0)}s"
                print(f"[cai explore] #{issue_number} refined-and-retried in {dur}", flush=True)
                log_run("explore", repo=REPO, issue=issue_number,
                        duration=dur, result="refine_and_retry", exit=0)
                return 0

            # Unrecognised recommendation — fall through to no_marker

        # Outcome 2: Refined Issue
        refined_pos = stdout.find("## Refined Issue")
        if refined_pos != -1:
            refined_body = stdout[refined_pos:].strip()
            original_body = _strip_stored_plan_block(issue.get("body") or "(no body)")
            quoted_original = "\n".join(f"> {line}" for line in original_body.splitlines())
            new_body = (
                f"{refined_body}\n\n"
                f"---\n\n"
                f"> **Original issue text:**\n>\n"
                f"{quoted_original}\n"
            )
            _run(
                ["gh", "issue", "edit", str(issue_number),
                 "--repo", REPO, "--body", new_body],
                capture_output=True,
            )
            _set_labels(
                issue_number,
                add=[LABEL_REFINED],
                remove=[LABEL_IN_PROGRESS],
                log_prefix="cai explore",
            )
            dur = f"{int(time.monotonic() - t0)}s"
            print(f"[cai explore] #{issue_number} refined and handed to fix in {dur}", flush=True)
            log_run("explore", repo=REPO, issue=issue_number,
                    duration=dur, result="refined", exit=0)
            return 0

        # Outcome 3: Exploration Blocked
        blocked_pos = stdout.find("## Exploration Blocked")
        if blocked_pos != -1:
            blocked_block = stdout[blocked_pos:].strip()
            _run(
                ["gh", "issue", "comment", str(issue_number),
                 "--repo", REPO,
                 "--body", f"{blocked_block}\n\n---\n_Escalated by `cai explore`._"],
                capture_output=True,
            )
            _set_labels(
                issue_number,
                add=[LABEL_PR_NEEDS_HUMAN],
                remove=[LABEL_IN_PROGRESS],
                log_prefix="cai explore",
            )
            dur = f"{int(time.monotonic() - t0)}s"
            print(f"[cai explore] #{issue_number} blocked/escalated in {dur}", flush=True)
            log_run("explore", repo=REPO, issue=issue_number,
                    duration=dur, result="blocked", exit=0)
            return 0

        # No recognised marker — rollback to :needs-exploration.
        rollback()
        dur = f"{int(time.monotonic() - t0)}s"
        print(f"[cai explore] #{issue_number} no outcome marker; rolling back in {dur}", flush=True)
        log_run("explore", repo=REPO, issue=issue_number,
                duration=dur, result="no_marker", exit=0)
        return 0

    except Exception as exc:
        print(f"[cai explore] unexpected error: {exc}", file=sys.stderr)
        rollback()
        log_run("explore", repo=REPO, issue=issue_number, result="error", exit=1)
        return 1
    finally:
        if work_dir.exists():
            shutil.rmtree(work_dir, ignore_errors=True)
