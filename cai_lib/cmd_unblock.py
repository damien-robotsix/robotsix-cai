"""cai_lib.cmd_unblock — label-gated FSM resume.

Scans issues parked at ``auto-improve:human-needed`` and PRs parked at
``auto-improve:pr-human-needed`` that the admin has marked ready for
resume by applying the ``human:solved`` label. Picking up on the
*label* (rather than any fresh admin comment) means:

- An admin can discuss or ask questions on the issue/PR without the
  automation prematurely deciding the divert is resolved.
- The resume loop skips parked targets entirely until the admin opts in,
  so we don't re-run the classifier every cycle on every open
  parked issue/PR.

For each gated target, invokes the ``cai-unblock`` Haiku agent to
classify the admin's reply into a resume target, fires the matching
``human_to_<state>`` / ``pr_human_to_<state>`` transition, and finally
removes the ``human:solved`` label so the signal is one-shot.
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from typing import Optional

from cai_lib.config import (
    REPO,
    ADMIN_LOGINS,
    LABEL_HUMAN_NEEDED,
    LABEL_PR_HUMAN_NEEDED,
    LABEL_HUMAN_SOLVED,
    is_admin_login,
)
from cai_lib.fsm import (
    Confidence,
    apply_transition,
    apply_pr_transition,
    resume_transition_for,
    resume_pr_transition_for,
)
from cai_lib.github import (
    _gh_json,
    _set_pr_labels,
    close_issue_completed,
    blocking_issue_numbers,
    open_blockers,
)
from cai_lib.logging_utils import log_run
from cai_lib.subprocess_utils import _run_claude_p


# JSON schema for structured unblock verdict (forced tool-use via --json-schema).
# Combined enum covers both the issue-side and PR-side resume targets — the
# cai-unblock agent chooses from the subset appropriate for the ``Kind:`` header
# in the user message.
_UNBLOCK_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "resume_to": {
            "type": "string",
            "enum": [
                # Issue-side (Kind: issue)
                "RAISED", "REFINING", "NEEDS_EXPLORATION",
                "PLAN_APPROVED", "SOLVED",
                # PR-side (Kind: pr)
                "REVIEWING_CODE", "REVIEWING_DOCS",
                "REVISION_PENDING", "APPROVED",
            ],
        },
        "confidence": {
            "type": "string",
            "enum": ["LOW", "MEDIUM", "HIGH"],
        },
        "reasoning": {
            "type": "string",
        },
    },
    "required": ["resume_to", "confidence", "reasoning"],
}


def _list_human_needed_issues() -> list[dict]:
    """Return open issues parked at ``:human-needed`` that the admin has
    marked ready for resume via ``human:solved``.

    Passing ``--label`` twice to ``gh issue list`` ANDs the filters, so
    we only get issues that carry BOTH labels. Everything else stays
    parked and is ignored by this pass.

    Issues whose ``blocked-on:<N>`` blockers are still open are excluded —
    even admin-driven unblock respects the dependency ordering.
    """
    try:
        candidates = _gh_json([
            "issue", "list",
            "--repo", REPO,
            "--label", LABEL_HUMAN_NEEDED,
            "--label", LABEL_HUMAN_SOLVED,
            "--state", "open",
            "--json", "number,title,body,labels,updatedAt,comments",
            "--limit", "100",
        ]) or []
    except subprocess.CalledProcessError as e:
        print(
            f"[cai unblock] gh issue list failed:\n{e.stderr}",
            file=sys.stderr,
        )
        return []

    out: list[dict] = []
    _blocker_cache: dict[int, bool] = {}
    for issue in candidates:
        blockers = blocking_issue_numbers(issue.get("labels", []))
        if blockers:
            open_set = open_blockers(blockers, cache=_blocker_cache)
            if open_set:
                print(
                    f"[cai unblock] #{issue['number']}: blocked on open "
                    f"{sorted(open_set)} — skipping",
                    flush=True,
                )
                continue
        out.append(issue)
    return out


def _extract_admin_comments(issue: dict) -> list[dict]:
    """Return admin-authored comments on *issue*, oldest first."""
    out = []
    for c in issue.get("comments") or []:
        login = (c.get("author") or {}).get("login") or ""
        if is_admin_login(login):
            out.append(c)
    return out


def _build_unblock_message(*, kind: str, issue: dict) -> str:
    """Format the user message for the cai-unblock agent.

    Includes the target's labels, body, and the full comment thread
    (chronological, all authors) — the agent uses the admin's most recent
    comment as the resume signal and the rest as context.
    """
    body = issue.get("body") or "(no body)"
    labels = [
        (lb.get("name") if isinstance(lb, dict) else lb)
        for lb in issue.get("labels", [])
    ]
    labels_line = ", ".join(labels) if labels else "(none)"

    comments = issue.get("comments") or []
    comments_block = ""
    for c in comments:
        author = (c.get("author") or {}).get("login") or "unknown"
        created = c.get("createdAt", "") or c.get("created_at", "")
        marker = " [admin]" if is_admin_login(author) else ""
        text = c.get("body", "") or ""
        comments_block += f"\n**{author}**{marker} ({created}):\n{text}\n"

    return (
        f"Kind: {kind}\n"
        f"\n"
        f"## Labels\n"
        f"{labels_line}\n"
        f"\n"
        f"## Body\n\n"
        f"### #{issue['number']} — {issue.get('title', '')}\n\n"
        f"{body}\n"
        f"\n"
        f"## Comments\n"
        f"{comments_block or '(no comments)'}\n"
    )


def _try_unblock_issue(issue: dict) -> Optional[str]:
    """Attempt to resume *issue* from :human-needed. Returns the result tag.

    Result tags (used for logging):
      - ``"no_admin_comment"`` — ``human:solved`` applied but no admin
        comment yet — the classifier has nothing to anchor on
      - ``"low_confidence"``   — agent's Confidence < HIGH, left parked
      - ``"no_target"``        — agent emitted no valid ResumeTo target
      - ``"resumed"``          — transition fired, solved label cleared;
        if resumed to SOLVED, the issue is also closed in GitHub as
        "completed"
      - ``"agent_failed"``     — claude invocation returned non-zero
    """
    issue_number = issue["number"]

    admin_comments = _extract_admin_comments(issue)
    if not admin_comments:
        # Admin applied human:solved without leaving any comment. The
        # classifier would have nothing to anchor on, so leave the issue
        # parked. The label stays on so we retry once a comment lands.
        return "no_admin_comment"

    user_message = _build_unblock_message(kind="issue", issue=issue)
    result = _run_claude_p(
        ["claude", "-p", "--agent", "cai-unblock",
         "--dangerously-skip-permissions",
         "--json-schema", json.dumps(_UNBLOCK_JSON_SCHEMA)],
        category="unblock",
        agent="cai-unblock",
        input=user_message,
    )
    if result.returncode != 0:
        print(
            f"[cai unblock] #{issue_number} agent failed "
            f"(exit {result.returncode}):\n{result.stderr}",
            file=sys.stderr,
        )
        return "agent_failed"

    try:
        payload = json.loads(result.stdout)
    except (json.JSONDecodeError, ValueError) as exc:
        print(
            f"[cai unblock] #{issue_number} failed to parse JSON: {exc}; "
            f"stdout starts with: {(result.stdout or '')[:120]!r}",
            file=sys.stderr,
            flush=True,
        )
        payload = {}

    target = (payload.get("resume_to") or "").upper() or None
    conf_str = (payload.get("confidence") or "").upper()
    confidence = Confidence[conf_str] if conf_str in Confidence.__members__ else None
    reasoning = payload.get("reasoning", "(no reasoning provided)")
    print(
        f"[cai unblock] #{issue_number} verdict: resume_to={target or 'MISSING'} "
        f"confidence={conf_str or 'MISSING'} reasoning={reasoning}",
        flush=True,
    )

    if confidence != Confidence.HIGH:
        print(
            f"[cai unblock] #{issue_number} confidence="
            f"{confidence.name if confidence else 'MISSING'}; leaving parked",
            flush=True,
        )
        return "low_confidence"

    if not target:
        print(f"[cai unblock] #{issue_number} no ResumeTo target; leaving parked",
              flush=True)
        return "no_target"

    transition = resume_transition_for(target)
    if transition is None:
        print(
            f"[cai unblock] #{issue_number} unknown resume target {target!r}; "
            f"leaving parked",
            flush=True,
        )
        return "no_target"

    current_labels = [l["name"] for l in issue.get("labels", [])]  # noqa: E741
    # The transition already clears :human-needed; also drop the
    # human:solved signal so the label is one-shot.
    ok = apply_transition(
        issue_number, transition.name,
        current_labels=current_labels,
        extra_remove=[LABEL_HUMAN_SOLVED],
        log_prefix="cai unblock",
    )
    if not ok:
        return "agent_failed"

    print(
        f"[cai unblock] #{issue_number} resumed via {transition.name} "
        f"→ {transition.to_state.name}",
        flush=True,
    )

    if transition.name == "human_to_solved":
        close_issue_completed(
            issue_number,
            f"Resumed to SOLVED per admin direction: {reasoning}. "
            f"Closing as completed.",
            log_prefix="cai unblock",
        )

    return "resumed"


def handle_human_needed(issue: dict) -> int:
    """Dispatcher handler for :class:`IssueState.HUMAN_NEEDED` issues.

    Picked up by :func:`cai_lib.dispatcher.dispatch_issue` when the cycle
    selects a parked issue. Delegates to :func:`_try_unblock_issue` only
    when the admin has applied ``human:solved``; otherwise returns 0 so
    the inner driver treats the issue as blocked and moves on. The
    picker in :func:`_pick_oldest_actionable_target` skips parked
    issues lacking ``human:solved`` so this branch is rarely hit, but
    it keeps manual ``cai dispatch --issue N`` safe against a
    race with label removal.
    """
    labels = [l["name"] for l in issue.get("labels", [])]  # noqa: E741
    if LABEL_HUMAN_SOLVED not in labels:
        print(
            f"[cai dispatch] #{issue['number']} parked at :human-needed "
            f"without {LABEL_HUMAN_SOLVED} — leaving parked",
            flush=True,
        )
        return 0
    tag = _try_unblock_issue(issue) or "skipped"
    print(f"[cai dispatch] auto-unblock #{issue['number']}: {tag}", flush=True)
    return 1 if tag == "agent_failed" else 0


def _list_pr_human_needed_prs() -> list[dict]:
    """PR-side counterpart to :func:`_list_human_needed_issues`.

    Returns open PRs carrying BOTH ``auto-improve:pr-human-needed`` and
    ``human:solved``. PRs lacking ``human:solved`` stay parked and are
    ignored by this pass.

    PRs whose ``blocked-on:<N>`` blockers are still open are excluded —
    even admin-driven unblock respects the dependency ordering.
    """
    try:
        candidates = _gh_json([
            "pr", "list",
            "--repo", REPO,
            "--label", LABEL_PR_HUMAN_NEEDED,
            "--label", LABEL_HUMAN_SOLVED,
            "--state", "open",
            "--json", "number,title,body,labels,updatedAt,comments",
            "--limit", "100",
        ]) or []
    except subprocess.CalledProcessError as e:
        print(
            f"[cai unblock] gh pr list failed:\n{e.stderr}",
            file=sys.stderr,
        )
        return []

    out: list[dict] = []
    _blocker_cache: dict[int, bool] = {}
    for pr in candidates:
        blockers = blocking_issue_numbers(pr.get("labels", []))
        if blockers:
            open_set = open_blockers(blockers, cache=_blocker_cache)
            if open_set:
                print(
                    f"[cai unblock] PR #{pr['number']}: blocked on open "
                    f"{sorted(open_set)} — skipping",
                    flush=True,
                )
                continue
        out.append(pr)
    return out


def _try_unblock_pr(pr: dict) -> Optional[str]:
    """Attempt to resume *pr* from :pr-human-needed. Returns the result tag.

    Mirrors :func:`_try_unblock_issue`. Resume target maps to a
    ``pr_human_to_<state>`` transition via
    :func:`resume_pr_transition_for`, applied with
    :func:`apply_pr_transition`.
    """
    pr_number = pr["number"]

    admin_comments = _extract_admin_comments(pr)
    if not admin_comments:
        return "no_admin_comment"

    user_message = _build_unblock_message(kind="pr", issue=pr)
    result = _run_claude_p(
        ["claude", "-p", "--agent", "cai-unblock",
         "--dangerously-skip-permissions",
         "--json-schema", json.dumps(_UNBLOCK_JSON_SCHEMA)],
        category="unblock",
        agent="cai-unblock",
        input=user_message,
    )
    if result.returncode != 0:
        print(
            f"[cai unblock] PR #{pr_number} agent failed "
            f"(exit {result.returncode}):\n{result.stderr}",
            file=sys.stderr,
        )
        return "agent_failed"

    try:
        payload = json.loads(result.stdout)
    except (json.JSONDecodeError, ValueError) as exc:
        print(
            f"[cai unblock] PR #{pr_number} failed to parse JSON: {exc}; "
            f"stdout starts with: {(result.stdout or '')[:120]!r}",
            file=sys.stderr,
            flush=True,
        )
        payload = {}

    target = (payload.get("resume_to") or "").upper() or None
    conf_str = (payload.get("confidence") or "").upper()
    confidence = Confidence[conf_str] if conf_str in Confidence.__members__ else None
    reasoning = payload.get("reasoning", "(no reasoning provided)")
    print(
        f"[cai unblock] PR #{pr_number} verdict: resume_to={target or 'MISSING'} "
        f"confidence={conf_str or 'MISSING'} reasoning={reasoning}",
        flush=True,
    )

    if confidence != Confidence.HIGH:
        print(
            f"[cai unblock] PR #{pr_number} confidence="
            f"{confidence.name if confidence else 'MISSING'}; leaving parked",
            flush=True,
        )
        return "low_confidence"

    if not target:
        print(f"[cai unblock] PR #{pr_number} no ResumeTo target; leaving parked",
              flush=True)
        return "no_target"

    transition = resume_pr_transition_for(target)
    if transition is None:
        print(
            f"[cai unblock] PR #{pr_number} unknown resume target {target!r}; "
            f"leaving parked",
            flush=True,
        )
        return "no_target"

    # The transition already clears :pr-human-needed via labels_remove.
    # The human:solved label needs an explicit removal pass — unlike the
    # issue-side helper, apply_pr_transition has no ``extra_remove`` kw.
    ok = apply_pr_transition(
        pr_number, transition.name,
        log_prefix="cai unblock",
    )
    if not ok:
        return "agent_failed"

    if not _set_pr_labels(
        pr_number, remove=[LABEL_HUMAN_SOLVED], log_prefix="cai unblock",
    ):
        # The state transition landed; failing to clear :solved will make
        # the next pass re-pick this PR and loop on the same decision.
        # Log and surface as a non-fatal agent_failed so the wrapper can
        # retry or a human can intervene.
        print(
            f"[cai unblock] PR #{pr_number} resumed via {transition.name} "
            f"but failed to clear {LABEL_HUMAN_SOLVED}",
            file=sys.stderr,
        )
        return "agent_failed"

    print(
        f"[cai unblock] PR #{pr_number} resumed via {transition.name} "
        f"→ {transition.to_state.name}",
        flush=True,
    )
    return "resumed"


def handle_pr_human_needed(pr: dict) -> int:
    """Dispatcher handler for :class:`PRState.PR_HUMAN_NEEDED` PRs.

    Mirrors :func:`handle_human_needed`. Only fires ``_try_unblock_pr``
    when the admin has applied ``human:solved``; otherwise returns 0 so
    the inner driver treats the PR as blocked. The dispatcher's picker
    already filters PRs lacking ``human:solved``, so this branch is a
    belt-and-braces guard against a race with label removal.
    """
    labels = [
        (lb.get("name") if isinstance(lb, dict) else lb)
        for lb in pr.get("labels", [])
    ]
    if LABEL_HUMAN_SOLVED not in labels:
        print(
            f"[cai dispatch] PR #{pr['number']} parked at :pr-human-needed "
            f"without {LABEL_HUMAN_SOLVED} — leaving parked",
            flush=True,
        )
        return 0
    tag = _try_unblock_pr(pr) or "skipped"
    print(f"[cai dispatch] auto-unblock PR #{pr['number']}: {tag}", flush=True)
    return 1 if tag == "agent_failed" else 0


def cmd_unblock(args) -> int:
    """Scan :human-needed issues and PRs and attempt FSM resume via cai-unblock."""
    if not ADMIN_LOGINS:
        print(
            "[cai unblock] WARNING: CAI_ADMIN_LOGINS is not set — no one is "
            "recognised as an admin, so every human:solved label will be "
            "ignored and parked issues/PRs will never be unblocked. "
            "Set CAI_ADMIN_LOGINS to a comma-separated list of GitHub logins "
            "in your .env or docker-compose.yml environment block.",
            file=sys.stderr,
        )
    t0 = time.monotonic()
    issues = _list_human_needed_issues()
    prs = _list_pr_human_needed_prs()
    if not issues and not prs:
        print("[cai unblock] no :human-needed issues or PRs; nothing to do",
              flush=True)
        log_run("unblock", repo=REPO, result="no_targets", exit=0)
        return 0

    counters: dict[str, int] = {}
    for issue in issues:
        tag = _try_unblock_issue(issue) or "skipped"
        counters[f"issue_{tag}"] = counters.get(f"issue_{tag}", 0) + 1
    for pr in prs:
        tag = _try_unblock_pr(pr) or "skipped"
        counters[f"pr_{tag}"] = counters.get(f"pr_{tag}", 0) + 1

    dur = f"{int(time.monotonic() - t0)}s"
    summary = ", ".join(f"{k}={v}" for k, v in sorted(counters.items()))
    print(f"[cai unblock] done in {dur}: {summary}", flush=True)
    log_run("unblock", repo=REPO, duration=dur, exit=0, **counters)
    return 0
