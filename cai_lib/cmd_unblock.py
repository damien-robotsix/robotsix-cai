"""cai_lib.cmd_unblock — label-gated FSM resume.

Scans issues parked at ``auto-improve:human-needed`` that the admin
has marked ready for resume by applying the ``human:solved`` label.
Picking up on the *label* (rather than any fresh admin comment) means:

- An admin can discuss or ask questions on the issue without the
  automation prematurely deciding the divert is resolved.
- The resume loop skips parked issues entirely until the admin opts in,
  so we don't re-run the classifier every cycle on every open
  :human-needed issue.

For each gated issue carrying a pending-transition marker, invokes the
``cai-unblock`` Haiku agent to classify the admin's reply into a resume
target, fires the matching ``human_to_<state>`` transition via
:func:`cai_lib.fsm.apply_transition`, and finally removes the
``human:solved`` label so the signal is one-shot.

PR-side unblock (``auto-improve:pr-human-needed``) is handled in a
follow-up — the PR submachine needs a PR-labelling counterpart to
``_set_labels`` before it can be wired symmetrically.
"""
from __future__ import annotations

import subprocess
import sys
import time
from typing import Optional

from cai_lib.config import (
    REPO,
    LABEL_HUMAN_NEEDED,
    LABEL_HUMAN_SOLVED,
    is_admin_login,
)
from cai_lib.fsm import (
    Confidence,
    apply_transition,
    parse_confidence,
    parse_pending_marker,
    parse_resume_target,
    resume_transition_for,
    strip_pending_marker,
)
from cai_lib.github import _gh_json, _set_labels
from cai_lib.logging_utils import log_run
from cai_lib.subprocess_utils import _run, _run_claude_p


def _list_human_needed_issues() -> list[dict]:
    """Return open issues parked at ``:human-needed`` that the admin has
    marked ready for resume via ``human:solved``.

    Passing ``--label`` twice to ``gh issue list`` ANDs the filters, so
    we only get issues that carry BOTH labels. Everything else stays
    parked and is ignored by this pass.
    """
    try:
        return _gh_json([
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


def _extract_admin_comments(issue: dict) -> list[dict]:
    """Return admin-authored comments on *issue*, oldest first."""
    out = []
    for c in issue.get("comments") or []:
        login = (c.get("author") or {}).get("login") or ""
        if is_admin_login(login):
            out.append(c)
    return out


def _build_unblock_message(
    *,
    kind: str,
    issue: dict,
    marker: dict,
    admin_comments: list[dict],
) -> str:
    """Format the user message for the cai-unblock agent."""
    marker_line = (
        f"transition={marker.get('transition', '?')} "
        f"from={marker.get('from', '?')} "
        f"intended={marker.get('intended', '?')} "
        f"conf={marker.get('conf', '?')}"
    )
    body = issue.get("body") or "(no body)"
    comments_block = ""
    for c in admin_comments:
        author = (c.get("author") or {}).get("login") or "unknown"
        created = c.get("createdAt", "") or c.get("created_at", "")
        text = c.get("body", "") or ""
        comments_block += f"\n**{author}** ({created}):\n{text}\n"
    return (
        f"Kind: {kind}\n"
        f"\n"
        f"## Pending transition marker\n"
        f"{marker_line}\n"
        f"\n"
        f"## Body\n\n"
        f"### #{issue['number']} — {issue.get('title', '')}\n\n"
        f"{body}\n"
        f"\n"
        f"## Admin comments\n"
        f"{comments_block or '(no admin comments)'}\n"
    )


def _clear_pending_marker_on_body(issue_number: int, current_body: str) -> bool:
    """Strip the pending marker from *current_body* and push via gh."""
    stripped = strip_pending_marker(current_body)
    if stripped == current_body:
        return True  # nothing to do
    update = _run(
        ["gh", "issue", "edit", str(issue_number),
         "--repo", REPO, "--body", stripped],
        capture_output=True,
    )
    if update.returncode != 0:
        print(
            f"[cai unblock] failed to strip marker on #{issue_number}:\n"
            f"{update.stderr}",
            file=sys.stderr,
        )
        return False
    return True


def _try_unblock_issue(issue: dict) -> Optional[str]:
    """Attempt to resume *issue* from :human-needed. Returns the result tag.

    Result tags (used for logging):
      - ``"no_marker"``        — no pending marker in body, left parked
      - ``"no_admin_comment"`` — ``human:solved`` applied but no admin
        comment yet — the classifier has nothing to read
      - ``"low_confidence"``   — agent's Confidence < HIGH, left parked
      - ``"no_target"``        — agent emitted no valid ResumeTo target
      - ``"resumed"``          — transition fired, marker + solved label cleared
      - ``"agent_failed"``     — claude invocation returned non-zero
    """
    issue_number = issue["number"]
    body = issue.get("body") or ""
    marker = parse_pending_marker(body)
    if not marker:
        return "no_marker"

    admin_comments = _extract_admin_comments(issue)
    if not admin_comments:
        # Admin applied human:solved without leaving any comment. The
        # classifier would have nothing to read, so leave the issue parked
        # rather than guess. The label stays on so we retry once a comment
        # lands.
        return "no_admin_comment"

    user_message = _build_unblock_message(
        kind="issue", issue=issue, marker=marker, admin_comments=admin_comments,
    )
    result = _run_claude_p(
        ["claude", "-p", "--agent", "cai-unblock",
         "--dangerously-skip-permissions"],
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

    stdout = result.stdout
    print(stdout, flush=True)

    target = parse_resume_target(stdout)
    confidence = parse_confidence(stdout)
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

    _clear_pending_marker_on_body(issue_number, body)
    print(
        f"[cai unblock] #{issue_number} resumed via {transition.name} "
        f"→ {transition.to_state.name}",
        flush=True,
    )
    return "resumed"


def cmd_unblock(args) -> int:
    """Scan :human-needed issues and attempt FSM resume via cai-unblock."""
    t0 = time.monotonic()
    issues = _list_human_needed_issues()
    if not issues:
        print("[cai unblock] no :human-needed issues; nothing to do",
              flush=True)
        log_run("unblock", repo=REPO, result="no_targets", exit=0)
        return 0

    counters: dict[str, int] = {}
    for issue in issues:
        tag = _try_unblock_issue(issue) or "skipped"
        counters[tag] = counters.get(tag, 0) + 1

    dur = f"{int(time.monotonic() - t0)}s"
    summary = ", ".join(f"{k}={v}" for k, v in sorted(counters.items()))
    print(f"[cai unblock] done in {dur}: {summary}", flush=True)
    log_run("unblock", repo=REPO, duration=dur, exit=0, **counters)
    return 0
