"""cai_lib.actions.revise — handler for PRState.REVISION_PENDING.

Invoked by the FSM dispatcher after it has fetched an open PR and
verified its state is ``PRState.REVISION_PENDING``. Fetches
unaddressed review comments, clones the repo, rebases onto main,
runs the ``cai-revise`` agent (or ``cai-rebase`` for conflict-only
cases), force-pushes the updated branch, and transitions the PR
back to ``REVIEWING_CODE``.
"""
from __future__ import annotations

import json as _json
import re
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

from cai_lib.config import (
    REPO,
    LABEL_REVISING,
    LABEL_REFINED,
    LABEL_PR_OPEN,
)
from cai_lib.fsm import (
    PRState,
    fire_trigger,
    get_pr_state,
)
from cai_lib.github import _gh_json, _set_labels
from cai_lib.subprocess_utils import _run, _run_claude_p
from cai_lib.logging_utils import log_run, log_run as _log_run_alias  # noqa: F401
from cai_lib.cmd_helpers import (
    _git,
    _gh_user_identity,
    _fetch_review_comments,
    _parse_iso_ts,
    _apply_agent_edit_staging,
    _is_bot_comment,
    _setup_agent_edit_staging,
    _work_directory_block,
)


# Sentinels for the pre-merge-review comment boilerplate. These
# duplicate the constants in cai_lib.actions.review_pr so this module
# can recognise findings comments without depending on that module.
_REVIEW_COMMENT_HEADING_FINDINGS = "## cai pre-merge review"
_REVIEW_COMMENT_HEADING_CLEAN = "## cai pre-merge review (clean)"


# Maximum number of characters of the original issue body to inline into the
# revise agent's user message when no recognisable section headings are found
# (old-format issues).  Structured issues are handled by _extract_revise_context
# which extracts only the relevant sections instead of truncating.
_REVISE_ISSUE_BODY_MAX_CHARS = 1500

# Ordered list of section headings to extract from an issue body for the
# revise agent. Each entry is (preferred_heading, *fallback_headings).
# The revise agent needs intent and scope, not detailed implementation steps.
_REVISE_CONTEXT_HEADINGS: tuple[tuple[str, ...], ...] = (
    ("### Description", "### Problem"),
    ("### Summary",),
    ("### Files to change", "### Files likely to touch"),
    ("### Scope guardrails",),
    ("### Review considerations",),
)


def _extract_revise_context(body: str) -> str:
    """Return only the sections relevant to cai-revise from an issue body.

    Splits on level-3 headings and keeps only the entries listed in
    _REVISE_CONTEXT_HEADINGS (first matching alias wins).  Falls back to
    the first _REVISE_ISSUE_BODY_MAX_CHARS characters when the body has
    no recognisable headings (old-format issues).
    """
    parts = body.split("\n### ")
    if len(parts) <= 1:
        return body[:_REVISE_ISSUE_BODY_MAX_CHARS]

    # Build {normalised_heading: full_section_text} from the split parts.
    sections: dict[str, str] = {}
    for part in parts[1:]:
        nl = part.find("\n")
        if nl == -1:
            heading, content = part.strip(), ""
        else:
            heading, content = part[:nl].strip(), part[nl + 1:]
        sections[heading.lower()] = f"### {heading}\n{content}"

    extracted: list[str] = []
    for aliases in _REVISE_CONTEXT_HEADINGS:
        for alias in aliases:
            key = alias.lstrip("# ").lower()
            if key in sections:
                extracted.append(sections[key].rstrip())
                break

    if not extracted:
        # No target headings found — old-format issue, fall back.
        return body[:_REVISE_ISSUE_BODY_MAX_CHARS]

    return "\n\n".join(extracted)


# Sentinel strings for the fixed boilerplate that cai review-pr wraps around
# agent output. We strip these when inlining review-pr findings into the
# revise user message — they are inert for the revise agent and just waste
# tokens.
_REVIEW_PR_FOOTER_SENTINEL = (
    "_Pre-merge consistency review by `cai review-pr`."
)
_REVIEW_PR_PREAMBLE = "Now I have enough information to report findings."


def _strip_review_pr_boilerplate(body: str) -> str:
    """Strip known preamble/footer from a cai review-pr findings comment body.

    The footer is the ``---`` separator line plus the attribution sentence
    added by ``cmd_review_pr``.  The preamble is a fixed phrase the
    ``cai-review-pr`` agent emits before its ``### Finding:`` blocks.
    Both are inert for the revise agent.
    """
    # Strip trailing footer: find the last "---" separator whose following
    # content is the review-pr attribution line.
    footer_idx = body.rfind("\n---\n")
    if footer_idx != -1:
        after_sep = body[footer_idx + 5:].lstrip()
        if after_sep.startswith(_REVIEW_PR_FOOTER_SENTINEL):
            body = body[:footer_idx].rstrip()
    # Strip the fixed agent preamble if present anywhere in the body.
    body = body.replace(_REVIEW_PR_PREAMBLE, "").strip()
    return body


# Marker that revise/cmd_implement uses when its subagent decides no code
# changes are needed in response to a comment. The presence of this
# marker AFTER all human comments means the bot has acknowledged the
# request and explicitly chose not to act — so we should NOT keep
# re-processing the same comments forever.
_NO_ADDITIONAL_CHANGES_MARKER = "## Revise subagent: no additional changes"

# JSON schema for cai-comment-filter output.
_FILTER_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "unresolved": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["id", "reason"],
            },
        },
    },
    "required": ["unresolved"],
}

# Maximum characters of PR diff to pass to the comment-filter haiku.
_FILTER_DIFF_MAX_CHARS = 20000


def _filter_comments_with_haiku(
    all_comments: list[dict],
    pr_number: int,
) -> list[dict]:
    """Filter PR comments using the cai-comment-filter haiku agent.

    Replaces the old commit-timestamp watermark. Returns only comments
    the agent judges to be genuinely unresolved. On agent failure,
    returns all non-bot comments (conservative fallback — better to
    over-process than silently drop human requests).
    """
    if not all_comments:
        return []

    # Assign a stable synthetic index to each comment so the agent
    # can reference them by ID without needing a GitHub comment ID.
    indexed = [{"_idx": str(i), **c} for i, c in enumerate(all_comments)]

    # Fetch the PR diff (truncated if large).
    diff_result = _run(
        ["gh", "pr", "diff", str(pr_number), "--repo", REPO],
        capture_output=True,
    )
    pr_diff = (diff_result.stdout or "").strip()
    if len(pr_diff) > _FILTER_DIFF_MAX_CHARS:
        pr_diff = pr_diff[:_FILTER_DIFF_MAX_CHARS] + "\n\n[diff truncated]"

    # Build user message for the haiku agent.
    comments_text = ""
    for c in indexed:
        idx = c["_idx"]
        author = c.get("author", {}).get("login", "unknown")
        created = c.get("createdAt", "")
        body = c.get("body", "")
        comments_text += f"### Comment {idx} by @{author} ({created})\n\n{body}\n\n"

    user_message = (
        f"## PR #{pr_number}\n\n"
        f"## All PR Comments\n\n{comments_text}"
        f"## Current PR Diff\n\n```diff\n{pr_diff}\n```\n"
    )

    result = _run_claude_p(
        ["claude", "-p", "--agent", "cai-comment-filter",
         "--dangerously-skip-permissions",
         "--json-schema", _json.dumps(_FILTER_JSON_SCHEMA)],
        category="revise.filter",
        agent="cai-comment-filter",
        input=user_message,
        cwd="/app",
    )

    if result.returncode != 0 or not (result.stdout or "").strip():
        print(
            f"[cai revise] cai-comment-filter failed (rc={result.returncode}); "
            "treating all non-bot comments as unaddressed",
            file=sys.stderr,
        )
        return [c for c in all_comments if not _is_bot_comment(c)]

    try:
        payload = _json.loads(result.stdout)
    except (_json.JSONDecodeError, ValueError) as exc:
        print(
            f"[cai revise] cai-comment-filter output was not valid JSON: {exc}; "
            "treating all non-bot comments as unaddressed",
            file=sys.stderr,
        )
        return [c for c in all_comments if not _is_bot_comment(c)]

    unresolved_ids = {item["id"] for item in payload.get("unresolved", [])}
    return [c for c, ic in zip(all_comments, indexed) if ic["_idx"] in unresolved_ids]


# Marker that revise posts when an auto-rebase against main fails
# even after the resolver subagent has tried to merge the conflicts.
# If this marker appears AFTER the current commit, we short-circuit
# the loop on the very next tick: the resolver has already taken its
# best shot and failed, and re-trying every cron tick just spams the
# PR with identical failure comments. One failed resolver attempt is
# enough — see #188.
#
# NOTE: this string is intentionally distinct from the legacy
# "## Revise subagent: rebase failed" marker so PRs that were stuck
# under the pre-resolver code path get exactly one fresh attempt
# with the new resolver before being skipped again.
_REBASE_FAILED_MARKER = "## Revise subagent: rebase resolution failed"


def _rebase_conflict_files(work_dir: Path) -> list[str]:
    """Return the list of files currently in a conflicted (unmerged) state."""
    res = _git(
        work_dir, "diff", "--name-only", "--diff-filter=U", check=False,
    )
    return [line for line in res.stdout.strip().splitlines() if line]


def _recover_stuck_rebase_prs() -> int:
    """Close PRs the rebase resolver gave up on so the issue can flow
    through the planning cycle and the implement subagent can re-attempt from
    a fresh branch off current main.

    Trigger condition: an open `auto-improve/<N>-*` PR has a
    `## Revise subagent: rebase resolution failed` comment newer than
    its latest commit. The loop guard from #196 already stops the
    revise step from spamming retry comments — but without recovery
    the PR sits stuck forever, accumulating an ever-larger conflict
    surface every time main moves. Closing it and resetting the issue
    to `:refined` lets it re-flow through plan → approval → fix against
    current main on a future cycle (#144 was the original symptom).

    Returns the number of PRs recovered.
    """
    try:
        prs = _gh_json([
            "pr", "list",
            "--repo", REPO,
            "--state", "open",
            "--limit", "100",
            "--json",
            "number,headRefName,comments,commits",
        ])
    except subprocess.CalledProcessError:
        return 0

    recovered = 0
    for pr in prs:
        branch = pr.get("headRefName", "")
        if not branch.startswith("auto-improve/"):
            continue

        # Pull issue number from the branch name (`auto-improve/<N>-*`).
        m = re.match(r"auto-improve/(\d+)-", branch)
        if not m:
            continue
        issue_number = int(m.group(1))
        pr_number = pr["number"]

        commits = pr.get("commits", [])
        last_commit_date = commits[-1].get("committedDate", "") if commits else ""
        commit_ts = _parse_iso_ts(last_commit_date)
        if commit_ts is None:
            continue

        # Look for a `rebase resolution failed` marker newer than the
        # latest commit — that means the resolver tried, failed, and
        # nothing has moved since.
        stuck = False
        for c in pr.get("comments", []):
            body = (c.get("body") or "").lstrip()
            if not body.startswith(_REBASE_FAILED_MARKER):
                continue
            ts = _parse_iso_ts(c.get("createdAt"))
            if ts is None or ts <= commit_ts:
                continue
            stuck = True
            break
        if not stuck:
            continue

        print(
            f"[cai revise] PR #{pr_number}: rebase resolver gave up "
            f"and no commits since; closing and resetting issue "
            f"#{issue_number} to :refined so fix can retry",
            flush=True,
        )

        comment = (
            "## Revise subagent: closing stuck PR for fresh attempt\n\n"
            "The rebase resolver could not land this branch onto "
            "current `main` and no further progress is possible from "
            f"this branch. Closing so the implement subagent can re-open a "
            f"fresh PR for #{issue_number} against the current `main`.\n\n"
            "---\n"
            "_Closed automatically by `cai revise` recovery. The "
            "linked issue has been reset to `auto-improve:refined` and "
            "will be picked up on the next `cai implement` tick._"
        )
        close_res = _run(
            ["gh", "pr", "close", str(pr_number),
             "--repo", REPO, "--delete-branch", "--comment", comment],
            capture_output=True,
        )
        if close_res.returncode != 0:
            print(
                f"[cai revise] PR #{pr_number}: gh pr close failed:\n"
                f"{close_res.stderr}",
                file=sys.stderr,
            )
            continue

        # Reset the linked issue back to the eligible-for-fix state.
        # NOTE: LABEL_MERGE_BLOCKED is intentionally NOT removed here.
        # If cmd_implement opens a fresh PR for this issue, the revise guard
        # in _select_revise_targets() must still see merge-blocked until
        # cmd_merge re-evaluates the new PR and removes it.  cmd_merge
        # explicitly does NOT skip on merge-blocked, so it will evaluate
        # the new PR regardless.  Issue #432.
        _set_labels(
            issue_number,
            add=[LABEL_REFINED],
            remove=[LABEL_PR_OPEN, LABEL_REVISING],
            log_prefix="cai revise",
        )
        log_run("revise", repo=REPO, pr=pr_number, issue=issue_number,
                result="recovered_stuck_rebase", exit=0)
        recovered += 1

    return recovered




def handle_revise(pr: dict) -> int:
    """Iterate on an open PR based on review comments.

    The dispatcher has already verified the PR is at
    ``PRState.REVISION_PENDING`` and hands this handler a single PR
    dict. We run the shared orphaned/stuck recovery sweeps (they are
    cheap and keep the PR pool tidy), then resolve the target via
    ``_select_revise_targets`` filtered to the handed PR number, and
    execute the same clone → rebase → agent → force-push pipeline as
    the legacy ``cmd_revise``.
    """
    print("[cai revise] checking for PRs with unaddressed comments", flush=True)

    # Orphaned-PR sweep is now owned by the queue audit (Step 1g) so
    # PRs parked at any non-revision state (e.g. :pr-human-needed)
    # are also covered. See issue #869.

    # Recover any PRs the rebase resolver has given up on, so they
    # don't sit stuck forever. Refs #144.
    recovered = _recover_stuck_rebase_prs()
    if recovered:
        print(
            f"[cai revise] recovered {recovered} stuck PR(s) for fresh fix attempt",
            flush=True,
        )

    pr_number_in = pr.get("number")
    if pr_number_in is None:
        print("[cai revise] handler called without pr['number']", file=sys.stderr)
        log_run("revise", repo=REPO, result="missing_pr_number", exit=1)
        return 1

    # Resolve the single target: fetch fresh PR detail for the handed
    # PR and build a one-item target list (matches the direct-targeting
    # path of the legacy cmd_revise).
    try:
        pr_detail = _gh_json([
            "pr", "view", str(pr_number_in),
            "--repo", REPO,
            "--json", "number,headRefName,comments,labels,commits,mergeable,mergeStateStatus",
        ])
    except subprocess.CalledProcessError as e:
        print(f"[cai revise] gh pr view #{pr_number_in} failed:\n{e.stderr}", file=sys.stderr)
        log_run("revise", repo=REPO, pr=pr_number_in, result="pr_lookup_failed", exit=1)
        return 1
    branch = pr_detail.get("headRefName", "")
    m = re.match(r"^auto-improve/(\d+)-", branch)
    if not m:
        print(
            f"[cai revise] PR #{pr_number_in} branch '{branch}' is not an auto-improve branch",
            file=sys.stderr,
        )
        log_run("revise", repo=REPO, pr=pr_number_in, result="not_auto_improve", exit=1)
        return 1
    issue_number = int(m.group(1))
    # Collect comments (issue-level + line-by-line review).
    issue_comments = pr_detail.get("comments", [])
    line_comments = _fetch_review_comments(pr_detail["number"])
    all_comments = issue_comments + line_comments
    # Filter to genuinely unresolved comments using the haiku agent.
    unaddressed = _filter_comments_with_haiku(all_comments, pr_detail["number"])
    needs_rebase = pr_detail.get("mergeable") == "CONFLICTING" or \
        pr_detail.get("mergeStateStatus") == "DIRTY"
    targets = [{
        "pr_number": pr_detail["number"],
        "issue_number": issue_number,
        "branch": branch,
        "comments": unaddressed,
        "needs_rebase": needs_rebase,
    }]

    if not targets:
        print("[cai revise] no PRs need revision; nothing to do", flush=True)
        log_run("revise", repo=REPO, result="no_targets",
                recovered=recovered, exit=0)
        return 0

    print(f"[cai revise] found {len(targets)} PR(s) to revise", flush=True)

    had_failure = False
    for target in targets:
        pr_number = target["pr_number"]
        issue_number = target["issue_number"]
        branch = target["branch"]
        comments = target["comments"]

        print(
            f"[cai revise] revising PR #{pr_number} (issue #{issue_number}, "
            f"{len(comments)} unaddressed comment(s))",
            flush=True,
        )

        # 1. Lock — add :revising label.
        if not _set_labels(issue_number, add=[LABEL_REVISING], log_prefix="cai revise"):
            print(
                f"[cai revise] could not lock #{issue_number}",
                file=sys.stderr,
            )
            log_run("revise", repo=REPO, pr=pr_number,
                    result="lock_failed", exit=1)
            had_failure = True
            continue

        _run(["gh", "auth", "setup-git"], capture_output=True)

        _uid = uuid.uuid4().hex[:8]
        work_dir = Path(f"/tmp/cai-revise-{issue_number}-{_uid}")

        try:
            if work_dir.exists():
                shutil.rmtree(work_dir)

            # 2. Clone and check out the existing branch.
            clone = _run(
                ["gh", "repo", "clone", REPO, str(work_dir)],
                capture_output=True,
            )
            if clone.returncode != 0:
                print(
                    f"[cai revise] clone failed:\n{clone.stderr}",
                    file=sys.stderr,
                )
                _set_labels(issue_number, remove=[LABEL_REVISING], log_prefix="cai revise")
                log_run("revise", repo=REPO, pr=pr_number,
                        result="clone_failed", exit=1)
                had_failure = True
                continue

            _git(work_dir, "fetch", "origin", branch)
            _git(work_dir, "checkout", branch)

            # 3. Configure git identity.
            name, email = _gh_user_identity()
            _git(work_dir, "config", "user.name", name)
            _git(work_dir, "config", "user.email", email)

            # 3b. Deterministically attempt the rebase onto main.
            #     We always rebase — if main hasn't moved, it's a
            #     no-op. Depending on what's needed, the wrapper
            #     routes to: (a) early exit if clean + no comments,
            #     (b) cai-rebase (haiku) if conflicts + no comments,
            #     or (c) cai-revise (sonnet) if comments ± conflicts.
            _git(work_dir, "fetch", "origin", "main")
            pre_agent_head = _git(
                work_dir, "rev-parse", "HEAD", check=False,
            ).stdout.strip()
            rebase = _git(
                work_dir, "rebase", "origin/main", check=False,
            )

            rebase_merge_dir = work_dir / ".git" / "rebase-merge"
            rebase_apply_dir = work_dir / ".git" / "rebase-apply"
            rebase_in_progress = (
                rebase_merge_dir.exists() or rebase_apply_dir.exists()
            )

            if rebase_in_progress:
                conflict_files = _rebase_conflict_files(work_dir)
                rebase_state_block = (
                    "## Rebase state\n\n"
                    f"**Status:** in progress — {len(conflict_files)} "
                    "conflicted file(s)\n\n"
                    "The wrapper ran `git rebase origin/main` and it "
                    "stopped on conflicts. You must drive the rebase "
                    "to completion before addressing review comments. "
                    "Conflicted files:\n\n"
                    + "\n".join(f"- `{f}`" for f in conflict_files)
                    + "\n"
                )
                print(
                    f"[cai revise] PR #{pr_number}: rebase stopped on "
                    f"{len(conflict_files)} conflict(s); handing to agent",
                    flush=True,
                )
            elif rebase.returncode != 0:
                # Rebase failed but no rebase in progress — anomalous
                # state (git refused to start the rebase at all). Bail
                # loudly rather than hand a broken worktree to the agent.
                print(
                    f"[cai revise] PR #{pr_number}: rebase exit "
                    f"{rebase.returncode} with no in-progress state:\n"
                    f"{rebase.stderr}",
                    file=sys.stderr,
                )
                _set_labels(issue_number, remove=[LABEL_REVISING], log_prefix="cai revise")
                log_run("revise", repo=REPO, pr=pr_number,
                        result="rebase_weird_failure", exit=1)
                had_failure = True
                continue
            else:
                rebase_state_block = (
                    "## Rebase state\n\n"
                    "**Status:** clean — `git rebase origin/main` "
                    "completed without conflicts. You can skip straight "
                    "to addressing review comments (if any).\n"
                )

            # 3c. Early exit: clean rebase with no comments.
            #     If the rebase completed without conflicts AND there
            #     are no unaddressed review comments, skip agent
            #     invocation entirely. Just force-push if HEAD moved
            #     (rebase may have advanced commits) and unlock.
            if not rebase_in_progress and not comments:
                post_rebase_head = _git(
                    work_dir, "rev-parse", "HEAD", check=False,
                ).stdout.strip()
                if pre_agent_head != post_rebase_head:
                    push = _run(
                        ["git", "-C", str(work_dir), "push",
                         "--force-with-lease", "origin", branch],
                        capture_output=True,
                    )
                    if push.returncode != 0:
                        print(
                            f"[cai revise] noop push failed:\n{push.stderr}",
                            file=sys.stderr,
                        )
                        _set_labels(issue_number, remove=[LABEL_REVISING],
                                    log_prefix="cai revise")
                        log_run("revise", repo=REPO, pr=pr_number,
                                result="noop_push_failed", exit=1)
                        had_failure = True
                        continue
                    print(
                        f"[cai revise] clean rebase pushed for PR #{pr_number} "
                        "(no comments to address)",
                        flush=True,
                    )
                else:
                    print(
                        f"[cai revise] PR #{pr_number}: rebase was no-op and "
                        "no comments to address; skipping agent",
                        flush=True,
                    )
                _set_labels(issue_number, remove=[LABEL_REVISING],
                            log_prefix="cai revise")
                log_run("revise", repo=REPO, pr=pr_number,
                        result="noop_clean", exit=0)
                continue

            # 4. Fetch original issue body.
            try:
                issue_data = _gh_json([
                    "issue", "view", str(issue_number),
                    "--repo", REPO,
                    "--json", "number,title,body",
                ])
            except subprocess.CalledProcessError:
                issue_data = {"number": issue_number, "title": "(unknown)", "body": ""}

            # 4b. Describe the PR's current state to the agent.
            #
            #     Historically this block dumped the full unified
            #     `gh pr diff` into the user message — a large token
            #     sink on PRs that touch many lines, especially since
            #     cai-revise runs every cycle for the full lifetime
            #     of a PR. The full diff is now gone entirely: the
            #     agent gets a compact `git diff origin/main..HEAD
            #     --stat` summary as a file-level map, and explores
            #     the clone itself (Read, Grep, Glob, and delegation
            #     to Explore) when it needs the actual content.
            #
            #     When `.cai/pr-context.md` is present (`cai-implement`
            #     writes it on every non-empty PR), the dossier is
            #     the richer map and the agent Reads it first. When
            #     it is missing (legacy PRs, or PRs where cai-implement
            #     exited with zero diff), the stat alone is enough —
            #     the agent uses it as the entry point and explores
            #     from there, then writes a fresh dossier before
            #     exiting so the next revise cycle has one.
            dossier_path = work_dir / ".cai" / "pr-context.md"
            stat_result = _git(
                work_dir, "diff", "origin/main..HEAD", "--stat",
                check=False,
            )
            pr_stat = (stat_result.stdout or "").strip() or (
                "(no changes vs origin/main)"
            )
            if dossier_path.exists():
                pr_state_block = (
                    f"## Current PR state\n\n"
                    f"A PR context dossier is present at "
                    f"`{work_dir}/.cai/pr-context.md` — **Read it "
                    f"first.** It lists the files touched, key "
                    f"symbols, design decisions, out-of-scope gaps, "
                    f"and invariants the change relies on. Use it as "
                    f"the ground-truth map of what this PR is doing "
                    f"and Read specific files in the clone for the "
                    f"actual current content.\n\n"
                    f"The full unified diff is **not** included — "
                    f"the dossier plus on-demand Reads is cheaper "
                    f"and more accurate. A `git diff "
                    f"origin/main..HEAD --stat` summary follows as a "
                    f"file-level map:\n\n"
                    f"```\n{pr_stat}\n```\n\n"
                )
            else:
                pr_state_block = (
                    f"## Current PR state\n\n"
                    f"_No `.cai/pr-context.md` dossier was found — "
                    f"this is a legacy PR or one where `cai-implement` "
                    f"exited with zero diff. The full unified diff "
                    f"is **not** included either — it is a token "
                    f"sink on large PRs and you can reconstruct the "
                    f"same information more accurately by Reading "
                    f"files in the clone directly._\n\n"
                    f"A `git diff origin/main..HEAD --stat` summary "
                    f"follows as a file-level map. **Use it as your "
                    f"entry point:** Read the listed files in the "
                    f"clone to see the actual current content, use "
                    f"Grep/Glob or the Explore subagent for any "
                    f"broader context you need, and — if you make "
                    f"code changes in this revision — create a "
                    f"minimal dossier at "
                    f"`{work_dir}/.cai/pr-context.md` before exiting "
                    f"(see `.claude/agents/cai-implement.md` → 'Before you "
                    f"exit: write the PR context dossier') so the "
                    f"next revise cycle starts with one.\n\n"
                    f"```\n{pr_stat}\n```\n\n"
                )

            # 5. Build the user message. The system prompt, tool
            #    allowlist (Agent + edit tools), and hard rules all
            #    live in `.claude/agents/cai-revise.md`.
            comments_section = "## Unaddressed review comments\n\n"
            if comments:
                for c in comments:
                    author = c.get("author", {}).get("login", "unknown")
                    body = c.get("body", "")
                    created = c.get("createdAt", "")
                    # Strip boilerplate from review-pr findings comments so
                    # the revise agent only sees the actionable ### Finding:
                    # blocks, not the fixed preamble/footer.
                    stripped = body.lstrip()
                    if (
                        stripped.startswith(_REVIEW_COMMENT_HEADING_FINDINGS)
                        and not stripped.startswith(_REVIEW_COMMENT_HEADING_CLEAN)
                    ):
                        body = _strip_review_pr_boilerplate(body)
                    comments_section += (
                        f"### Comment by @{author} ({created})\n\n"
                        f"{body}\n\n"
                    )
            else:
                comments_section += (
                    "(none — only the rebase needed attention)\n"
                )

            _issue_body_raw = issue_data.get("body") or "(no body)"
            _issue_num = issue_data["number"]
            _issue_body = _extract_revise_context(_issue_body_raw)

            user_message = (
                _work_directory_block(work_dir)
                + "\n"
                + f"{rebase_state_block}\n"
                + "## Original issue\n\n"
                + f"### #{_issue_num} — {issue_data.get('title', '')}\n\n"
                + f"{_issue_body}\n\n"
                + pr_state_block
                + comments_section
            )

            # 5b. Pre-create the `.cai-staging/agents/` directory so
            #     the agent has somewhere to write proposed updates
            #     to its own `.claude/agents/*.md` file(s). See
            #     `_setup_agent_edit_staging` for why we need this
            #     workaround.
            _setup_agent_edit_staging(work_dir)

            # 5c. Choose agent: rebase-only conflicts → haiku agent,
            #     otherwise → full cai-revise.
            rebase_only = rebase_in_progress and not comments
            agent_name = "cai-rebase" if rebase_only else "cai-revise"

            # 6. Invoke the declared subagent.
            #    Runs with `cwd=/app` and `--add-dir <work_dir>` so
            #    the agent reads its own definition (and memory)
            #    from the canonical /app paths while operating on
            #    the clone via absolute paths.
            #
            #    `--dangerously-skip-permissions` is required for
            #    the permission gating on file Edit/Write in the
            #    clone. Claude-code's hardcoded `.claude/agents/*.md`
            #    protection is NOT bypassed by any flag — we route
            #    self-modifications through the staging directory
            #    instead (see _work_directory_block).
            #
            #    cai-revise/cai-rebase delegate git rebase ops to the
            #    cai-git haiku subagent via the Agent tool instead of
            #    running git commands directly — see the respective
            #    agent definition files for details.
            print(
                f"[cai revise] running {agent_name} subagent for {work_dir}",
                flush=True,
            )
            agent = _run_claude_p(
                ["claude", "-p", "--agent", agent_name,
                 "--dangerously-skip-permissions",
                 "--add-dir", str(work_dir)],
                category="revise",
                agent=agent_name,
                input=user_message,
                cwd="/app",
            )
            if agent.stdout:
                print(agent.stdout, flush=True)

            # 6b. Apply any `.claude/agents/**/*.md` updates the agent
            #     staged at `<work_dir>/.cai-staging/agents/`. We
            #     apply UNCONDITIONALLY (even on agent non-zero
            #     exit) because cai-revise's return code is
            #     dominated by rebase outcome — the agent may have
            #     completed a valid self-modification before
            #     hitting an unrelated rebase failure, and we'd
            #     rather preserve that work than silently discard
            #     it. If we end up rolling back the branch below,
            #     the staged edits go with it.
            applied = _apply_agent_edit_staging(work_dir)
            if applied:
                print(
                    f"[cai revise] applied {applied} staged "
                    f".claude/agents/**/*.md update(s)",
                    flush=True,
                )

            agent_summary = (agent.stdout or "").strip()[:4000]

            # 7. Trust but verify the final state. The agent may have
            #    (a) resolved a rebase and addressed comments, (b)
            #    resolved a rebase only, (c) addressed comments only,
            #    or (d) aborted the rebase because a conflict was
            #    ambiguous. All four cases need distinct handling.
            rebase_still_in_progress = (
                rebase_merge_dir.exists() or rebase_apply_dir.exists()
            )
            remaining_conflicts = _rebase_conflict_files(work_dir)

            rebase_failure = (
                rebase_still_in_progress
                or bool(remaining_conflicts)
                or agent.returncode != 0
            )

            if not rebase_failure:
                # Also check: HEAD must contain origin/main as an
                # ancestor. If the agent ran `git rebase --abort`,
                # the rebase is "not in progress" but HEAD is back
                # where it started — not on top of main.
                ancestry = _run(
                    ["git", "-C", str(work_dir), "merge-base",
                     "--is-ancestor", "origin/main", "HEAD"],
                    capture_output=True,
                )
                if ancestry.returncode != 0:
                    rebase_failure = True

            if rebase_failure:
                if rebase_still_in_progress:
                    _git(work_dir, "rebase", "--abort", check=False)
                comment_body = (
                    "## Revise subagent: rebase resolution failed\n\n"
                    "Could not auto-rebase against current main, and "
                    "the revise subagent could not resolve the "
                    "conflicts cleanly. Please rebase manually."
                )
                if agent_summary:
                    comment_body += (
                        "\n\n<details><summary>Agent notes</summary>"
                        f"\n\n{agent_summary}\n\n</details>"
                    )
                _run(
                    ["gh", "pr", "comment", str(pr_number),
                     "--repo", REPO, "--body", comment_body],
                    capture_output=True,
                )
                print(
                    f"[cai revise] rebase/agent failed for PR #{pr_number}; "
                    "posted comment",
                    flush=True,
                )
                _set_labels(issue_number, remove=[LABEL_REVISING], log_prefix="cai revise")
                log_run("revise", repo=REPO, pr=pr_number,
                        result="rebase_failed", exit=0)
                continue

            # 8. Determine what work actually happened.
            post_agent_head = _git(
                work_dir, "rev-parse", "HEAD", check=False,
            ).stdout.strip()
            status = _git(work_dir, "status", "--porcelain", check=False)
            has_uncommitted = bool(status.stdout.strip())
            head_changed = pre_agent_head != post_agent_head

            if not has_uncommitted and not head_changed:
                # Nothing happened — rebase was clean AND the agent
                # decided no comments were actionable.
                reasoning = agent_summary or "(no output from agent)"
                comment_body = (
                    f"## Revise subagent: no additional changes\n\n"
                    f"{reasoning}\n\n"
                    f"---\n"
                    f"_The revise subagent reviewed the comments but "
                    f"did not find actionable changes to make._"
                )
                _run(
                    ["gh", "pr", "comment", str(pr_number),
                     "--repo", REPO, "--body", comment_body],
                    capture_output=True,
                )
                _set_labels(issue_number, remove=[LABEL_REVISING], log_prefix="cai revise")
                print(
                    f"[cai revise] no changes for PR #{pr_number}; "
                    "posted comment",
                    flush=True,
                )
                log_run("revise", repo=REPO, pr=pr_number,
                        comments_addressed=0, exit=0)
                continue

            # 9. Commit any uncommitted review-comment edits.
            if has_uncommitted:
                _git(work_dir, "add", "-A")
                commit_msg = (
                    f"auto-improve: revise per review comments\n\n"
                    f"Refs {REPO}#{issue_number}"
                )
                _git(work_dir, "commit", "-m", commit_msg)

            # 10. Force-push the rebased and/or revised branch.
            push = _run(
                ["git", "-C", str(work_dir), "push", "--force-with-lease",
                 "origin", branch],
                capture_output=True,
            )
            if push.returncode != 0:
                print(
                    f"[cai revise] git push failed:\n{push.stderr}",
                    file=sys.stderr,
                )
                _set_labels(issue_number, remove=[LABEL_REVISING], log_prefix="cai revise")
                log_run("revise", repo=REPO, pr=pr_number,
                        result="push_failed", exit=1)
                had_failure = True
                continue

            print(
                f"[cai revise] force-pushed revision to {branch}",
                flush=True,
            )

            # 11a. Advance FSM state: branch now has new commits, so any
            # post-review state must drop back to REVIEWING_CODE for the
            # new SHA.
            try:
                pr_now = _gh_json([
                    "pr", "view", str(pr_number),
                    "--repo", REPO, "--json", "labels,mergedAt,state",
                ])
            except subprocess.CalledProcessError:
                pr_now = {}
            current_state = get_pr_state(pr_now) if pr_now else None
            if current_state == PRState.REVISION_PENDING:
                fire_trigger(
                    pr_number, "revision_pending_to_reviewing_code",
                    is_pr=True,
                    log_prefix="cai revise",
                )
            elif current_state == PRState.CI_FAILING:
                fire_trigger(
                    pr_number, "ci_failing_to_reviewing_code",
                    is_pr=True,
                    log_prefix="cai revise",
                )
            elif current_state == PRState.REVIEWING_DOCS:
                fire_trigger(
                    pr_number, "reviewing_docs_to_reviewing_code",
                    is_pr=True,
                    log_prefix="cai revise",
                )
            # REVIEWING_CODE is already correct; OPEN / PR_HUMAN_NEEDED
            # / MERGED are unexpected here and left for the sweep to
            # reconcile.

            # 11. Post a summary comment describing what happened.
            if head_changed and has_uncommitted:
                summary_suffix = (
                    f"{len(comments)} review comment(s) addressed; "
                    "rebase was resolved by the subagent"
                )
            elif head_changed:
                summary_suffix = "rebase was resolved by the subagent"
            else:
                summary_suffix = (
                    f"{len(comments)} review comment(s) addressed; "
                    "rebase was already clean"
                )
            revision_comment = (
                f"## Revision summary\n\n"
                f"{agent_summary}\n\n"
                f"---\n"
                f"_Applied by `cai revise`. {summary_suffix}._\n"
            )
            _run(
                ["gh", "pr", "comment", str(pr_number),
                 "--repo", REPO, "--body", revision_comment],
                capture_output=True,
            )

            # 12. Remove lock label.
            _set_labels(issue_number, remove=[LABEL_REVISING], log_prefix="cai revise")
            log_run("revise", repo=REPO, pr=pr_number,
                    comments_addressed=len(comments), exit=0)

        except Exception as e:
            print(f"[cai revise] unexpected failure: {e!r}", file=sys.stderr)
            _set_labels(issue_number, remove=[LABEL_REVISING], log_prefix="cai revise")
            log_run("revise", repo=REPO, pr=pr_number,
                    result="unexpected_error", exit=1)
            had_failure = True
        finally:
            if work_dir.exists():
                shutil.rmtree(work_dir, ignore_errors=True)

    return 1 if had_failure else 0
