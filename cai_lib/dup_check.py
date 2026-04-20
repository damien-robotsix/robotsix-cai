"""Duplicate / resolved pre-check for freshly raised issues.

Invokes the ``cai-dup-check`` haiku subagent with the target issue
plus a context bundle (other open issues + recent merged PRs) and
parses the structured verdict. Intended to run as a cheap pre-step
before the full ``cai-triage`` agent: a ``HIGH``-confidence
``DUPLICATE`` or ``RESOLVED`` verdict lets the caller close the
issue without invoking triage at all.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from typing import Optional

from cai_lib.config import REPO
from cai_lib.github import _gh_json
from cai_lib.subprocess_utils import _run_claude_p


# ---------------------------------------------------------------------------
# Verdict dataclass + parser
# ---------------------------------------------------------------------------


@dataclass
class DupCheckVerdict:
    verdict: str              # "DUPLICATE" | "RESOLVED" | "NONE"
    confidence: str           # "HIGH" | "MEDIUM" | "LOW"
    target: Optional[int]     # issue number for DUPLICATE
    commit_sha: Optional[str] # sha or "PR #N" for RESOLVED
    reasoning: str

    @property
    def should_close(self) -> bool:
        return self.confidence == "HIGH" and self.verdict in ("DUPLICATE", "RESOLVED")


_VERDICT_RE = re.compile(r"^\s*Verdict\s*[:=]\s*(\w+)", re.IGNORECASE | re.MULTILINE)
_CONFIDENCE_RE = re.compile(r"^\s*Confidence\s*[:=]\s*(\w+)", re.IGNORECASE | re.MULTILINE)
_TARGET_RE = re.compile(r"^\s*Target\s*[:=]\s*#?(\d+)", re.IGNORECASE | re.MULTILINE)
_COMMIT_RE = re.compile(r"^\s*CommitSha\s*[:=]\s*(\S+)", re.IGNORECASE | re.MULTILINE)
_REASONING_RE = re.compile(r"^\s*Reasoning\s*[:=]\s*(.+)$", re.IGNORECASE | re.MULTILINE)


def parse_dup_check_verdict(text: str) -> Optional[DupCheckVerdict]:
    """Parse the structured output of ``cai-dup-check``.

    Returns ``None`` when the required ``Verdict:`` / ``Confidence:``
    fields cannot be found or the verdict value is unrecognised.
    Unrecognised confidence levels are downgraded to ``LOW`` rather
    than rejected, so a malformed-but-safe result still flows through
    to the full triage agent.
    """
    if not text:
        return None

    m_v = _VERDICT_RE.search(text)
    m_c = _CONFIDENCE_RE.search(text)
    if not m_v or not m_c:
        return None

    verdict = m_v.group(1).upper()
    if verdict not in ("DUPLICATE", "RESOLVED", "NONE"):
        return None

    confidence = m_c.group(1).upper()
    if confidence not in ("HIGH", "MEDIUM", "LOW"):
        confidence = "LOW"

    target = None
    m_t = _TARGET_RE.search(text)
    if m_t:
        target = int(m_t.group(1))

    commit_sha = None
    m_cs = _COMMIT_RE.search(text)
    if m_cs:
        commit_sha = m_cs.group(1)

    reasoning = ""
    m_r = _REASONING_RE.search(text)
    if m_r:
        reasoning = m_r.group(1).strip()

    # A DUPLICATE without a target or a RESOLVED without a commit/PR
    # reference is invalid at HIGH confidence — downgrade so the
    # caller doesn't act on it.
    if verdict == "DUPLICATE" and target is None:
        confidence = "LOW"
    if verdict == "RESOLVED" and not commit_sha:
        confidence = "LOW"

    return DupCheckVerdict(
        verdict=verdict,
        confidence=confidence,
        target=target,
        commit_sha=commit_sha,
        reasoning=reasoning,
    )


# ---------------------------------------------------------------------------
# Context assembly
# ---------------------------------------------------------------------------


def build_dup_check_message(
    issue: dict,
    other_issues: list[dict],
    recent_prs: list[dict],
) -> str:
    """Render the inline user message for the cai-dup-check agent."""
    title = issue["title"]
    body = issue.get("body") or "(empty)"
    labels = ", ".join(lb["name"] for lb in issue.get("labels", []))

    other_section = "## Other open issues\n\n"
    if other_issues:
        for oi in other_issues:
            oi_labels = ", ".join(lb["name"] for lb in oi.get("labels", []))
            excerpt = (oi.get("body") or "(empty)")[:400]
            other_section += (
                f"### #{oi['number']} — {oi['title']}\n"
                f"- **Labels:** {oi_labels}\n"
                f"- **Excerpt:** {excerpt}\n\n"
            )
    else:
        other_section += "(none)\n\n"

    pr_section = "## Recent merged PRs\n\n"
    merged = [pr for pr in recent_prs if pr.get("mergedAt")]
    if merged:
        for pr in merged:
            excerpt = (pr.get("body") or "")[:400]
            pr_section += (
                f"### PR #{pr['number']} — {pr['title']}\n"
                f"- **Merged:** {pr['mergedAt']}\n"
                f"- **Excerpt:** {excerpt}\n\n"
            )
    else:
        pr_section += "(none)\n\n"

    return (
        f"## Target issue: #{issue['number']}\n\n"
        f"**Title:** {title}\n"
        f"**Labels:** {labels}\n\n"
        f"**Body:**\n{body}\n\n"
        f"{other_section}"
        f"{pr_section}"
    )


def _fetch_context(issue_number: int) -> tuple[list[dict], list[dict]]:
    """Fetch open auto-improve issues and recent merged PRs for context.

    The target issue itself is excluded from the open-issues list.
    Failures fall back to empty lists — the verdict will then be
    ``NONE`` for lack of candidates.
    """
    try:
        context_issues = _gh_json([
            "issue", "list", "--repo", REPO,
            "--label", "auto-improve",
            "--state", "open",
            "--json", "number,title,labels,body",
            "--limit", "100",
        ]) or []
    except subprocess.CalledProcessError:
        context_issues = []
    context_issues = [ci for ci in context_issues if ci["number"] != issue_number]

    try:
        recent_prs = _gh_json([
            "pr", "list", "--repo", REPO,
            "--state", "merged",
            "--json", "number,title,body,mergedAt",
            "--limit", "30",
        ]) or []
    except subprocess.CalledProcessError:
        recent_prs = []

    return context_issues, recent_prs


def check_duplicate_or_resolved(issue: dict) -> Optional[DupCheckVerdict]:
    """Run the cai-dup-check pre-step on *issue*.

    Returns the parsed verdict, or ``None`` if the agent invocation
    failed or produced unparseable output — callers should treat
    ``None`` identically to a ``NONE`` verdict and fall through to
    the full triage agent.
    """
    other_issues, recent_prs = _fetch_context(issue["number"])
    user_message = build_dup_check_message(issue, other_issues, recent_prs)

    result = _run_claude_p(
        ["claude", "-p", "--agent", "cai-dup-check",
         "--dangerously-skip-permissions"],
        category="dup-check",
        agent="cai-dup-check",
        input=user_message,
    )
    if result.returncode != 0:
        return None

    return parse_dup_check_verdict(result.stdout)


def check_finding_duplicate(
    *,
    title: str,
    body: str,
    labels: Optional[list[str]] = None,
) -> Optional[DupCheckVerdict]:
    """Run the cai-dup-check pre-step on a *staged finding* before publish.

    Counterpart to :func:`check_duplicate_or_resolved` for callers that
    want to guard a finding against semantic duplicates BEFORE the
    corresponding GitHub issue is created. The finding has no issue
    number yet, so a sentinel ``0`` is used for the context-fetch
    exclusion (no real issue carries that number).

    Returns the parsed verdict, or ``None`` on agent failure /
    unparseable output — callers should treat ``None`` as "not a
    duplicate" and proceed with publishing. Only HIGH-confidence
    DUPLICATE / RESOLVED verdicts should short-circuit publish; the
    returned :class:`DupCheckVerdict` exposes ``should_close`` for that
    gate.
    """
    synthetic_issue = {
        "number": 0,
        "title": title,
        "body": body,
        "labels": [{"name": lb} for lb in (labels or [])],
    }
    return check_duplicate_or_resolved(synthetic_issue)
