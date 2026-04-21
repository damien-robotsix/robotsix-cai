"""cai_lib.cmd_rescue — autonomous rescue pass for parked issues and PRs.

Counterpart to :mod:`cai_lib.cmd_unblock`. Where ``cmd_unblock`` only acts
when an admin has explicitly applied the ``human:solved`` label,
``cmd_rescue`` runs autonomously: it scans ``auto-improve:human-needed``
issues and ``auto-improve:pr-human-needed`` PRs that DO NOT yet carry
``human:solved``, asks the :file:`cai-rescue` agent whether each divert
can be resumed without human input, and on a HIGH-confidence
``AUTONOMOUSLY_RESOLVABLE`` verdict fires the matching
``human_to_<state>`` / ``pr_human_to_<state>`` transition. Targets the
agent classifies as ``TRULY_HUMAN_NEEDED`` are left parked.

The pass also collects optional ``prevention_finding`` text from the
agent and publishes the survivors as ``auto-improve:raised`` issues via
:mod:`publish`, so that recurring divert patterns can be fixed at the
source.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import time
from typing import Optional

from cai_lib.cmd_helpers_issues import _extract_stored_plan

from cai_lib.config import (
    REPO,
    LABEL_HUMAN_NEEDED,
    LABEL_HUMAN_SOLVED,
    LABEL_OPUS_ATTEMPTED,
    LABEL_PLAN_NEEDS_REVIEW,
    LABEL_PR_HUMAN_NEEDED,
)
from cai_lib.fsm import (
    Confidence,
    fire_trigger,
    resume_pr_transition_for,
    resume_transition_for,
)
from cai_lib.github import (
    _gh_json,
    _post_issue_comment,
    _post_pr_comment,
    _set_labels,
    close_issue_completed,
    blocking_issue_numbers,
    open_blockers,
)
from cai_lib.logging_utils import log_run
from cai_lib.subprocess_utils import _run_claude_p


# JSON schema for the cai-rescue verdict (forced via --json-schema).
#
# The ``ATTEMPT_OPUS_IMPLEMENT`` verdict is a one-shot escalation path
# for parks where a stored plan exists but the Sonnet-backed
# cai-implement run gave up (spike marker, repeated test failures, no
# diff). The rescue driver applies ``LABEL_OPUS_ATTEMPTED`` and fires
# ``human_to_plan_approved`` so the next dispatcher tick re-runs
# implement on the same plan — this time with ``--model
# claude-opus-4-7`` (see :mod:`cai_lib.actions.implement`). The label
# also gates re-escalation: a second park on the same issue will not
# emit ATTEMPT_OPUS_IMPLEMENT again.
_RESCUE_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {
            "type": "string",
            "enum": [
                "AUTONOMOUSLY_RESOLVABLE",
                "ATTEMPT_OPUS_IMPLEMENT",
                "TRULY_HUMAN_NEEDED",
            ],
        },
        "confidence": {
            "type": "string",
            "enum": ["LOW", "MEDIUM", "HIGH"],
        },
        "resume_to": {
            "type": "string",
            "enum": [
                # Issue-side (Kind: issue-rescue)
                "RAISED", "REFINING", "NEEDS_EXPLORATION",
                "PLAN_APPROVED", "SOLVED",
                # PR-side (Kind: pr-rescue)
                "REVIEWING_CODE", "REVIEWING_DOCS",
                "REVISION_PENDING", "APPROVED",
            ],
        },
        "reasoning": {"type": "string"},
        "prevention_finding": {"type": "string"},
    },
    "required": ["verdict", "confidence", "reasoning"],
}


def _list_unresolved_human_needed_issues() -> list[dict]:
    """Return open ``:human-needed`` issues that lack ``human:solved`` and have no open blockers.

    Mirrors :func:`cmd_unblock._list_human_needed_issues` but inverts
    the second filter — we want the issues an admin has NOT yet acted
    on, since those are the candidates the autonomous rescue pass
    should consider. Issues carrying ``blocked-on:<N>`` labels are
    skipped if issue ``#<N>`` is still open.
    """
    try:
        candidates = _gh_json([
            "issue", "list",
            "--repo", REPO,
            "--label", LABEL_HUMAN_NEEDED,
            "--state", "open",
            "--json", "number,title,body,labels,updatedAt,comments",
            "--limit", "100",
        ]) or []
    except subprocess.CalledProcessError as e:
        print(
            f"[cai rescue] gh issue list failed:\n{e.stderr}",
            file=sys.stderr,
        )
        return []

    out: list[dict] = []
    _blocker_cache: dict[int, bool] = {}
    for issue in candidates:
        names = {
            (lb.get("name") if isinstance(lb, dict) else lb)
            for lb in issue.get("labels", [])
        }
        if LABEL_HUMAN_SOLVED in names:
            # Admin already opted-in — leave it to cmd_unblock.
            continue
        if LABEL_PLAN_NEEDS_REVIEW in names:
            # `handle_plan_gate` flagged this park for mandatory admin
            # sign-off via cai-select's requires_human_review=true
            # signal (#1128). The autonomous rescue pass cannot resolve
            # a divert the planner itself said needs admin input —
            # skip until an admin applies `human:solved` (handled by
            # `cai unblock`) or directly resumes the issue, at which
            # point the `human_to_*` transition also strips this label
            # via its `labels_remove`.
            print(
                f"[cai rescue] #{issue['number']}: carries "
                f"{LABEL_PLAN_NEEDS_REVIEW} — plan explicitly requires "
                f"admin review, skipping autonomous rescue",
                flush=True,
            )
            continue
        blockers = blocking_issue_numbers(issue.get("labels", []))
        if blockers:
            open_set = open_blockers(blockers, cache=_blocker_cache)
            if open_set:
                print(
                    f"[cai rescue] #{issue['number']}: blocked on open "
                    f"{sorted(open_set)} — skipping",
                    flush=True,
                )
                continue
        out.append(issue)
    return out


def _list_unresolved_pr_human_needed_prs() -> list[dict]:
    """Return open ``:pr-human-needed`` PRs that lack ``human:solved`` and have no open blockers.

    PR-side counterpart to :func:`_list_unresolved_human_needed_issues`:
    candidates for autonomous rescue are the ones an admin has NOT yet
    opted-in on via ``human:solved``. PRs carrying ``blocked-on:<N>``
    labels are skipped if issue ``#<N>`` is still open.
    """
    try:
        candidates = _gh_json([
            "pr", "list",
            "--repo", REPO,
            "--label", LABEL_PR_HUMAN_NEEDED,
            "--state", "open",
            "--json", "number,title,body,labels,updatedAt,comments",
            "--limit", "100",
        ]) or []
    except subprocess.CalledProcessError as e:
        print(
            f"[cai rescue] gh pr list failed:\n{e.stderr}",
            file=sys.stderr,
        )
        return []

    out: list[dict] = []
    _blocker_cache: dict[int, bool] = {}
    for pr in candidates:
        names = {
            (lb.get("name") if isinstance(lb, dict) else lb)
            for lb in pr.get("labels", [])
        }
        if LABEL_HUMAN_SOLVED in names:
            continue
        blockers = blocking_issue_numbers(pr.get("labels", []))
        if blockers:
            open_set = open_blockers(blockers, cache=_blocker_cache)
            if open_set:
                print(
                    f"[cai rescue] PR #{pr['number']}: blocked on open "
                    f"{sorted(open_set)} — skipping",
                    flush=True,
                )
                continue
        out.append(pr)
    return out


def _build_rescue_message(target: dict, *, kind: str) -> str:
    """Format the user message for the cai-rescue agent.

    Same shape as :func:`cmd_unblock._build_unblock_message` but with a
    ``Kind: issue-rescue`` or ``Kind: pr-rescue`` header so the agent
    knows it is the autonomous-rescue mode rather than the admin-resume
    mode, and which submachine's resume targets apply.
    """
    body = target.get("body") or "(no body)"
    labels = [
        (lb.get("name") if isinstance(lb, dict) else lb)
        for lb in target.get("labels", [])
    ]
    labels_line = ", ".join(labels) if labels else "(none)"

    comments = target.get("comments") or []
    comments_block = ""
    for c in comments:
        author = (c.get("author") or {}).get("login") or "unknown"
        created = c.get("createdAt", "") or c.get("created_at", "")
        text = c.get("body", "") or ""
        comments_block += f"\n**{author}** ({created}):\n{text}\n"

    return (
        f"Kind: {kind}\n"
        f"\n"
        f"## Labels\n"
        f"{labels_line}\n"
        f"\n"
        f"## Body\n\n"
        f"### #{target['number']} — {target.get('title', '')}\n\n"
        f"{body}\n"
        f"\n"
        f"## Comments\n"
        f"{comments_block or '(no comments)'}\n"
    )


def _post_rescue_comment(
    issue_number: int, *, target: str, reasoning: str,
) -> bool:
    """Post the audit comment explaining a rescue resume.

    Always posted BEFORE firing the FSM transition so the audit trail
    survives even if the transition errors mid-call.
    """
    body = (
        f"**🛟 Autonomous rescue**\n\n"
        f"`cai rescue` resumed this issue from `:human-needed` "
        f"→ `{target}` without admin input.\n\n"
        f"_Reasoning:_ {reasoning}\n"
    )
    return _post_issue_comment(issue_number, body, log_prefix="cai rescue")


def _post_pr_rescue_comment(
    pr_number: int, *, target: str, reasoning: str,
) -> bool:
    """PR-side counterpart of :func:`_post_rescue_comment`."""
    body = (
        f"**🛟 Autonomous rescue**\n\n"
        f"`cai rescue` resumed this PR from `:pr-human-needed` "
        f"→ `{target}` without admin input.\n\n"
        f"_Reasoning:_ {reasoning}\n"
    )
    return _post_pr_comment(pr_number, body, log_prefix="cai rescue")


def _post_opus_escalation_comment(
    issue_number: int, *, reasoning: str,
) -> bool:
    """Post the audit comment for an Opus-escalation rescue.

    Distinct wording from ``_post_rescue_comment`` because this path
    both resumes AND swaps models — operators reading the audit trail
    should see the escalation called out explicitly.
    """
    body = (
        f"**🛟 Autonomous rescue — Opus escalation**\n\n"
        f"`cai rescue` resumed this issue from `:human-needed` "
        f"→ `PLAN_APPROVED` and marked it `{LABEL_OPUS_ATTEMPTED}` so "
        f"the next `cai implement` run uses Opus instead of Sonnet.\n\n"
        f"_Reasoning:_ {reasoning}\n\n"
        f"_This is a one-shot escalation — if the Opus run also parks "
        f"at `:human-needed`, rescue will not re-escalate._\n"
    )
    return _post_issue_comment(issue_number, body, log_prefix="cai rescue")


def _issue_has_opus_attempted(issue: dict) -> bool:
    """Return True if *issue* already carries ``LABEL_OPUS_ATTEMPTED``."""
    for lb in issue.get("labels", []) or []:
        name = lb.get("name") if isinstance(lb, dict) else lb
        if name == LABEL_OPUS_ATTEMPTED:
            return True
    return False


def _schedule_opus_attempt(
    issue: dict, *, reasoning: str,
) -> Optional[str]:
    """Stamp ``LABEL_OPUS_ATTEMPTED`` and fire ``human_to_plan_approved``.

    Returns the result tag for run-log counters:
      - ``"opus_already_attempted"`` — label already present; leaving parked.
      - ``"opus_no_plan"``           — no stored plan to re-run; leaving parked.
      - ``"opus_attempt_scheduled"`` — label + transition applied.
      - ``"agent_failed"``           — label or transition call failed.
    """
    issue_number = issue["number"]

    if _issue_has_opus_attempted(issue):
        print(
            f"[cai rescue] #{issue_number} already carries "
            f"{LABEL_OPUS_ATTEMPTED}; refusing second escalation",
            flush=True,
        )
        return "opus_already_attempted"

    if _extract_stored_plan(issue.get("body") or "") is None:
        print(
            f"[cai rescue] #{issue_number} has no stored plan; "
            f"cannot escalate to Opus-implement",
            file=sys.stderr, flush=True,
        )
        return "opus_no_plan"

    # Audit comment first — surviving the transition error gives an
    # operator something to anchor on if the FSM call later fails.
    _post_opus_escalation_comment(issue_number, reasoning=reasoning)

    if not _set_labels(
        issue_number,
        add=[LABEL_OPUS_ATTEMPTED],
        log_prefix="cai rescue",
    ):
        print(
            f"[cai rescue] #{issue_number} failed to apply "
            f"{LABEL_OPUS_ATTEMPTED}; aborting escalation",
            file=sys.stderr, flush=True,
        )
        return "agent_failed"

    current_labels = [
        (lb.get("name") if isinstance(lb, dict) else lb)
        for lb in issue.get("labels", []) or []
    ]
    ok, _ = fire_trigger(
        issue_number, "human_to_plan_approved",
        current_labels=current_labels,
        log_prefix="cai rescue",
    )
    if not ok:
        return "agent_failed"

    print(
        f"[cai rescue] #{issue_number} Opus escalation scheduled "
        f"(→ PLAN_APPROVED, {LABEL_OPUS_ATTEMPTED})",
        flush=True,
    )
    return "opus_attempt_scheduled"


def _stage_prevention_finding(
    findings: list[dict], *, source_issue: int, prev_text: str,
) -> None:
    """Append a prevention finding to *findings* for end-of-run publish.

    Dedup key is ``sha256(prev_text)[:16]`` — identical wording across
    multiple rescues collapses to one issue at publish time.
    """
    text = (prev_text or "").strip()
    if not text:
        return
    # Title heuristic: first non-empty line, stripped of markdown header
    # markers and capped to a sensible length.
    title_line = ""
    for line in text.splitlines():
        line = line.strip().lstrip("#").strip()
        if line:
            title_line = line
            break
    title = (title_line or "Rescue prevention finding")[:120]
    if not title.lower().startswith("rescue prevention"):
        title = f"Rescue prevention: {title}"
    key = hashlib.sha256(text.encode()).hexdigest()[:16]
    findings.append({
        "title": title,
        "category": "reliability",
        "key": f"rescue-prev-{key}",
        "confidence": "high",
        "evidence": (
            f"Raised by `cai rescue` while resuming issue #{source_issue} "
            f"from `:human-needed`. The same divert pattern is likely to "
            f"recur unless prevented at the source."
        ),
        "remediation": text,
    })


def _publish_prevention_findings(findings: list[dict]) -> None:
    """Flush *findings* to /tmp and invoke publish.py once at end of run.

    Best-effort — publish failures are logged but never fatal to
    ``cmd_rescue``. The findings file is left in /tmp on failure so a
    human can inspect what would have been raised.
    """
    if not findings:
        return
    path = "/tmp/cai-rescue-findings.json"
    try:
        with open(path, "w") as fh:
            json.dump({"findings": findings}, fh)
    except OSError as exc:
        print(
            f"[cai rescue] could not write findings file {path!r}: {exc}",
            file=sys.stderr,
        )
        return

    result = subprocess.run(
        ["python", "/app/publish.py",
         "--namespace", "auto-improve",
         "--findings-file", path],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(
            f"[cai rescue] publish.py exited {result.returncode}; "
            f"prevention findings left in {path}\n{result.stderr}",
            file=sys.stderr,
        )
    else:
        # publish.py prints its own summary to stdout; surface it.
        if result.stdout:
            print(result.stdout, end="" if result.stdout.endswith("\n") else "\n")


def _try_rescue_issue(
    issue: dict, prevention_findings: list[dict],
) -> Optional[str]:
    """Attempt to resume *issue* autonomously. Returns the result tag.

    Result tags (used for run-log counters):
      - ``"truly_human_needed"`` — agent verdict; left parked.
      - ``"low_confidence"``     — verdict was AUTONOMOUSLY_RESOLVABLE
        but confidence was below HIGH; left parked.
      - ``"no_target"``          — agent did not emit a recognized
        ``resume_to``; left parked.
      - ``"resumed"``            — transition fired; if resumed to
        SOLVED, the issue is also closed in GitHub as "completed".
      - ``"agent_failed"``       — claude invocation returned non-zero
        or produced unparsable output.
    """
    issue_number = issue["number"]

    user_message = _build_rescue_message(issue, kind="issue-rescue")
    result = _run_claude_p(
        ["claude", "-p", "--agent", "cai-rescue",
         "--dangerously-skip-permissions",
         "--json-schema", json.dumps(_RESCUE_JSON_SCHEMA)],
        category="rescue",
        agent="cai-rescue",
        input=user_message,
    )
    if result.returncode != 0:
        print(
            f"[cai rescue] #{issue_number} agent failed "
            f"(exit {result.returncode}):\n{result.stderr}",
            file=sys.stderr,
        )
        return "agent_failed"

    try:
        payload = json.loads(result.stdout)
    except (json.JSONDecodeError, ValueError) as exc:
        print(
            f"[cai rescue] #{issue_number} failed to parse JSON: {exc}; "
            f"stdout starts with: {(result.stdout or '')[:120]!r}",
            file=sys.stderr,
            flush=True,
        )
        return "agent_failed"

    verdict = (payload.get("verdict") or "").upper()
    target = (payload.get("resume_to") or "").upper() or None
    conf_str = (payload.get("confidence") or "").upper()
    confidence = Confidence[conf_str] if conf_str in Confidence.__members__ else None
    reasoning = payload.get("reasoning", "(no reasoning provided)")
    prev_text = (payload.get("prevention_finding") or "").strip()
    print(
        f"[cai rescue] #{issue_number} verdict: {verdict or 'MISSING'} "
        f"resume_to={target or 'MISSING'} "
        f"confidence={conf_str or 'MISSING'} reasoning={reasoning}",
        flush=True,
    )

    # Stage the prevention finding regardless of whether we end up
    # resuming — a strong "truly human-needed" verdict can still surface
    # a useful pattern fix.
    if prev_text:
        _stage_prevention_finding(
            prevention_findings,
            source_issue=issue_number,
            prev_text=prev_text,
        )

    if verdict == "ATTEMPT_OPUS_IMPLEMENT":
        if confidence != Confidence.HIGH:
            print(
                f"[cai rescue] #{issue_number} ATTEMPT_OPUS_IMPLEMENT at "
                f"{confidence.name if confidence else 'MISSING'} confidence; "
                f"refusing escalation",
                flush=True,
            )
            return "low_confidence"
        return _schedule_opus_attempt(issue, reasoning=reasoning)

    if verdict != "AUTONOMOUSLY_RESOLVABLE":
        return "truly_human_needed"

    if confidence != Confidence.HIGH:
        print(
            f"[cai rescue] #{issue_number} confidence="
            f"{confidence.name if confidence else 'MISSING'}; leaving parked",
            flush=True,
        )
        return "low_confidence"

    if not target:
        print(
            f"[cai rescue] #{issue_number} no resume_to target; leaving parked",
            flush=True,
        )
        return "no_target"

    transition = resume_transition_for(target)
    if transition is None:
        print(
            f"[cai rescue] #{issue_number} unknown resume target {target!r}; "
            f"leaving parked",
            flush=True,
        )
        return "no_target"

    # Audit comment first — surviving the transition error gives an
    # operator something to anchor on if the FSM call later fails.
    _post_rescue_comment(
        issue_number,
        target=transition.to_state.name,
        reasoning=reasoning,
    )

    current_labels = [l["name"] for l in issue.get("labels", [])]  # noqa: E741
    ok, _ = fire_trigger(
        issue_number, transition.name,
        current_labels=current_labels,
        log_prefix="cai rescue",
    )
    if not ok:
        return "agent_failed"

    print(
        f"[cai rescue] #{issue_number} resumed via {transition.name} "
        f"→ {transition.to_state.name}",
        flush=True,
    )

    if transition.name == "human_to_solved":
        close_issue_completed(
            issue_number,
            f"Resumed to SOLVED by autonomous rescue: {reasoning}. "
            f"Closing as completed.",
            log_prefix="cai rescue",
        )

    return "resumed"


def _try_rescue_pr(
    pr: dict, prevention_findings: list[dict],
) -> Optional[str]:
    """Attempt to resume *pr* autonomously. Returns the result tag.

    PR-side mirror of :func:`_try_rescue_issue`. The
    ``ATTEMPT_OPUS_IMPLEMENT`` verdict is issue-only (it re-runs the
    implement phase on a stored plan) — if the agent emits it on a PR,
    we treat it as ``truly_human_needed`` and park the PR, logging the
    refusal so the behaviour shows up in the run counters.
    """
    pr_number = pr["number"]

    user_message = _build_rescue_message(pr, kind="pr-rescue")
    result = _run_claude_p(
        ["claude", "-p", "--agent", "cai-rescue",
         "--dangerously-skip-permissions",
         "--json-schema", json.dumps(_RESCUE_JSON_SCHEMA)],
        category="rescue",
        agent="cai-rescue",
        input=user_message,
    )
    if result.returncode != 0:
        print(
            f"[cai rescue] PR #{pr_number} agent failed "
            f"(exit {result.returncode}):\n{result.stderr}",
            file=sys.stderr,
        )
        return "agent_failed"

    try:
        payload = json.loads(result.stdout)
    except (json.JSONDecodeError, ValueError) as exc:
        print(
            f"[cai rescue] PR #{pr_number} failed to parse JSON: {exc}; "
            f"stdout starts with: {(result.stdout or '')[:120]!r}",
            file=sys.stderr,
            flush=True,
        )
        return "agent_failed"

    verdict = (payload.get("verdict") or "").upper()
    target = (payload.get("resume_to") or "").upper() or None
    conf_str = (payload.get("confidence") or "").upper()
    confidence = Confidence[conf_str] if conf_str in Confidence.__members__ else None
    reasoning = payload.get("reasoning", "(no reasoning provided)")
    prev_text = (payload.get("prevention_finding") or "").strip()
    print(
        f"[cai rescue] PR #{pr_number} verdict: {verdict or 'MISSING'} "
        f"resume_to={target or 'MISSING'} "
        f"confidence={conf_str or 'MISSING'} reasoning={reasoning}",
        flush=True,
    )

    if prev_text:
        _stage_prevention_finding(
            prevention_findings,
            source_issue=pr_number,
            prev_text=prev_text,
        )

    if verdict == "ATTEMPT_OPUS_IMPLEMENT":
        # Opus escalation reruns cai-implement on a stored plan — an
        # issue-only concept. Leave the PR parked; the agent doc forbids
        # this verdict on PRs, so it should only happen under drift.
        print(
            f"[cai rescue] PR #{pr_number} got ATTEMPT_OPUS_IMPLEMENT "
            f"(issue-only verdict); leaving parked",
            flush=True,
        )
        return "truly_human_needed"

    if verdict != "AUTONOMOUSLY_RESOLVABLE":
        return "truly_human_needed"

    if confidence != Confidence.HIGH:
        print(
            f"[cai rescue] PR #{pr_number} confidence="
            f"{confidence.name if confidence else 'MISSING'}; leaving parked",
            flush=True,
        )
        return "low_confidence"

    if not target:
        print(
            f"[cai rescue] PR #{pr_number} no resume_to target; leaving parked",
            flush=True,
        )
        return "no_target"

    transition = resume_pr_transition_for(target)
    if transition is None:
        print(
            f"[cai rescue] PR #{pr_number} unknown resume target {target!r}; "
            f"leaving parked",
            flush=True,
        )
        return "no_target"

    # Audit comment first — surviving a transition failure still gives
    # an operator something to anchor on.
    _post_pr_rescue_comment(
        pr_number,
        target=transition.to_state.name,
        reasoning=reasoning,
    )

    ok, _ = fire_trigger(
        pr_number, transition.name,
        is_pr=True,
        log_prefix="cai rescue",
    )
    if not ok:
        return "agent_failed"

    print(
        f"[cai rescue] PR #{pr_number} resumed via {transition.name} "
        f"→ {transition.to_state.name}",
        flush=True,
    )
    return "resumed"


def cmd_rescue(args) -> int:
    """Scan parked :human-needed issues and :pr-human-needed PRs and
    attempt autonomous resume.

    Always returns 0 unless a hard infrastructure failure occurs —
    individual per-target failures are recorded in counters but do not
    fail the overall run, since the next cron tick will retry.
    """
    t0 = time.monotonic()
    issues = _list_unresolved_human_needed_issues()
    prs = _list_unresolved_pr_human_needed_prs()
    if not issues and not prs:
        print(
            "[cai rescue] no unresolved :human-needed issues or "
            ":pr-human-needed PRs; nothing to do",
            flush=True,
        )
        log_run("rescue", repo=REPO, result="no_targets", exit=0)
        return 0

    counters: dict[str, int] = {}
    prevention_findings: list[dict] = []
    for issue in issues:
        tag = _try_rescue_issue(issue, prevention_findings) or "skipped"
        counters[f"issue_{tag}"] = counters.get(f"issue_{tag}", 0) + 1
    for pr in prs:
        tag = _try_rescue_pr(pr, prevention_findings) or "skipped"
        counters[f"pr_{tag}"] = counters.get(f"pr_{tag}", 0) + 1

    _publish_prevention_findings(prevention_findings)

    dur = f"{int(time.monotonic() - t0)}s"
    summary = ", ".join(f"{k}={v}" for k, v in sorted(counters.items()))
    print(
        f"[cai rescue] done in {dur}: {summary} "
        f"prevention_findings_staged={len(prevention_findings)}",
        flush=True,
    )
    log_run(
        "rescue", repo=REPO, duration=dur, exit=0,
        prevention_findings=len(prevention_findings),
        **counters,
    )
    return 0
