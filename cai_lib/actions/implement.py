"""cai_lib.actions.implement — handler for the implement phase of the FSM.

Derived from ``cmd_implement`` in ``cai.py``. The dispatcher hands the
handler an issue whose state is either :class:`IssueState.PLAN_APPROVED`
(fresh entry — we apply ``approved_to_in_progress`` first) or
:class:`IssueState.IN_PROGRESS` (resume — we skip the entry transition
because a prior run locked the issue and crashed / was interrupted).

On fresh entry we also check whether a branch
``auto-improve/<issue_number>-*`` already exists on origin; if so we
reuse it (resume semantics) rather than creating a fresh branch.

Direct-invoke (``args.issue``) handling and the "find oldest
:plan-approved issue" pickup are intentionally dropped here — the
dispatcher owns queue selection.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import uuid

from pathlib import Path

from cai_lib.config import (
    REPO,
    LABEL_IN_PROGRESS,
    LABEL_PLAN_APPROVED,
    LABEL_PR_OPEN,
    LABEL_REFINED,
    LABEL_HUMAN_NEEDED,
    LABEL_OPUS_ATTEMPTED,
    LABEL_RAISED,
    LOG_PATH,
)

# Model ID passed to ``claude -p --model`` when the Opus-escalation
# label is present on the issue. Kept in one place so the next Opus
# release only needs to touch this constant (and the matching pin in
# claude-code's declarative agent files).
_OPUS_MODEL_ID = "claude-opus-4-7"
from cai_lib.github import (
    _gh_json,
    _set_labels,
    _build_implement_user_message,
    close_issue_not_planned,
)
from cai_lib.subprocess_utils import _run, _run_claude_p
from cai_lib.logging_utils import log_run
from cai_lib.cmd_helpers import (
    _work_directory_block,
    _git,
    _gh_user_identity,
    _fetch_previous_fix_attempts,
    _setup_agent_edit_staging,
    _apply_agent_edit_staging,
    _build_attempt_history_block,
    _extract_stored_plan,
)
from cai_lib.fsm import (
    apply_transition,
    Confidence,
    IssueState,
    get_issue_state,
    parse_confidence,
)


# ---------------------------------------------------------------------------
# Handler-local helpers (moved from cai.py — only used by the implement phase).
# ---------------------------------------------------------------------------

_SLUG_RE = re.compile(r"[^a-z0-9]+")

# Max consecutive `tests_failed` entries for the same issue before we
# escalate to :human-needed instead of rolling back. Prevents the
# implement loop from monopolising cycles on an unresolvable issue
# (see issues #748 / #695).
_MAX_TESTS_FAILED_RETRIES = 3


def _slugify(text: str, max_len: int = 50) -> str:
    """Branch-friendly slug — lowercase ascii, dashes, no leading/trailing."""
    slug = _SLUG_RE.sub("-", text.lower()).strip("-")
    if len(slug) > max_len:
        slug = slug[:max_len].rstrip("-")
    return slug or "fix"


def _get_plan_for_fix(issue: dict) -> str | None:
    """Retrieve the implementation plan for a fix run.

    A stored plan is now required for every fix run (see the plan gate
    in `_select_fix_target` and `cmd_implement`). This helper is only
    called after that gate, so the plan should always be present; the
    None branch is kept as a defensive WARNING rather than a hard abort.
    """
    plan = _extract_stored_plan(issue.get("body", ""))
    if plan:
        print(f"[cai implement] using stored plan from issue body ({len(plan)} chars)", flush=True)
    else:
        print("[cai implement] WARNING: plan gate bypassed — no stored plan in body", flush=True)
    return plan


def _parse_suggested_issues(agent_output: str) -> list[dict]:
    """Extract suggested issues from the subagent's stdout.

    The subagent can emit blocks like:

        ## Suggested Issue
        ### Title
        <title text>
        ### Body
        <body text>

    Returns a list of dicts with 'title' and 'body' keys.
    """
    issues: list[dict] = []
    parts = re.split(r"^## Suggested Issue\s*$", agent_output, flags=re.MULTILINE)
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


def _create_suggested_issues(
    suggested: list[dict], source_issue_number: int,
) -> int:
    """Create GitHub issues raised by the implement subagent. Returns count created."""
    created = 0
    for s in suggested:
        issue_body = (
            f"{s['body']}\n\n"
            f"---\n"
            f"_Raised by the implement subagent while working on "
            f"#{source_issue_number}._\n"
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
            print(f"[cai implement] created suggested issue: {url}", flush=True)
            created += 1
        else:
            print(
                f"[cai implement] failed to create suggested issue "
                f"'{s['title']}': {result.stderr}",
                file=sys.stderr,
            )
    return created


def _pre_screen_issue_actionability(issue: dict) -> tuple[str, str]:
    """Cheap Haiku pre-screen to classify an issue before running plan-select.

    Returns a tuple of (verdict, reason) where verdict is one of:
      "actionable" — proceed to clone + plan-select pipeline
      "spike"      — issue needs research before code can be written
      "ambiguous"  — issue is too vague to identify any concrete change

    On any error (network failure, JSON parse error, unexpected format),
    falls through to ("actionable", "pre-screen error: <msg>") so the
    pipeline is never blocked.
    """
    try:
        title = issue.get("title", "")
        body = issue.get("body", "") or ""
        labels = ", ".join(lbl["name"] for lbl in issue.get("labels", []))

        prompt_text = (
            "You are a triage classifier for GitHub issues in an automated "
            "code-improvement pipeline.\n\n"
            "Given the issue below, classify it as one of:\n"
            '- "actionable": A concrete code change can be identified. '
            "This is the DEFAULT — use it when in doubt.\n"
            '- "spike": The issue clearly requires research, investigation, '
            "or evaluation before any code can be written. No specific file "
            "or function is identifiable.\n"
            '- "ambiguous": The issue is so vague that no file, function, '
            "or specific change can be identified, AND it does not describe "
            "a research task.\n\n"
            "IMPORTANT: Strongly bias toward \"actionable\". Only use "
            '"spike" or "ambiguous" when you are highly confident. If the '
            "issue mentions any specific file, function, code pattern, or "
            'error, classify as "actionable".\n\n'
            "Respond with ONLY a JSON object (no markdown fencing):\n"
            '{"verdict": "actionable"|"spike"|"ambiguous", '
            '"reason": "<one sentence explanation>"}\n\n'
            "## Issue\n"
            f"**Title:** {title}\n"
            f"**Labels:** {labels}\n\n"
            f"{body}"
        )

        # NOTE: intentional deviation from the --agent convention used by other
        # _run_claude_p call sites.  The pre-screen is a pure inline prompt with
        # no tool access and no agent definition file — using --model directly
        # avoids the overhead of an agent file for a call that never needs one.
        proc = _run_claude_p(
            ["claude", "-p", "--model", "claude-haiku-4-5", prompt_text],
            category="implement.pre-screen",
        )

        raw = (proc.stdout or "").strip()
        # Strip markdown code fences if the model ignored the instruction
        if raw.startswith("```"):
            raw = raw.removeprefix("```json").removeprefix("```")
            raw = raw.removesuffix("```").strip()

        parsed = json.loads(raw)
        verdict = parsed.get("verdict", "actionable")
        reason = parsed.get("reason", "")
        if verdict not in ("actionable", "spike", "ambiguous"):
            return ("actionable", f"unexpected verdict {verdict!r}")
        return (verdict, reason)

    except Exception as e:  # noqa: BLE001
        print(
            f"[cai implement] pre-screen error (falling through to actionable): {e}",
            file=sys.stderr,
            flush=True,
        )
        return ("actionable", f"pre-screen error: {e}")


def _find_existing_branch(issue_number: int) -> str | None:
    """Return an existing origin branch ``auto-improve/<N>-*``, or None.

    Uses the GitHub matching-refs API so we don't need a local clone to
    probe.  On any failure we return None and let the caller create a
    fresh branch — the resume optimisation is best-effort.
    """
    try:
        refs = _gh_json([
            "api",
            f"repos/{REPO}/git/matching-refs/heads/auto-improve/{issue_number}-",
        ])
    except subprocess.CalledProcessError as e:
        print(
            f"[cai implement] matching-refs lookup failed (continuing with fresh branch): {e.stderr}",
            file=sys.stderr,
            flush=True,
        )
        return None
    if not isinstance(refs, list) or not refs:
        return None
    # refs[i]["ref"] looks like "refs/heads/auto-improve/42-foo-bar-abcd1234".
    for entry in refs:
        ref = entry.get("ref", "")
        prefix = "refs/heads/"
        if ref.startswith(prefix):
            return ref[len(prefix):]
    return None


def _extract_test_failures(output: str, max_chars: int = 3000) -> str:
    """Filter ``python -m unittest -v`` output to FAIL/ERROR sections only.

    The full unittest log can be hundreds of lines of ``... ok`` entries
    that crowd out the actual failure signal when posted into a divert
    comment. We surface three concise pieces instead:

    1. A list of failing test identifiers (``FAIL: <dotted>`` /
       ``ERROR: <dotted>``) from the per-test status lines at the top
       of the verbose output.
    2. Each detailed ``FAIL:`` / ``ERROR:`` block emitted after the
       ``===`` separators, including the traceback for that test.
    3. The final summary line (``FAILED (failures=N, errors=M)`` or
       ``OK``) so the divert consumer can see aggregate counts.

    Result is capped at *max_chars* and suffixed with ``... (truncated)``
    when clipped. If the input has no recognisable FAIL/ERROR markers
    (e.g. a crash before unittest ran), the raw output is returned
    truncated so the divert comment is never empty.
    """
    lines = output.splitlines()

    status_re = re.compile(
        r"^(\S+)\s+\(([^)]+)\)\s+\.\.\.\s+(FAIL|ERROR)\b"
    )
    block_start_re = re.compile(r"^(FAIL|ERROR):\s+")

    failing_names: list[str] = []
    for ln in lines:
        m = status_re.match(ln)
        if m:
            failing_names.append(f"{m.group(3)}: {m.group(2)}")

    blocks: list[str] = []
    i = 0
    n = len(lines)
    while i < n:
        if block_start_re.match(lines[i]):
            block_lines = [lines[i]]
            i += 1
            while i < n:
                # Next detailed block starts — stop so we don't swallow it.
                if (
                    lines[i].startswith("=====")
                    and i + 1 < n
                    and block_start_re.match(lines[i + 1])
                ):
                    break
                # End of test session — stop before the "Ran N tests" summary.
                if (
                    lines[i].startswith("-----")
                    and i + 1 < n
                    and lines[i + 1].startswith("Ran ")
                ):
                    break
                block_lines.append(lines[i])
                i += 1
            blocks.append("\n".join(block_lines).rstrip())
        else:
            i += 1

    summary = ""
    for ln in reversed(lines):
        stripped = ln.strip()
        if stripped.startswith("FAILED") or stripped == "OK":
            summary = stripped
            break

    if not failing_names and not blocks:
        truncated = output[:max_chars]
        if len(output) > max_chars:
            truncated += "\n\n... (truncated)"
        return truncated

    parts: list[str] = []
    if failing_names:
        parts.append("Failing tests:")
        parts.extend(f"  - {name}" for name in failing_names)
        parts.append("")
    if blocks:
        parts.extend(blocks)
    if summary:
        if parts and parts[-1] != "":
            parts.append("")
        parts.append(summary)

    result = "\n".join(parts).rstrip()
    if len(result) > max_chars:
        result = result[:max_chars].rstrip() + "\n\n... (truncated)"
    return result


def _count_consecutive_tests_failed(issue_number: int) -> int:
    """Count trailing consecutive ``result=tests_failed`` log entries
    for *issue_number* in LOG_PATH.

    Walks the [implement] entries for this issue in reverse and
    counts how many trailing entries have ``result=tests_failed``.
    Returns 0 on any I/O failure (the guard is best-effort — we
    must never block the implement pipeline on log read errors).
    """
    try:
        if not LOG_PATH.exists():
            return 0
        lines = LOG_PATH.read_text().splitlines()
    except OSError:
        return 0
    issue_tag = f"issue={issue_number}"
    relevant = [
        ln for ln in lines
        if "[implement]" in ln and issue_tag in ln and "result=" in ln
    ]
    count = 0
    for ln in reversed(relevant):
        if "result=tests_failed" in ln:
            count += 1
        else:
            break
    return count


# ---------------------------------------------------------------------------
# Handler.
# ---------------------------------------------------------------------------


def handle_implement(issue: dict) -> int:
    """Run the implement subagent against one eligible issue.

    The dispatcher supplies an issue whose state is either
    :class:`IssueState.PLAN_APPROVED` (fresh entry — we apply
    ``approved_to_in_progress`` first) or :class:`IssueState.IN_PROGRESS`
    (resume — we skip the entry transition). On normal success we apply
    ``in_progress_to_pr``; on pre-screen ``spike`` / subagent "Needs
    Spike" marker we divert to ``:human-needed``.
    """
    issue_number = issue["number"]
    title = issue["title"]
    label_names = [lbl["name"] for lbl in issue.get("labels", [])]
    state = get_issue_state(label_names)
    print(f"[cai implement] picked #{issue_number}: {title}", flush=True)

    # Hard plan gate — every fix run requires a stored plan.
    if _extract_stored_plan(issue.get("body", "")) is None:
        print(
            f"[cai implement] #{issue_number} has no stored plan; "
            f"demoting to {LABEL_REFINED}",
            flush=True,
        )
        _set_labels(
            issue_number,
            add=[LABEL_REFINED],
            remove=[LABEL_PLAN_APPROVED],
            log_prefix="cai implement",
        )
        log_run("implement", repo=REPO, issue=issue_number, result="no_stored_plan", exit=0)
        return 0

    # 1. Entry transition — idempotent.
    if state == IssueState.PLAN_APPROVED:
        if not apply_transition(
            issue_number, "approved_to_in_progress",
            current_labels=label_names,
            log_prefix="cai implement",
        ):
            print(f"[cai implement] could not lock #{issue_number}", file=sys.stderr)
            log_run("implement", repo=REPO, issue=issue_number, result="lock_failed", exit=1)
            return 1
        print(f"[cai implement] locked #{issue_number} (label {LABEL_IN_PROGRESS})", flush=True)
    elif state == IssueState.IN_PROGRESS:
        print(
            f"[cai implement] resuming #{issue_number} already at :in-progress",
            flush=True,
        )
    else:
        print(
            f"[cai implement] unexpected state {state} for #{issue_number}; skipping",
            file=sys.stderr,
            flush=True,
        )
        log_run("implement", repo=REPO, issue=issue_number, result="bad_state", exit=0)
        return 0

    # Make sure git can authenticate over HTTPS via the gh token. This
    # is also done in entrypoint.sh, but redoing it here is cheap and
    # idempotent and lets ad-hoc `docker run` invocations work too.
    _run(["gh", "auth", "setup-git"], capture_output=True)

    # Pre-screen: cheap Haiku call to triage obvious non-actionable issues
    # before the expensive clone + plan-select pipeline.
    ps_verdict, ps_reason = _pre_screen_issue_actionability(issue)
    print(f"[cai implement] pre-screen: verdict={ps_verdict} reason={ps_reason}", flush=True)

    if ps_verdict == "spike":
        _set_labels(
            issue_number,
            add=[LABEL_HUMAN_NEEDED],
            remove=[LABEL_IN_PROGRESS],
        )
        _run(
            ["gh", "issue", "comment", str(issue_number),
             "--repo", REPO,
             "--body",
             f"## Pre-screen: spike-shaped\n\n"
             f"{ps_reason}\n\n---\n"
             f"_Flagged by `cai implement` pre-screen (Haiku) as "
             f"spike-shaped (needs research, not code). No spike agent "
             f"exists — routed to human review. Re-label to "
             f"`{LABEL_PLAN_APPROVED}` to retry._"],
            capture_output=True,
        )
        log_run("implement", repo=REPO, issue=issue_number, result="pre_screen_human", exit=0)
        return 0

    if ps_verdict == "ambiguous":
        # Bounce to :refined (not :plan-approved) so the issue re-flows
        # through refine → plan → confidence-gated approval. Returning to
        # :plan-approved would make _select_fix_target pick it up again
        # immediately and loop on the same pre-screen verdict.
        _set_labels(
            issue_number,
            add=[LABEL_REFINED],
            remove=[LABEL_IN_PROGRESS],
        )
        _run(
            ["gh", "issue", "comment", str(issue_number),
             "--repo", REPO,
             "--body",
             f"## Pre-screen: ambiguous issue\n\n"
             f"{ps_reason}\n\n---\n"
             f"_Flagged by `cai implement` pre-screen (Haiku). The issue "
             f"was returned to `{LABEL_REFINED}` for refinement._"],
            capture_output=True,
        )
        log_run("implement", repo=REPO, issue=issue_number, result="pre_screen_ambiguous", exit=0)
        return 0

    _uid = uuid.uuid4().hex[:8]
    work_dir = Path(f"/tmp/cai-implement-{issue_number}-{_uid}")
    locked = True

    def rollback() -> None:
        nonlocal locked
        if not locked:
            return
        _set_labels(
            issue_number,
            add=[LABEL_PLAN_APPROVED],
            remove=[LABEL_IN_PROGRESS],
        )
        locked = False

    try:
        if work_dir.exists():
            shutil.rmtree(work_dir)

        # 2. Clone.
        clone = _run(
            ["git", "clone", "--depth", "1", f"https://github.com/{REPO}.git", str(work_dir)],
            capture_output=True,
        )
        if clone.returncode != 0:
            print(f"[cai implement] git clone failed:\n{clone.stderr}", file=sys.stderr)
            rollback()
            log_run("implement", repo=REPO, issue=issue_number, result="clone_failed", exit=1)
            return 1

        # 3. Configure git identity from the gh token's owner.
        name, email = _gh_user_identity()
        _git(work_dir, "config", "user.name", name)
        _git(work_dir, "config", "user.email", email)
        print(f"[cai implement] git identity: {name} <{email}>", flush=True)

        # 4. Branch — reuse an existing auto-improve/<N>-* branch on origin
        #    if present (resume), otherwise cut a fresh one off main.
        existing = _find_existing_branch(issue_number)
        if existing:
            print(
                f"[cai implement] reusing existing branch {existing} for #{issue_number}",
                flush=True,
            )
            fetch = _run(
                ["git", "-C", str(work_dir), "fetch", "origin",
                 f"{existing}:{existing}"],
                capture_output=True,
            )
            if fetch.returncode != 0:
                print(
                    f"[cai implement] fetch of existing branch failed:\n{fetch.stderr}",
                    file=sys.stderr,
                )
                rollback()
                log_run("implement", repo=REPO, issue=issue_number,
                        result="fetch_existing_failed", exit=1)
                return 1
            _git(work_dir, "checkout", existing)
            branch = existing
        else:
            branch = f"auto-improve/{issue_number}-{_slugify(title)}"
            _git(work_dir, "checkout", "-b", branch)

        # 4b. Fetch previous fix attempts (closed, unmerged PRs) and
        #     build a history block so the fix agent doesn't repeat
        #     rejected approaches.
        attempts = _fetch_previous_fix_attempts(issue_number)
        attempt_history_block = _build_attempt_history_block(attempts)
        if attempt_history_block:
            print(
                f"[cai implement] injecting {len(attempts)} previous fix attempt(s) for #{issue_number}",
                flush=True,
            )

        # 4c. Retrieve the pre-computed plan from the issue body.
        #     `cai plan` stored the plan; it was auto-approved on HIGH
        #     confidence, or an admin resumed it via cai-unblock. Either
        #     way it now carries `:plan-approved`.
        selected_plan = _get_plan_for_fix(issue)

        # 4d. Pre-create the `.cai-staging/agents/` directory so the
        #     agent has somewhere to write proposed updates to its
        #     own `.claude/agents/*.md` file(s). Claude-code's
        #     headless `-p` mode hardcodes a protection on every
        #     `.claude/agents/*.md` path that no permission flag
        #     bypasses, so we route self-modifications through a
        #     non-protected scratch path and copy them back after
        #     the agent exits successfully. See
        #     `_setup_agent_edit_staging` / `_apply_agent_edit_staging`.
        _setup_agent_edit_staging(work_dir)

        # 5. Run the cai-implement declarative subagent.
        user_message = (
            _work_directory_block(work_dir)
            + "\n"
            + _build_implement_user_message(issue, attempt_history_block)
        )
        if selected_plan:
            user_message = (
                _work_directory_block(work_dir)
                + "\n"
                + "## Selected Implementation Plan\n\n"
                + "The following plan was pre-computed by `cai plan` and "
                + "approved by a human reviewer. "
                + "Follow this plan to implement the fix.\n\n"
                + f"{selected_plan}\n\n"
                + "---\n\n"
                + _build_implement_user_message(issue, attempt_history_block)
            )
        opus_escalation = LABEL_OPUS_ATTEMPTED in label_names
        claude_cmd = ["claude", "-p", "--agent", "cai-implement"]
        if opus_escalation:
            claude_cmd += ["--model", _OPUS_MODEL_ID]
            print(
                f"[cai implement] #{issue_number} carries "
                f"{LABEL_OPUS_ATTEMPTED}; invoking cai-implement with "
                f"--model {_OPUS_MODEL_ID}",
                flush=True,
            )
        claude_cmd += ["--dangerously-skip-permissions",
                       "--add-dir", str(work_dir)]
        print(f"[cai implement] running cai-implement subagent for {work_dir}", flush=True)
        agent = _run_claude_p(
            claude_cmd,
            category="implement",
            agent="cai-implement",
            input=user_message,
            cwd="/app",
        )
        if agent.stdout:
            print(agent.stdout, flush=True)
        if agent.returncode != 0:
            print(
                f"[cai implement] subagent claude -p failed (exit {agent.returncode}):\n"
                f"{agent.stderr}",
                file=sys.stderr,
            )
            rollback()
            log_run("implement", repo=REPO, issue=issue_number,
                    result="subagent_failed", exit=agent.returncode)
            return agent.returncode

        # 5b. Create any suggested issues the subagent raised.
        agent_text = agent.stdout or ""
        suggested = _parse_suggested_issues(agent_text)
        if suggested:
            n = _create_suggested_issues(suggested, issue_number)
            print(f"[cai implement] created {n}/{len(suggested)} suggested issue(s)", flush=True)

        # 5c. Apply any `.claude/agents/**/*.md` updates the agent staged.
        applied = _apply_agent_edit_staging(work_dir)
        if applied:
            print(
                f"[cai implement] applied {applied} staged "
                f".claude/agents/**/*.md update(s)",
                flush=True,
            )

        # 6. Inspect the working tree. Empty diff = deliberate
        #    no-action OR a spike-shaped bail-out.
        status = _git(work_dir, "status", "--porcelain", check=False)
        if not status.stdout.strip():
            agent_text = agent.stdout or ""
            reasoning = agent_text.strip()[:2000]

            # Detect the spike marker. The cai-implement agent emits a
            # `## Needs Spike` block when bailing on a spike-shaped
            # issue. No spike agent exists, so route to :human-needed.
            is_spike = re.search(
                r"^##\s*Needs Spike\b",
                agent_text,
                flags=re.MULTILINE,
            ) is not None

            if is_spike:
                comment_body = (
                    "## Implement subagent: needs human review\n\n"
                    f"{reasoning}\n\n"
                    "---\n"
                    "_Set by `cai implement` after the subagent identified "
                    "this issue as needing research / verification / "
                    "evaluation rather than a code change. No spike agent "
                    "exists — a human must decide how to proceed. Re-label "
                    f"to `{LABEL_PLAN_APPROVED}` to retry as a routine "
                    "fix instead._"
                )
                print(
                    f"[cai implement] subagent produced no changes for "
                    f"#{issue_number}; marking auto-improve:human-needed",
                    flush=True,
                )
                _run(
                    ["gh", "issue", "comment", str(issue_number),
                     "--repo", REPO,
                     "--body", comment_body],
                    capture_output=True,
                )
                terminal_remove = [LABEL_IN_PROGRESS, LABEL_PLAN_APPROVED]
                if not _set_labels(
                    issue_number,
                    add=[LABEL_HUMAN_NEEDED],
                    remove=terminal_remove,
                ):
                    if not _set_labels(
                        issue_number,
                        add=[LABEL_HUMAN_NEEDED],
                        remove=terminal_remove,
                    ):
                        print(
                            f"[cai implement] WARNING: label transition to "
                            f"auto-improve:human-needed failed twice for "
                            f"#{issue_number} — issue may be stuck without "
                            "a lifecycle label",
                            file=sys.stderr, flush=True,
                        )
                        rollback()
                        log_run("implement", repo=REPO, issue=issue_number,
                                result="label_transition_failed", exit=1)
                        return 1
                locked = False
                log_run("implement", repo=REPO, issue=issue_number,
                        result="human_needed", exit=0)
                return 0
            else:
                reasoning_msg = (
                    "## Implement subagent: no action needed\n\n"
                    f"{reasoning}\n\n"
                    "---\n"
                    "_Closed as **not planned** — the implement subagent reviewed "
                    "and decided no code change was needed. Re-open and re-label "
                    f"to `{LABEL_PLAN_APPROVED}` to retry._"
                )
                print(
                    f"[cai implement] subagent produced no changes for "
                    f"#{issue_number}; closing as not planned",
                    flush=True,
                )
                if not close_issue_not_planned(
                    issue_number, reasoning_msg, log_prefix="cai implement"
                ):
                    rollback()
                    log_run("implement", repo=REPO, issue=issue_number,
                            result="close_failed", exit=1)
                    return 1
                # Strip in-progress labels even after close (allowed on closed issues).
                _set_labels(
                    issue_number,
                    remove=[LABEL_IN_PROGRESS, LABEL_PLAN_APPROVED],
                )
                locked = False
                log_run("implement", repo=REPO, issue=issue_number,
                        result="dismissed_resolved", exit=0)
                return 0

        # Count changed files for the log line.
        diff_files = len(status.stdout.strip().splitlines())

        # 7. Commit.
        _git(work_dir, "add", "-A")
        commit_msg = (
            f"auto-improve: {title}\n\n"
            f"Generated by `cai implement` against issue #{issue_number}.\n\n"
            f"Refs {REPO}#{issue_number}"
        )
        _git(work_dir, "commit", "-m", commit_msg)

        # 7b. Run regression tests against the clone's working tree before
        # pushing.
        test_result = _run(
            [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"],
            cwd=str(work_dir),
            capture_output=True,
        )
        if test_result.returncode != 0:
            failure_output = (
                f"{test_result.stdout or ''}\n"
                f"{test_result.stderr or ''}"
            ).strip()
            print(
                f"[cai implement] regression tests failed — not opening PR\n"
                f"{failure_output}",
                file=sys.stderr,
            )
            # Log the failure first so it is visible to the consecutive-failure
            # counter immediately below.
            log_run("implement", repo=REPO, issue=issue_number,
                    result="tests_failed", exit=1)

            consecutive = _count_consecutive_tests_failed(issue_number)
            if consecutive >= _MAX_TESTS_FAILED_RETRIES:
                # Escalate out of the implement loop. Two exit paths:
                #
                # 1. MEDIUM plan (#923) — the stored plan already tripped a
                #    confidence gate (admin approved it after a divert, or
                #    the anchor-mitigated gate let it through at MEDIUM).
                #    Three failures on top of that are strong evidence the
                #    plan itself is wrong, so re-plan autonomously via
                #    in_progress_to_refining rather than parking at
                #    :human-needed. cai-refine will strip the stale plan
                #    block when it runs.
                # 2. HIGH / MISSING plan — fall through to the established
                #    human-needed escalation so an admin can investigate.
                stored_plan_confidence = parse_confidence(
                    issue.get("body", "") or ""
                )
                failure_summary = _extract_test_failures(failure_output)

                if stored_plan_confidence == Confidence.MEDIUM:
                    comment_body = (
                        "## Implement subagent: re-planning after repeated test failures\n\n"
                        f"Regression tests failed {consecutive} consecutive "
                        f"times for this issue. The stored plan was approved "
                        f"at `MEDIUM` confidence (it already tripped a gate), "
                        f"so three strikes are strong evidence the plan "
                        f"itself is wrong. Routing back to `:refining` for "
                        f"autonomous re-planning instead of human review.\n\n"
                        "### Failing tests\n\n"
                        f"```\n{failure_summary}\n```\n\n"
                        "---\n"
                        "_Set by `cai implement` after "
                        f"{_MAX_TESTS_FAILED_RETRIES} consecutive "
                        f"`tests_failed` log entries on a MEDIUM-confidence "
                        "plan. Issue #923._"
                    )
                    print(
                        f"[cai implement] {consecutive} consecutive "
                        f"tests_failed for #{issue_number} on MEDIUM plan; "
                        f"routing to :refining via in_progress_to_refining",
                        flush=True,
                    )
                    _run(
                        ["gh", "issue", "comment", str(issue_number),
                         "--repo", REPO,
                         "--body", comment_body],
                        capture_output=True,
                    )
                    if not apply_transition(
                        issue_number, "in_progress_to_refining",
                        log_prefix="cai implement",
                    ):
                        print(
                            f"[cai implement] WARNING: in_progress_to_refining "
                            f"failed for #{issue_number} — falling back to "
                            f"auto-improve:human-needed",
                            file=sys.stderr, flush=True,
                        )
                        # Fall through to the human-needed path as a safety
                        # net rather than leaving the issue stuck at
                        # :in-progress.
                    else:
                        locked = False
                        log_run("implement", repo=REPO, issue=issue_number,
                                result="tests_failed_auto_refine", exit=0)
                        return 0

                comment_body = (
                    "## Implement subagent: repeated test failures\n\n"
                    f"Regression tests failed {consecutive} consecutive times "
                    f"for this issue. Escalating to human review to avoid "
                    f"monopolising the implement loop.\n\n"
                    "### Failing tests\n\n"
                    f"```\n{failure_summary}\n```\n\n"
                    "---\n"
                    "_Set by `cai implement` after "
                    f"{_MAX_TESTS_FAILED_RETRIES} consecutive `tests_failed` "
                    "log entries. Re-label to "
                    f"`{LABEL_PLAN_APPROVED}` to retry once the underlying "
                    "problem is resolved._"
                )
                print(
                    f"[cai implement] {consecutive} consecutive tests_failed "
                    f"for #{issue_number}; marking auto-improve:human-needed",
                    flush=True,
                )
                _run(
                    ["gh", "issue", "comment", str(issue_number),
                     "--repo", REPO,
                     "--body", comment_body],
                    capture_output=True,
                )
                terminal_remove = [LABEL_IN_PROGRESS]
                if not _set_labels(
                    issue_number,
                    add=[LABEL_HUMAN_NEEDED],
                    remove=terminal_remove,
                ):
                    if not _set_labels(
                        issue_number,
                        add=[LABEL_HUMAN_NEEDED],
                        remove=terminal_remove,
                    ):
                        print(
                            f"[cai implement] WARNING: label transition to "
                            f"auto-improve:human-needed failed twice for "
                            f"#{issue_number} — issue may be stuck without "
                            "a lifecycle label",
                            file=sys.stderr, flush=True,
                        )
                        rollback()
                        log_run("implement", repo=REPO, issue=issue_number,
                                result="label_transition_failed", exit=1)
                        return 1
                locked = False
                log_run("implement", repo=REPO, issue=issue_number,
                        result="tests_failed_escalated", exit=0)
                return 0

            rollback()
            return 1

        # 8. Push.
        push = _run(
            ["git", "-C", str(work_dir), "push",
             "--force", "-u", "origin", branch],
            capture_output=True,
        )
        if push.returncode != 0:
            print(f"[cai implement] git push failed:\n{push.stderr}", file=sys.stderr)
            rollback()
            log_run("implement", repo=REPO, issue=issue_number,
                    result="push_failed", exit=1)
            return 1

        # 9. Open the PR.
        agent_output = (agent.stdout or "").strip()
        pr_summary = ""
        _marker = "## PR Summary"
        if _marker in agent_output:
            pr_summary = agent_output[agent_output.index(_marker):]
            pr_summary = re.split(
                r"^## Suggested Issue\s*$", pr_summary, flags=re.MULTILINE,
            )[0].rstrip()
            for fence in ("```", "~~~"):
                if pr_summary.rstrip().endswith(fence):
                    pr_summary = pr_summary.rstrip()[: -len(fence)].rstrip()
        if not pr_summary.strip():
            pr_summary = (
                f"## PR Summary\n\n"
                f"{agent_output[:4000]}"
            )
        pr_body = (
            f"Refs {REPO}#{issue_number}\n\n"
            f"**Issue:** #{issue_number} — {title}\n\n"
            f"{pr_summary}\n\n"
            f"---\n"
            f"_Auto-generated by `cai implement`. The implement subagent runs autonomously "
            f"with full tool permissions — please review the diff carefully._\n"
        )
        pr = _run(
            [
                "gh", "pr", "create",
                "--repo", REPO,
                "--title", f"auto-improve: {title}",
                "--body", pr_body,
                "--head", branch,
                "--base", "main",
            ],
            capture_output=True,
        )
        if pr.returncode != 0:
            print(f"[cai implement] gh pr create failed:\n{pr.stderr}", file=sys.stderr)
            rollback()
            log_run("implement", repo=REPO, issue=issue_number,
                    result="pr_create_failed", exit=1)
            return 1

        pr_url = pr.stdout.strip()
        print(f"[cai implement] opened PR: {pr_url}", flush=True)

        pr_number = pr_url.rstrip("/").rsplit("/", 1)[-1]

        # 10. Transition label :in-progress -> :pr-open via the FSM.
        if not apply_transition(
            issue_number, "in_progress_to_pr",
            log_prefix="cai implement",
        ):
            print(
                f"[cai implement] label transition to :pr-open failed for #{issue_number}; retrying",
                flush=True,
            )
            if not apply_transition(
                issue_number, "in_progress_to_pr",
                log_prefix="cai implement",
            ):
                print(
                    f"[cai implement] WARNING: label transition to :pr-open failed twice for "
                    f"#{issue_number} — issue may be orphaned from PR {pr_url}",
                    file=sys.stderr, flush=True,
                )
        locked = False
        log_run("implement", repo=REPO, issue=issue_number, branch=branch,
                pr=pr_number, diff_files=diff_files, exit=0)
        return 0

    except Exception as e:
        print(f"[cai implement] unexpected failure: {e!r}", file=sys.stderr)
        rollback()
        log_run("implement", repo=REPO, issue=issue_number,
                result="unexpected_error", exit=1)
        return 1
    finally:
        if work_dir.exists():
            shutil.rmtree(work_dir, ignore_errors=True)
