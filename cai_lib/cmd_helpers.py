"""Cross-command helpers shared between cai.py and cai_lib/actions/*."""

import json
import re
import shutil
import subprocess
import sys

from datetime import datetime, timezone
from pathlib import Path

from cai_lib.config import (
    REPO,
    LABEL_PR_OPEN,
    LABEL_REVISING,
    LABEL_MERGE_BLOCKED,
    LABEL_PR_NEEDS_HUMAN,
)
from cai_lib.github import _gh_json, _set_labels
from cai_lib.subprocess_utils import _run


# Paths of the staging directories inside a cloned worktree, relative
# to the clone root.
AGENT_EDIT_STAGING_REL = Path(".cai-staging") / "agents"
PLUGIN_STAGING_REL = Path(".cai-staging") / "plugins"
CLAUDEMD_STAGING_REL = Path(".cai-staging") / "claudemd"


# IMPORTANT: only "no-action" / "summary" bot comments belong here.
# Comments that contain ACTIONABLE content for the revise subagent
# (most notably review-pr findings) must NOT be in this list — they
# need to flow through to the unaddressed set so revise can act on
# them. The "## cai pre-merge review (clean)" form is filtered (no
# findings → nothing for revise to do). The plain "## cai pre-merge
# review" form is NOT filtered because it carries `### Finding:`
# blocks that revise should address.
_BOT_COMMENT_MARKERS = (
    "## Implement subagent:",
    "## Fix subagent:",  # compat: pre-rename bot comments
    "## Revise subagent:",
    "## Revision summary",
    "## CI-fix subagent:",
    "## cai pre-merge review (clean)",
    "## cai docs review (clean)",
    "## cai docs review (applied)",
    "## cai merge verdict",
)


# Duplicates of module-level markers in cai.py. Kept in sync with the
# cai.py definitions; these copies exist so cmd_helpers is importable
# without a circular dependency on cai.py.
_NO_ADDITIONAL_CHANGES_MARKER = "## Revise subagent: no additional changes"
_REBASE_FAILED_MARKER = "## Revise subagent: rebase resolution failed"


def _gh_user_identity() -> tuple[str, str]:
    """Resolve the gh-token owner's git name and email."""
    user = _gh_json(["api", "user"])
    name = user.get("name") or user["login"]
    email = user.get("email") or f"{user['id']}+{user['login']}@users.noreply.github.com"
    return name, email


def _fetch_previous_fix_attempts(issue_number: int) -> list[dict]:
    """Retrieve closed, unmerged PRs for this issue and extract merge verdicts.

    Returns a list of dicts with keys: pr_number, title, merge_verdict,
    review_summary. Entries with no extractable verdict are omitted.
    Capped at the 3 most recently created PRs.
    """
    try:
        prs = _gh_json([
            "pr", "list",
            "--repo", REPO,
            "--state", "closed",
            "--search", f'"Refs {REPO}#{issue_number}" in:body',
            "--json", "number,title,headRefName,createdAt,mergedAt",
            "--limit", "10",
        ]) or []
    except subprocess.CalledProcessError:
        return []

    # Filter to unmerged (closed without merge), sort newest-first, cap at 3.
    unmerged = [p for p in prs if not p.get("mergedAt")]
    unmerged.sort(key=lambda p: p["createdAt"], reverse=True)
    unmerged = unmerged[:3]

    if not unmerged:
        return []

    results = []
    for pr in unmerged:
        pr_number = pr["number"]
        try:
            pr_data = _gh_json([
                "pr", "view", str(pr_number),
                "--repo", REPO,
                "--json", "comments",
            ]) or {}
        except subprocess.CalledProcessError:
            continue

        comments = pr_data.get("comments", [])

        merge_verdict = None
        review_summary = None
        for comment in reversed(comments):
            body = comment.get("body", "")
            if merge_verdict is None and "## Merge Verdict" in body:
                truncated = body[:500]
                if len(body) > 500:
                    truncated += "…"
                merge_verdict = truncated
            if review_summary is None and "### Finding:" in body:
                truncated = body[:300]
                if len(body) > 300:
                    truncated += "…"
                review_summary = truncated

        if merge_verdict is not None:
            results.append({
                "pr_number": pr_number,
                "title": pr["title"],
                "merge_verdict": merge_verdict,
                "review_summary": review_summary,
            })

    return results


def _build_attempt_history_block(attempts: list[dict]) -> str:
    """Format previous fix attempts as a markdown section.

    Returns empty string when attempts is empty so callers can
    unconditionally append without adding spurious content.
    """
    if not attempts:
        return ""
    block = "\n## Previous Fix Attempts\n\n"
    for attempt in attempts:
        block += f"### PR #{attempt['pr_number']}: {attempt['title']}\n\n"
        block += f"**Merge verdict:**\n{attempt['merge_verdict']}\n\n"
        if attempt.get("review_summary"):
            block += f"**Review summary:**\n{attempt['review_summary']}\n\n"
    return block


def _extract_stored_plan(issue_body: str) -> str | None:
    """Extract the stored plan from an issue body, or None if not present."""
    start_marker = "<!-- cai-plan-start -->"
    end_marker = "<!-- cai-plan-end -->"
    start = issue_body.find(start_marker)
    end = issue_body.find(end_marker)
    if start == -1 or end == -1 or end <= start:
        return None
    content = issue_body[start + len(start_marker):end].strip()
    heading = "## Selected Implementation Plan"
    if content.startswith(heading):
        content = content[len(heading):].strip()
    return content if content else None


def _strip_stored_plan_block(issue_body: str) -> str:
    """Remove an existing cai-plan block from the issue body, if present."""
    start_marker = "<!-- cai-plan-start -->"
    end_marker = "<!-- cai-plan-end -->"
    start = issue_body.find(start_marker)
    end = issue_body.find(end_marker)
    if start == -1 or end == -1 or end <= start:
        return issue_body
    # Remove from start_marker through end_marker plus any trailing newlines.
    after = end + len(end_marker)
    while after < len(issue_body) and issue_body[after] == "\n":
        after += 1
    return issue_body[:start] + issue_body[after:]


def _setup_agent_edit_staging(work_dir: Path) -> Path:
    """Create the staging directories where agents write proposed
    `.claude/agents/*.md` and `.claude/plugins/` updates. Idempotent.

    Returns the absolute agent-staging directory path so the caller can
    pass it to the agent via the user message.
    """
    staging = work_dir / AGENT_EDIT_STAGING_REL
    staging.mkdir(parents=True, exist_ok=True)
    plugin_staging = work_dir / PLUGIN_STAGING_REL
    plugin_staging.mkdir(parents=True, exist_ok=True)
    claudemd_staging = work_dir / CLAUDEMD_STAGING_REL
    claudemd_staging.mkdir(parents=True, exist_ok=True)
    return staging


def _apply_agent_edit_staging(work_dir: Path) -> int:
    """Copy any files staged at `<work_dir>/.cai-staging/agents/`
    back to `<work_dir>/.claude/agents/`, copy any plugin tree staged
    at `<work_dir>/.cai-staging/plugins/` to `<work_dir>/.claude/plugins/`,
    then remove the staging directory so it doesn't land in the PR.

    Security boundaries:

      1. Each staged agent file is copied to `<work_dir>/.claude/agents/`
         using the same basename. If no target exists a new file is
         created; if one exists it is overwritten.
      2. Staged plugin trees are merged into `<work_dir>/.claude/plugins/`
         using shutil.copytree with dirs_exist_ok=True.
      3. The staging dir lives entirely inside `work_dir` so escapes
         via `..` are not possible (the wrapper iterates one
         directory level via `iterdir()` and copies whole files).
      4. The staging dir is removed before commit if all staging
         operations succeeded. If plugin staging fails, the staging
         dir is preserved for inspection and the function returns
         early so staged content is not silently lost.

    Returns the count of files successfully applied. If the staging
    dir doesn't exist or is empty, returns 0 with no side effects.
    """
    staging = work_dir / AGENT_EDIT_STAGING_REL
    applied = 0

    if staging.exists() and staging.is_dir():
        target_dir = work_dir / ".claude" / "agents"
        for staged_file in sorted(staging.iterdir()):
            if not staged_file.is_file():
                continue

            target = target_dir / staged_file.name
            if not target.exists():
                print(
                    f"[cai] agent edit staging: creating new agent file "
                    f".claude/agents/{staged_file.name}",
                    flush=True,
                )

            try:
                content = staged_file.read_text()
                target.write_text(content)
                print(
                    f"[cai] applied staged agent file: "
                    f".claude/agents/{staged_file.name} "
                    f"({len(content)} bytes)",
                    flush=True,
                )
                applied += 1
            except OSError as exc:
                print(
                    f"[cai] agent edit staging: failed to apply "
                    f"{staged_file.name}: {exc}",
                    file=sys.stderr,
                )
                continue

    # Apply any plugin staging: .cai-staging/plugins/ → .claude/plugins/
    plugin_staging = work_dir / PLUGIN_STAGING_REL
    if plugin_staging.exists() and plugin_staging.is_dir():
        plugin_target = work_dir / ".claude" / "plugins"
        try:
            shutil.copytree(str(plugin_staging), str(plugin_target),
                            dirs_exist_ok=True)
            print(
                "[cai] applied staged plugin tree: .claude/plugins/ "
                "(merged from .cai-staging/plugins/)",
                flush=True,
            )
            applied += 1
        except OSError as exc:
            print(
                f"[cai] agent edit staging: failed to apply plugin tree: {exc}",
                file=sys.stderr,
            )
            # Preserve .cai-staging so staged plugin files are not
            # silently lost when the copy fails — caller can inspect
            # or retry. Do not fall through to shutil.rmtree below.
            return applied

    # Apply any CLAUDE.md staging: .cai-staging/claudemd/ → <work_dir>/.
    # Use rglob("CLAUDE.md") so only files literally named CLAUDE.md
    # are copied — stray files in the staging tree are ignored.
    claudemd_staging = work_dir / CLAUDEMD_STAGING_REL
    if claudemd_staging.exists() and claudemd_staging.is_dir():
        for staged_file in sorted(claudemd_staging.rglob("CLAUDE.md")):
            rel = staged_file.relative_to(claudemd_staging)
            target = work_dir / rel
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                content = staged_file.read_text()
                target.write_text(content)
                print(
                    f"[cai] applied staged CLAUDE.md: {rel} "
                    f"({len(content)} bytes)",
                    flush=True,
                )
                applied += 1
            except OSError as exc:
                print(
                    f"[cai] agent edit staging: failed to apply "
                    f"CLAUDE.md at {rel}: {exc}",
                    file=sys.stderr,
                )
                # Preserve .cai-staging so staged files are not
                # silently lost when the copy fails.
                return applied

    # Clean up the entire .cai-staging tree (one level above the
    # agents/ subdir) so nothing leaks into the PR.
    cai_staging_root = work_dir / ".cai-staging"
    if cai_staging_root.exists():
        try:
            shutil.rmtree(cai_staging_root)
        except OSError as exc:
            print(
                f"[cai] agent edit staging: cleanup of "
                f"{cai_staging_root} failed: {exc}",
                file=sys.stderr,
            )

    return applied


def _git(work_dir: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    cmd = ["git", "-C", str(work_dir)] + list(args)
    return subprocess.run(cmd, text=True, check=check, capture_output=True)


def _work_directory_block(work_dir: Path) -> str:
    """Return the standard "## Work directory" user-message section
    that informs a cloned-worktree subagent where its actual work
    happens, and how to update protected `.claude/agents/*.md`
    files via the staging directory.

    All cloned-worktree subagents (cai-implement, cai-revise, cai-rebase,
    cai-review-pr, cai-review-docs, cai-code-audit, cai-propose, cai-propose-review,
    cai-update-check, cai-plan, cai-select, cai-git) are invoked with `cwd=/app`
    rather than `cwd=<clone>`. This makes their canonical agent
    definition (`/app/.claude/agents/<name>.md`) and per-agent memory
    (`/app/.claude/agent-memory/<name>/`) directly available via
    cwd-relative paths.

    The trade-off: the agent must use ABSOLUTE paths to read/edit
    files in the actual clone, since the clone is no longer the
    cwd. This block tells the agent where the clone is and reminds
    it to use absolute paths.

    Self-modification of `.claude/agents/*.md`: claude-code's
    headless `-p` mode hardcodes a protection that blocks
    Edit/Write on any `.claude/agents/*.md` file, regardless of
    `--dangerously-skip-permissions`, `--permission-mode`, or
    `permissions.allow` rules. We work around it with a staging
    directory — see `_setup_agent_edit_staging` /
    `_apply_agent_edit_staging`. The block below tells the agent
    how to use it.
    """
    staging_rel = AGENT_EDIT_STAGING_REL.as_posix()
    staging_abs = (work_dir / AGENT_EDIT_STAGING_REL).as_posix()
    claudemd_abs = (work_dir / CLAUDEMD_STAGING_REL).as_posix()
    return (
        "## Work directory\n\n"
        "You are running with cwd `/app` so your declarative agent "
        "definition and per-agent memory are read from the canonical "
        "image / volume locations. Your actual work happens on a "
        "fresh clone of the repository at:\n\n"
        f"    {work_dir}\n\n"
        "**You MUST use absolute paths under that directory for all "
        "Read/Edit/Write/Glob/Grep calls that target the work.** "
        "Relative paths resolve to `/app` (the canonical, baked-in "
        "version of the repo) which you should treat as read-only. "
        "Edits to `/app/...` would land in the container's writable "
        "layer and be lost on next restart — they would NOT make it "
        "into git.\n\n"
        "Examples:\n"
        f"  - GOOD: `Read(\"{work_dir}/cai.py\")`\n"
        "  - BAD:  `Read(\"cai.py\")`               (reads /app/cai.py)\n"
        f"  - GOOD: `Edit(\"{work_dir}/parse.py\", ...)`\n"
        "  - BAD:  `Edit(\"parse.py\", ...)`        (edits /app/parse.py)\n\n"
        "If you have Bash in your tool allowlist, the same rule "
        "applies: use `git -C` (or absolute paths) for any git "
        "operation that should target the clone, NOT the cwd.\n\n"
        "  - GOOD: `git -C "
        f"{work_dir} status`\n"
        "  - BAD:  `git status`        (reports /app status, not "
        "the clone's)\n\n"
        "## Updating `.claude/agents/*.md` (self-modification)\n\n"
        "Claude-code's headless `-p` mode hardcodes a write block "
        "on every `.claude/agents/*.md` path, regardless of any "
        "permission flag or settings rule. Edit/Write calls against "
        f"`{work_dir}/.claude/agents/<name>.md` WILL fail with a "
        "sensitive-file protection error — this is not something "
        "you can bypass from inside your session.\n\n"
        "The wrapper provides a **staging directory** at:\n\n"
        f"    {staging_abs}\n\n"
        "To update an `.claude/agents/<name>.md` file, use the "
        "Write tool to write the COMPLETE new file content "
        "(frontmatter + body) to "
        f"`{staging_abs}/<name>.md`. After your session exits "
        "successfully the wrapper copies every file it finds in "
        f"`{staging_rel}/` back over the corresponding "
        "`.claude/agents/<same-name>.md` in the clone, then deletes "
        "the staging directory so it never lands in the PR.\n\n"
        "Rules:\n"
        "  - Staged files are copied unconditionally — new agent "
        "definitions are created if no target exists yet.\n"
        "  - Write the FULL file, not a diff or patch. The wrapper "
        "does an unconditional full-file overwrite.\n"
        "  - Use the same basename as the target "
        f"(e.g. `{staging_abs}/cai-implement.md` → "
        f"`{work_dir}/.claude/agents/cai-implement.md`).\n"
        "  - Do NOT attempt `Edit` or `Write` on the protected "
        f"`{work_dir}/.claude/agents/...` path — it will always "
        "fail. Go through the staging dir.\n\n"
        "Example:\n"
        f"  - GOOD: `Write(\"{staging_abs}/cai-implement.md\", "
        "\"<full new file content>\")`\n"
        f"  - BAD:  `Edit(\"{work_dir}/.claude/agents/cai-implement.md\", "
        "...)`  (blocked by claude-code)\n"
        "\n"
        "## Updating `CLAUDE.md` files (self-modification)\n\n"
        "Claude-code's headless `-p` mode also hardcodes a write block "
        "on `CLAUDE.md` files (project-level context files). Edit/Write "
        f"calls against `{work_dir}/CLAUDE.md` or any subdirectory "
        "`CLAUDE.md` WILL fail with a sensitive-file protection error.\n\n"
        "The wrapper provides a **staging directory** at:\n\n"
        f"    {claudemd_abs}\n\n"
        "To update a `CLAUDE.md` file at any path (root or subdirectory), "
        f"write it to `{claudemd_abs}/<same-relative-path>/CLAUDE.md`. "
        "The wrapper scans for all files named exactly `CLAUDE.md` under "
        f"the staging tree and copies each to the matching path in "
        f"`{work_dir}/`, creating parent directories as needed. The "
        "staging directory is then deleted so it never lands in the PR.\n\n"
        "Rules:\n"
        "  - Only files literally named `CLAUDE.md` are copied — other "
        "files in the staging tree are ignored.\n"
        "  - Preserve the full relative path (e.g., to update "
        f"`{work_dir}/CLAUDE.md`, write to "
        f"`{claudemd_abs}/CLAUDE.md`; to update "
        f"`{work_dir}/subdir/CLAUDE.md`, write to "
        f"`{claudemd_abs}/subdir/CLAUDE.md`).\n"
        "  - Write the FULL file, not a diff or patch.\n"
        f"  - Do NOT attempt `Edit` or `Write` on `{work_dir}/CLAUDE.md` "
        "directly — it will always fail. Go through the staging dir.\n\n"
        "Example:\n"
        f"  - GOOD: `Write(\"{claudemd_abs}/CLAUDE.md\", "
        "\"<full new file content>\")`\n"
        f"  - BAD:  `Edit(\"{work_dir}/CLAUDE.md\", ...)`  "
        "(blocked by claude-code)\n"
    )


def _is_bot_comment(comment: dict) -> bool:
    """Return True if a comment body looks like it was posted by a cai subagent."""
    body = (comment.get("body") or "").lstrip()
    return any(body.startswith(m) for m in _BOT_COMMENT_MARKERS)


def _parse_iso_ts(value):
    """Parse an ISO-8601 UTC timestamp ('2026-04-10T00:23:34Z'), return datetime or None."""
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None



def _fetch_review_comments(pr_number: int) -> list[dict]:
    """Fetch line-by-line review comments for a PR, normalized to issue-comment shape.

    `gh pr view --json comments` only returns issue-level comments. Line-
    by-line review comments (left on specific lines in the diff) live on
    a separate REST endpoint. This helper fetches them via `gh api` and
    reshapes each one to match the issue-comment format used by the rest
    of the revise logic: `{author: {login}, createdAt, body}`.

    The body is prefixed with a `(line comment on path:line)` marker so
    the subagent knows where the comment is anchored in the diff.
    """
    try:
        result = _run(
            ["gh", "api", f"repos/{REPO}/pulls/{pr_number}/comments"],
            capture_output=True,
        )
        if result.returncode != 0:
            return []
        raw = json.loads(result.stdout)
    except (json.JSONDecodeError, ValueError):
        return []

    normalized = []
    for c in raw:
        author_login = c.get("user", {}).get("login", "")
        created_at = c.get("created_at", "")
        body = c.get("body", "")
        path = c.get("path", "")
        line_num = c.get("line") or c.get("original_line")
        if path and line_num:
            body = f"(line comment on `{path}:{line_num}`)\n\n{body}"
        elif path:
            body = f"(line comment on `{path}`)\n\n{body}"
        normalized.append({
            "author": {"login": author_login},
            "createdAt": created_at,
            "body": body,
        })
    return normalized


def _pr_set_needs_human(pr_number: int, needs: bool) -> None:
    """Add or remove the `needs-human-review` label on a PR.

    Idempotent: gh silently no-ops if the label is already in the
    requested state. Logged but not fatal on failure — labelling is a
    UX nicety, not a correctness requirement.
    """
    flag = "--add-label" if needs else "--remove-label"
    res = _run(
        ["gh", "pr", "edit", str(pr_number),
         "--repo", REPO, flag, LABEL_PR_NEEDS_HUMAN],
        capture_output=True,
    )
    if res.returncode != 0:
        action = "add" if needs else "remove"
        print(
            f"[cai merge] PR #{pr_number}: could not {action} "
            f"label `{LABEL_PR_NEEDS_HUMAN}`:\n{res.stderr}",
            file=sys.stderr,
        )


def _parse_merge_verdict(text: str) -> dict | None:
    """Extract confidence, action, and reasoning from the agent's output."""
    conf_m = re.search(r"\*\*Confidence:\*\*\s*(high|medium|low)", text, re.IGNORECASE)
    act_m = re.search(r"\*\*Action:\*\*\s*(merge|hold|reject)", text, re.IGNORECASE)
    reason_m = re.search(r"\*\*Reasoning:\*\*\s*(.+)", text, re.IGNORECASE)
    if not conf_m or not act_m:
        return None
    return {
        "confidence": conf_m.group(1).lower(),
        "action": act_m.group(1).lower(),
        "reasoning": reason_m.group(1).strip() if reason_m else "(no reasoning provided)",
    }
