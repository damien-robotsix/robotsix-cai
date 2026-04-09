"""cai revise — iterate on open PRs based on review comments."""

import json
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from cai_common import (
    LABEL_PR_OPEN,
    LABEL_REVISING,
    REPO,
    REVISE_PROMPT,
    _gh_json,
    _gh_user_identity,
    _git,
    _run,
    _set_labels,
    log_run,
)


# Heading markers used by cmd_fix and cmd_revise when they post
# comments to PRs/issues. The revise subcommand uses these to filter
# out bot-generated comments from the "unaddressed" set, which would
# otherwise cause self-loops (the bot acting on its own output).
#
# Login-based self-filtering doesn't work reliably in cai's common
# deployment pattern: the container uses the human operator's gh token,
# so the bot's "identity" is the same as the user's. Content-based
# marker matching is the robust alternative.
_BOT_COMMENT_MARKERS = (
    "## Fix subagent:",
    "## Revise subagent:",
    "## Revision summary",
)


def _is_bot_comment(comment: dict) -> bool:
    """Return True if a comment body looks like it was posted by a cai subagent."""
    body = (comment.get("body") or "").lstrip()
    return any(body.startswith(m) for m in _BOT_COMMENT_MARKERS)


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


def _select_revise_targets() -> list[dict]:
    """Return PRs needing revision (unaddressed comments since last commit).

    Eligible = branch matches auto-improve/<N>-* AND linked issue has
    label auto-improve:pr-open. Returns a list of dicts with keys:
    pr_number, issue_number, branch, comments (the unaddressed ones).

    Reads BOTH issue-level comments (via `gh pr list --json comments`)
    and line-by-line review comments (via `gh api .../pulls/N/comments`)
    so reviewers can leave either kind of feedback.
    """
    try:
        prs = _gh_json([
            "pr", "list",
            "--repo", REPO,
            "--state", "open",
            "--json", "number,headRefName,comments",
            "--limit", "50",
        ]) or []
    except subprocess.CalledProcessError as e:
        print(f"[cai revise] gh pr list failed:\n{e.stderr}", file=sys.stderr)
        return []

    targets = []
    for pr in prs:
        branch = pr.get("headRefName", "")
        m = re.match(r"^auto-improve/(\d+)-", branch)
        if not m:
            continue
        issue_number = int(m.group(1))

        # Check that the linked issue has :pr-open label.
        try:
            issue = _gh_json([
                "issue", "view", str(issue_number),
                "--repo", REPO,
                "--json", "labels,state",
            ])
        except subprocess.CalledProcessError:
            continue
        if not issue or issue.get("state", "").upper() != "OPEN":
            continue
        label_names = {lbl["name"] for lbl in issue.get("labels", [])}
        if LABEL_PR_OPEN not in label_names:
            continue

        # Find the most recent commit timestamp on the branch.
        try:
            commit_info = _gh_json([
                "api", f"repos/{REPO}/commits",
                "--jq", ".[0].commit.committer.date",
                "-q", "sha=" + branch,
            ])
        except Exception:
            commit_info = None

        # Use gh pr view to get the last commit date more reliably.
        try:
            pr_detail = _gh_json([
                "pr", "view", str(pr["number"]),
                "--repo", REPO,
                "--json", "commits",
            ])
            commits = pr_detail.get("commits", [])
            if commits:
                last_commit_date = commits[-1].get("committedDate", "")
            else:
                last_commit_date = ""
        except Exception:
            last_commit_date = ""

        if not last_commit_date:
            continue

        # Parse commit timestamp.
        try:
            commit_ts = datetime.strptime(
                last_commit_date, "%Y-%m-%dT%H:%M:%SZ"
            ).replace(tzinfo=timezone.utc)
        except ValueError:
            continue

        # Merge issue-level and line-by-line review comments.
        issue_comments = pr.get("comments", [])
        line_comments = _fetch_review_comments(pr["number"])
        comments = issue_comments + line_comments

        # Filter: createdAt > commit timestamp AND not bot-generated.
        unaddressed = []
        for c in comments:
            try:
                c_ts = datetime.strptime(
                    c["createdAt"], "%Y-%m-%dT%H:%M:%SZ"
                ).replace(tzinfo=timezone.utc)
            except (ValueError, KeyError):
                continue
            if c_ts <= commit_ts:
                continue
            if _is_bot_comment(c):
                continue
            unaddressed.append(c)

        if not unaddressed:
            continue

        targets.append({
            "pr_number": pr["number"],
            "issue_number": issue_number,
            "branch": branch,
            "comments": unaddressed,
        })

    return targets


def cmd_revise(args) -> int:
    """Iterate on open PRs based on review comments."""
    print("[cai revise] checking for PRs with unaddressed comments", flush=True)

    targets = _select_revise_targets()
    if not targets:
        print("[cai revise] no PRs need revision; nothing to do", flush=True)
        log_run("revise", repo=REPO, result="no_targets", exit=0)
        return 0

    print(f"[cai revise] found {len(targets)} PR(s) to revise", flush=True)

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
        if not _set_labels(issue_number, add=[LABEL_REVISING]):
            print(
                f"[cai revise] could not lock #{issue_number}",
                file=sys.stderr,
            )
            log_run("revise", repo=REPO, pr=pr_number,
                    result="lock_failed", exit=1)
            continue

        _run(["gh", "auth", "setup-git"], capture_output=True)

        work_dir = Path(f"/tmp/cai-revise-{issue_number}")

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
                _set_labels(issue_number, remove=[LABEL_REVISING])
                log_run("revise", repo=REPO, pr=pr_number,
                        result="clone_failed", exit=1)
                continue

            _git(work_dir, "fetch", "origin", branch)
            _git(work_dir, "checkout", branch)

            # 3. Configure git identity.
            name, email = _gh_user_identity()
            _git(work_dir, "config", "user.name", name)
            _git(work_dir, "config", "user.email", email)

            # 4. Fetch original issue body.
            try:
                issue_data = _gh_json([
                    "issue", "view", str(issue_number),
                    "--repo", REPO,
                    "--json", "number,title,body",
                ])
            except subprocess.CalledProcessError:
                issue_data = {"number": issue_number, "title": "(unknown)", "body": ""}

            # Get current PR diff.
            diff_result = _run(
                ["gh", "pr", "diff", str(pr_number), "--repo", REPO],
                capture_output=True,
            )
            pr_diff = diff_result.stdout if diff_result.returncode == 0 else "(could not fetch diff)"

            # 5. Build the revise prompt.
            prompt_text = REVISE_PROMPT.read_text()
            comments_section = "## Unaddressed review comments\n\n"
            for c in comments:
                author = c.get("author", {}).get("login", "unknown")
                body = c.get("body", "")
                created = c.get("createdAt", "")
                comments_section += (
                    f"### Comment by @{author} ({created})\n\n"
                    f"{body}\n\n"
                )

            full_prompt = (
                f"{prompt_text}\n\n"
                f"## Original issue\n\n"
                f"### #{issue_data['number']} \u2014 {issue_data.get('title', '')}\n\n"
                f"{issue_data.get('body') or '(no body)'}\n\n"
                f"## Current PR diff\n\n"
                f"```diff\n{pr_diff}\n```\n\n"
                f"{comments_section}"
            )

            # 6. Run the revise subagent.
            print(f"[cai revise] running revise subagent in {work_dir}", flush=True)
            agent = _run(
                ["claude", "-p", "--permission-mode", "acceptEdits",
                 "--disallowedTools", "Bash"],
                input=full_prompt,
                cwd=str(work_dir),
                capture_output=True,
            )
            if agent.stdout:
                print(agent.stdout, flush=True)
            if agent.returncode != 0:
                print(
                    f"[cai revise] subagent failed (exit {agent.returncode}):\n"
                    f"{agent.stderr}",
                    file=sys.stderr,
                )
                _set_labels(issue_number, remove=[LABEL_REVISING])
                log_run("revise", repo=REPO, pr=pr_number,
                        comments_addressed=0, exit=agent.returncode)
                continue

            # 7. Inspect the working tree.
            status = _git(work_dir, "status", "--porcelain", check=False)
            if not status.stdout.strip():
                # Empty diff — post a comment explaining.
                reasoning = (agent.stdout or "").strip()[:2000]
                comment_body = (
                    f"## Revise subagent: no additional changes\n\n"
                    f"{reasoning}\n\n"
                    f"---\n"
                    f"_The revise subagent reviewed the comments but did not "
                    f"find actionable changes to make._"
                )
                _run(
                    ["gh", "pr", "comment", str(pr_number),
                     "--repo", REPO, "--body", comment_body],
                    capture_output=True,
                )
                _set_labels(issue_number, remove=[LABEL_REVISING])
                print(
                    f"[cai revise] no changes for PR #{pr_number}; "
                    "posted comment",
                    flush=True,
                )
                log_run("revise", repo=REPO, pr=pr_number,
                        comments_addressed=0, exit=0)
                continue

            # 8. Commit and force-push.
            _git(work_dir, "add", "-A")
            commit_msg = (
                f"auto-improve: revise per review comments\n\n"
                f"Refs damien-robotsix/robotsix-cai#{issue_number}"
            )
            _git(work_dir, "commit", "-m", commit_msg)

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
                _set_labels(issue_number, remove=[LABEL_REVISING])
                log_run("revise", repo=REPO, pr=pr_number,
                        result="push_failed", exit=1)
                continue

            print(f"[cai revise] force-pushed revision to {branch}", flush=True)

            # 9. Remove lock label.
            _set_labels(issue_number, remove=[LABEL_REVISING])
            log_run("revise", repo=REPO, pr=pr_number,
                    comments_addressed=len(comments), exit=0)

        except Exception as e:
            print(f"[cai revise] unexpected failure: {e!r}", file=sys.stderr)
            _set_labels(issue_number, remove=[LABEL_REVISING])
            log_run("revise", repo=REPO, pr=pr_number,
                    result="unexpected_error", exit=1)
        finally:
            if work_dir.exists():
                shutil.rmtree(work_dir, ignore_errors=True)

    return 0
