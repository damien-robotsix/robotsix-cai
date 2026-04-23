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

import re
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

from cai_lib.config import REPO
from cai_lib.dispatcher import HandlerResult
from cai_lib.fsm import get_pr_state, PRState
from cai_lib.github import _gh_json, _fetch_linked_issue_block
from cai_lib.subprocess_utils import _run, _run_claude_p
from cai_lib.cmd_helpers import (
    _git, _gh_user_identity, _work_directory_block,
    _setup_agent_edit_staging, _apply_agent_edit_staging,
    _parse_oob_issues, _create_oob_issues,
)
from cai_lib.logging_utils import log_run


# Docs-review comment headings. Duplicated from cai.py because cmd_merge
# still reads the same constants there — keep the strings in sync until
# cmd_merge is extracted.
_DOCS_REVIEW_COMMENT_HEADING_PREFIX = "## cai docs review"
_DOCS_REVIEW_COMMENT_HEADING_CLEAN = "## cai docs review (clean)"
_DOCS_REVIEW_COMMENT_HEADING_APPLIED = "## cai docs review (applied)"


def _build_deletion_manifest_block(work_dir: Path) -> str:
    """Return the authoritative deletion manifest block for the
    ``cai-review-docs`` user message.

    Computes the set of files this PR removes vs ``origin/main``
    using ``git diff --name-only --diff-filter=D``, cross-verifies
    that each reported path is actually absent from ``work_dir``
    (guarding against pathological `diff` output), and renders a
    Markdown block the agent consumes as the single source of
    truth for deletions.

    Established by issue #960 / PR #950: without a deterministic
    manifest, the agent inferred deletions from ``git diff --stat``
    and removed live references to still-present files.
    """
    del_result = _git(
        work_dir, "diff", "origin/main..HEAD",
        "--name-only", "--diff-filter=D",
        check=False,
    )
    raw_paths = [
        line.strip()
        for line in (del_result.stdout or "").splitlines()
        if line.strip()
    ]
    verified_deleted = [p for p in raw_paths if not (work_dir / p).exists()]
    if verified_deleted:
        manifest_lines = "\n".join(f"- `{p}`" for p in verified_deleted)
        return (
            "## Authoritative deletion manifest\n\n"
            "The following files are deleted by this PR vs "
            "`origin/main` (verified by "
            "`git diff --name-only --diff-filter=D` **and** confirmed "
            "absent from the work directory). This list is the "
            "**single source of truth** for deletions — do NOT infer "
            "deletions from the stat summary above; any file NOT in "
            "this list is still present in the work directory and "
            "must be treated as status `M`:\n\n"
            f"{manifest_lines}\n\n"
        )
    return (
        "## Authoritative deletion manifest\n\n"
        "This PR deletes no files vs `origin/main`. Do NOT remove "
        "any reference on the assumption that a file was deleted — "
        "the `--stat` summary above only shows line-count deltas, "
        "not deletions.\n\n"
    )


def _run_generated_docs(work_dir: Path) -> str:
    """Run the deterministic doc generators on the PR working tree.

    Replaces the former ``.github/workflows/regenerate-docs.yml`` job
    by running the generators inside the docs-review FSM step. Drift
    lands uncommitted in ``work_dir`` and is picked up by the caller's
    final ``git add -A && git commit``.

    Returns a Markdown block describing what ran and, if applicable,
    which files drifted — so the agent knows the working tree is
    ahead of the PR HEAD.
    """
    scripts = [
        ("docs/fsm.md", ["python", "scripts/generate-fsm-docs.py"]),
    ]
    failures: list[str] = []
    for label, cmd in scripts:
        res = _run(cmd, cwd=str(work_dir), capture_output=True)
        if res.returncode != 0:
            failures.append(
                f"- `{' '.join(cmd)}` (target: {label}) exited "
                f"{res.returncode}: {(res.stderr or '').strip()[:200]}"
            )

    status = _git(work_dir, "status", "--porcelain", check=False)
    drift_lines = [
        line for line in (status.stdout or "").splitlines() if line.strip()
    ]

    lines = ["## Generated-docs regeneration\n"]
    lines.append(
        "The deterministic generators have been run against the PR "
        "branch. Any drift listed below is uncommitted in the work "
        "directory and will be bundled into your final docs-update "
        "commit.\n"
    )
    if failures:
        lines.append("**Generator errors (investigate):**\n")
        lines.extend(failures)
        lines.append("")
    if drift_lines:
        lines.append("**Working-directory drift after generators:**\n")
        lines.append("```")
        lines.extend(drift_lines)
        lines.append("```\n")
    else:
        lines.append("Generators produced no drift.\n")
    return "\n".join(lines) + "\n"


def _run_module_coverage_check(work_dir: Path) -> str:
    """Run ``scripts/check-modules-coverage.py`` against the PR tree.

    Returns a Markdown block that either reports a clean pass or
    embeds the diagnostic output so the ``cai-review-docs`` agent
    can update ``docs/modules.yaml`` (and add any missing
    ``docs/modules/<name>.md``) in-place.
    """
    res = _run(
        ["python", "scripts/check-modules-coverage.py"],
        cwd=str(work_dir),
        capture_output=True,
    )
    out = ((res.stdout or "") + (res.stderr or "")).strip()
    if res.returncode == 0:
        return (
            "## Module coverage\n\n"
            f"`scripts/check-modules-coverage.py` passed: {out}\n\n"
        )
    return (
        "## Module coverage — FAILED\n\n"
        "`scripts/check-modules-coverage.py` exited non-zero. "
        "Update `docs/modules.yaml` so every tracked file is matched "
        "by exactly one module. If you add a new module, also write "
        "the corresponding `docs/modules/<name>.md` narrative. Diagnostic "
        "output:\n\n"
        f"```\n{out}\n```\n\n"
    )


def handle_review_docs(pr: dict) -> HandlerResult:
    """Run cai-review-docs on *pr* (already at PRState.REVIEWING_DOCS)."""
    t0 = time.monotonic()

    pr_number = pr["number"]
    head_sha = pr["headRefOid"]
    branch = pr.get("headRefName", "")
    title = pr["title"]

    print(f"[cai review-docs] targeting PR #{pr_number}: {title}", flush=True)

    # Idempotency: if we already posted a docs review for this SHA, advance
    # the FSM based on the cached outcome instead of re-running the agent.
    # Walk comments newest-first so the most recent verdict for this SHA wins.
    for comment in reversed(pr.get("comments", [])):
        body = (comment.get("body") or "")
        first_line = body.split("\n", 1)[0]
        if not (
            first_line.startswith(_DOCS_REVIEW_COMMENT_HEADING_PREFIX)
            and head_sha in first_line
        ):
            continue
        if first_line.startswith(_DOCS_REVIEW_COMMENT_HEADING_CLEAN):
            # Prior run reviewed cleanly but failed to advance state.
            # Apply the transition the fresh-run path would have applied.
            print(
                f"[cai review-docs] PR #{pr_number}: cached clean review at "
                f"{head_sha[:8]} — advancing to APPROVED",
                flush=True,
            )
            log_run("review_docs", repo=REPO, pr=pr_number,
                    result="cached_clean_advanced", exit=0)
            return HandlerResult(trigger="reviewing_docs_to_approved")
        if first_line.startswith(_DOCS_REVIEW_COMMENT_HEADING_APPLIED):
            # A docs-fix push happened at this SHA. Docs review is the
            # last gate before merge; we do not bounce back to code
            # review just because doc files changed (the merge handler
            # is the final gatekeeper).
            print(
                f"[cai review-docs] PR #{pr_number}: cached applied-fix at "
                f"{head_sha[:8]} — advancing to APPROVED",
                flush=True,
            )
            log_run("review_docs", repo=REPO, pr=pr_number,
                    result="cached_applied_advanced", exit=0)
            return HandlerResult(trigger="reviewing_docs_to_approved")
        # Heading prefix matched but suffix is unfamiliar — fall through
        # to a fresh review rather than guess.
        break

    # State gate (defensive — dispatcher should already have verified).
    if get_pr_state(pr) != PRState.REVIEWING_DOCS:
        print(
            f"[cai review-docs] PR #{pr_number}: not in REVIEWING_DOCS "
            f"state; waiting",
            flush=True,
        )
        log_run("review_docs", repo=REPO, pr=pr_number,
                result="wrong_state", exit=0)
        return HandlerResult(trigger="")

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
            return HandlerResult(trigger="")

        _git(work_dir, "fetch", "origin", branch)
        _git(work_dir, "checkout", branch)

        # Configure git identity so the agent can commit.
        name, email = _gh_user_identity()
        _git(work_dir, "config", "user.name", name)
        _git(work_dir, "config", "user.email", email)
        _setup_agent_edit_staging(work_dir)

        # Regenerate deterministic docs on the PR branch. Folded in
        # from the former .github/workflows/regenerate-docs.yml so all
        # doc upkeep happens in one FSM pass (issue #907). Any drift
        # lands as uncommitted changes in work_dir and is bundled into
        # the final docs-update commit below.
        generator_block = _run_generated_docs(work_dir)

        # Module-coverage gate (docs/modules.yaml). If the registry no
        # longer covers every tracked file, we surface the diagnostic
        # to the agent so it can update the registry and add any
        # missing per-module narrative.
        coverage_block = _run_module_coverage_check(work_dir)

        # --stat summary serves as the file-level map for the agent;
        # the full diff is intentionally omitted (token sink).
        stat_result = _git(
            work_dir, "diff", "origin/main..HEAD", "--stat",
            check=False,
        )
        pr_stat = (stat_result.stdout or "").strip() or (
            "(no changes vs origin/main)"
        )

        # Authoritative deletion manifest (issue #960): the exact set
        # of files this PR actually removes vs origin/main, determined
        # by `git diff --name-only --diff-filter=D` and cross-verified
        # against the work directory. The agent consumes this list as
        # the single source of truth for deletions, instead of guessing
        # from the --stat summary (which was the false-premise failure
        # mode in PR #950).
        deletion_manifest_block = _build_deletion_manifest_block(work_dir)

        author_login = pr.get("author", {}).get("login", "unknown")
        issue_block = _fetch_linked_issue_block(pr.get("body", ""))
        user_message = (
            _work_directory_block(work_dir, issue_block)
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
            + deletion_manifest_block
            + generator_block
            + coverage_block
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
             "--allowedTools", "Read,Grep,Glob,Edit,Write",
             "--add-dir", str(work_dir)],
            category="review-docs",
            agent="cai-review-docs",
            input=user_message,
            cwd="/app",
            target_kind="pr",
            target_number=pr_number,
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
            return HandlerResult(trigger="")

        agent_output = (agent.stdout or "").strip()

        # Parse and create any out-of-scope issues emitted by the agent,
        # then strip them from agent_output so they don't appear in the
        # PR comment.
        oob_issues = _parse_oob_issues(agent_output)
        if oob_issues:
            _create_oob_issues(oob_issues, pr_number, "cai review-docs")
            agent_output = re.sub(
                r"^## Out-of-scope Issue\s*\n.*?(?=^## Out-of-scope Issue|\Z)",
                "",
                agent_output,
                flags=re.MULTILINE | re.DOTALL,
            ).strip()

        applied = _apply_agent_edit_staging(work_dir)
        if applied:
            print(
                f"[cai review-docs] applied {applied} staged "
                f".claude/agents/**/*.md update(s)",
                flush=True,
            )

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
                return HandlerResult(trigger="")
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

        # Advance FSM state. Docs review is the final pre-merge gate:
        # whether or not it pushed doc fixes, the PR moves to APPROVED
        # and the merge handler decides whether to merge. Bouncing back
        # to REVIEWING_CODE on a doc push caused review/docs ping-pong
        # loops that produced no new code findings.
        if has_doc_changes:
            result_word = "fixes pushed"
            result_tag = "fixes_pushed"
        else:
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
        return HandlerResult(trigger="reviewing_docs_to_approved")

    except subprocess.CalledProcessError as e:
        print(
            f"[cai review-docs] subprocess failure for PR #{pr_number}: "
            f"{e!r}",
            file=sys.stderr,
        )
        dur = f"{int(time.monotonic() - t0)}s"
        log_run("review_docs", repo=REPO, pr=pr_number,
                duration=dur, result="subprocess_error", exit=1)
        return HandlerResult(trigger="")
    except Exception as e:
        print(
            f"[cai review-docs] unexpected failure for PR #{pr_number}: "
            f"{e!r}",
            file=sys.stderr,
        )
        dur = f"{int(time.monotonic() - t0)}s"
        log_run("review_docs", repo=REPO, pr=pr_number,
                duration=dur, result="unexpected_error", exit=1)
        return HandlerResult(trigger="")
    finally:
        if work_dir.exists():
            shutil.rmtree(work_dir, ignore_errors=True)
