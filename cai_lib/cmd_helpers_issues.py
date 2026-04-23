"""Issue-lifecycle helpers for cai action wrappers."""

import re
import subprocess
import sys

from cai_lib.config import REPO, LABEL_RAISED
from cai_lib.github import _gh_json, _strip_cost_comments
from cai_lib.subprocess_utils import _run


# ---------------------------------------------------------------------------
# Files-to-change section parser (shared with plan.py, implement.py,
# merge.py, and cmd_helpers_git.py).
# ---------------------------------------------------------------------------

# Case-insensitive "### Files to change" header; section body captured in
# group 1, bounded by the next "### " heading or end of text.
_FILES_TO_CHANGE_SECTION_RE = re.compile(
    r"^###\s+Files\s+to\s+change\s*$\n(.*?)(?=^###\s|\Z)",
    re.IGNORECASE | re.DOTALL | re.MULTILINE,
)

# Match backticked path tokens of the form ``path/with.ext`` — requires
# at least one ``/`` and an extension, so free-standing symbol names
# (e.g. ``parse_config``) and extensionless bare names are ignored.
_FILES_TO_CHANGE_PATH_RE = re.compile(
    r"`([^`\s]+/[^`\s]*\.[A-Za-z0-9]+)`"
)


def _parse_files_to_change(issue_body: str) -> list[str]:
    """Return the list of relative file paths declared in the issue body's
    ``### Files to change`` section.

    Paths are extracted from backtick-quoted ``path/with.ext`` tokens.
    Returns an empty list when the section is absent or contains no paths.
    """
    if not issue_body:
        return []
    section = _FILES_TO_CHANGE_SECTION_RE.search(issue_body)
    if not section:
        return []
    return _FILES_TO_CHANGE_PATH_RE.findall(section.group(1))


def _parse_oob_issues(agent_output: str) -> list[dict]:
    """Extract out-of-scope issue blocks from a review agent's output.

    The agent can emit blocks like:

        ## Out-of-scope Issue
        ### Title
        <title text>
        ### Body
        <body text>

    Returns a list of dicts with 'title' and 'body' keys.
    """
    issues: list[dict] = []
    parts = re.split(r"^## Out-of-scope Issue\s*$", agent_output, flags=re.MULTILINE)
    for part in parts[1:]:  # skip everything before the first marker
        title = ""
        body = ""
        title_match = re.search(
            r"^### Title\s*\n(.*?)(?=^### |\Z)",
            part,
            flags=re.MULTILINE | re.DOTALL,
        )
        body_match = re.search(
            r"^### Body\s*\n(.*?)(?=^## |\Z)",
            part,
            flags=re.MULTILINE | re.DOTALL,
        )
        if title_match:
            title = title_match.group(1).strip()
        if body_match:
            body = body_match.group(1).strip()
        if title:
            issues.append({"title": title, "body": body})
    return issues


def _create_oob_issues(
    issues: list[dict], pr_number: int, caller_label: str
) -> int:
    """Create GitHub issues for out-of-scope findings from a review agent.

    *caller_label* is used in log messages and the issue attribution footer
    (e.g. ``"cai review-pr"`` or ``"cai review-docs"``).

    Returns the count of successfully created issues.
    """
    created = 0
    for s in issues:
        issue_body = (
            f"{s['body']}\n\n"
            f"---\n"
            f"_Raised by `{caller_label}` while reviewing PR #{pr_number}._\n"
        )
        labels = ",".join(["auto-improve", LABEL_RAISED])
        result = _run(
            [
                "gh", "issue", "create",
                "--repo", REPO,
                "--title", s["title"],
                "--body", issue_body,
                "--label", labels,
            ],
            capture_output=True,
        )
        if result.returncode == 0:
            url = result.stdout.strip()
            print(f"[{caller_label}] created out-of-scope issue: {url}", flush=True)
            created += 1
        else:
            print(
                f"[{caller_label}] failed to create out-of-scope issue "
                f"'{s['title']}': {result.stderr}",
                file=sys.stderr,
            )
    return created


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

        comments = _strip_cost_comments(pr_data.get("comments", []))

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
