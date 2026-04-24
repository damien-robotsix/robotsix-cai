"""MERGED -> SOLVED handler.

Verifies that a merged PR actually remediated its linked issue. On a
solved verdict we apply the ``merged_to_solved`` transition and close
the GitHub issue as "completed"; on an unsolved verdict we re-queue up
to three times before diverting to human review; inconclusive verdicts
post reasoning without changing labels.

Derived from ``cmd_confirm`` in ``cai.py`` — the per-issue path is
preserved byte-for-byte; only the outer "for each :merged issue" loop
has been stripped, since the dispatcher hands this handler exactly one
issue.
"""

import json
import re
import subprocess
import sys
import time

from cai_lib.config import (
    LABEL_MERGED,
    LABEL_PR_NEEDS_HUMAN,
    LABEL_REFINED,
    LABEL_SOLVED,
    PARSE_SCRIPT,
    REPO,
)
from cai_lib import transcript_sync
from cai_lib.cmd_helpers import _fetch_previous_fix_attempts
from cai_lib.fsm import fire_trigger
from cai_lib.github import _gh_json, _set_labels, close_issue_completed
from cai_lib.utils.log import (
    _get_issue_category,
    _log_outcome,
    log_run,
)
from claude_agent_sdk import ClaudeAgentOptions

from cai_lib.cai_subagent import run_subagent
from cai_lib.subprocess_utils import _run


def _parse_verdicts(text: str) -> list[tuple[int, str, str]]:
    """Parse `### Verdict: #N` blocks. Returns (issue_num, status, reasoning)."""
    verdicts = []
    blocks = re.split(r"^### Verdict:\s*", text, flags=re.MULTILINE)
    for block in blocks[1:]:
        lines = block.strip().splitlines()
        if not lines:
            continue
        # Header line: "#N — title"
        header_match = re.match(r"#(\d+)", lines[0])
        if not header_match:
            continue
        issue_num = int(header_match.group(1))
        body = "\n".join(lines[1:])
        status_match = re.search(
            r"^- \*\*Status:\*\*\s*(.+)$", body, flags=re.MULTILINE
        )
        reasoning_match = re.search(
            r"^- \*\*Reasoning:\*\*\s*(.+)$", body, flags=re.MULTILINE
        )
        status = status_match.group(1).strip().strip("`").lower() if status_match else ""
        reasoning = reasoning_match.group(1).strip() if reasoning_match else ""
        if status in ("solved", "unsolved", "inconclusive"):
            verdicts.append((issue_num, status, reasoning))
    return verdicts


def handle_confirm(issue: dict) -> int:
    """Re-analyze the recent window to verify one :merged issue is solved.

    For an unsolved verdict, logs the outcome and either re-queues to
    :refined (up to 3 attempts) or escalates to :needs-human-review
    after max attempts.
    """
    print("[cai confirm] checking merged issues against recent signals", flush=True)
    t0 = time.monotonic()

    merged_issues = [issue]

    print(f"[cai confirm] found {len(merged_issues)} merged issue(s)", flush=True)

    # 2. Run parse.py against the transcript dir (global window settings).
    # When cross-host sync is enabled, refresh the aggregate mirror first
    # so the confirm check considers signals from every machine, not just
    # this one. No-op when sync is disabled.
    transcript_sync.pull()
    parse_dir = transcript_sync.parse_source()
    parsed = _run(
        ["python", str(PARSE_SCRIPT), str(parse_dir)],
        capture_output=True,
    )
    if parsed.returncode != 0:
        print(
            f"[cai confirm] parse.py failed (exit {parsed.returncode}):\n{parsed.stderr}",
            flush=True,
        )
        dur = f"{int(time.monotonic() - t0)}s"
        log_run("confirm", repo=REPO, duration=dur, exit=parsed.returncode)
        return parsed.returncode

    parsed_signals = parsed.stdout.strip()

    # Extract stats from parse.py's JSON output.
    try:
        signals = json.loads(parsed_signals)
    except (json.JSONDecodeError, ValueError):
        signals = {}
    token_usage = signals.get("token_usage", {})
    in_tokens = token_usage.get("input_tokens", 0)
    out_tokens = token_usage.get("output_tokens", 0)
    session_count = signals.get("session_count", 0)

    if in_tokens > 0 and in_tokens < 500:
        print(
            f"[cai confirm] WARNING: in_tokens={in_tokens} is below the "
            f"expected floor of 500 — the transcript window may be too "
            f"narrow or session files may be nearly empty",
            flush=True,
        )

    # 2b. For each merged issue, fetch the associated merged PR diff.
    MAX_DIFF_LEN = 8000
    for mi in merged_issues:
        num = mi["number"]
        try:
            prs = _gh_json([
                "pr", "list", "--repo", REPO,
                "--search", f'"Refs {REPO}#{num}" in:body',
                "--state", "merged",
                "--json", "number",
                "--limit", "1",
            ]) or []
        except subprocess.CalledProcessError:
            prs = []
        if prs:
            pr_num = prs[0]["number"]
            diff_result = _run(
                ["gh", "pr", "diff", str(pr_num), "--repo", REPO],
                capture_output=True,
            )
            if diff_result.returncode == 0 and diff_result.stdout.strip():
                diff_text = diff_result.stdout.strip()
                if len(diff_text) > MAX_DIFF_LEN:
                    diff_text = diff_text[:MAX_DIFF_LEN] + "\n... (truncated)"
                mi["_pr_diff"] = diff_text
                mi["_pr_number"] = pr_num

    # 3. Build the user message (parsed signals + merged issues +
    #    PR diffs). The system prompt, tool allowlist, and model
    #    choice all live in `.claude/agents/lifecycle/cai-confirm.md` — the
    #    wrapper only passes dynamic per-run context via stdin.
    issues_section = "## Merged issues to verify\n\n"
    for mi in merged_issues:
        issues_section += (
            f"### #{mi['number']} — {mi['title']}\n\n"
            f"{mi.get('body') or '(no body)'}\n\n"
        )
        if mi.get("_pr_diff"):
            issues_section += (
                f"#### Merged PR diff (PR #{mi['_pr_number']})\n\n"
                f"```diff\n{mi['_pr_diff']}\n```\n\n"
            )

    user_message = (
        "## Parsed signals\n\n"
        "```json\n"
        f"{parsed_signals}\n"
        "```\n\n"
        f"{issues_section}"
    )

    # 4. Invoke the declared cai-confirm subagent.
    #    Issue #1226 spike: this handler talks to the SDK directly via
    #    ``run_subagent`` instead of the ``_run_claude_p`` argv facade.
    confirm_options = ClaudeAgentOptions(
        extra_args={"agent": "cai-confirm"},
    )
    confirm = run_subagent(
        user_message,
        confirm_options,
        category="confirm",
        agent="cai-confirm",
        target_kind="issue",
        target_number=issue["number"],
    )
    if not confirm.ok:
        print(
            f"[cai confirm] claude -p failed (exit 1):\n"
            f"{confirm.error_summary or ''}",
            flush=True,
        )
        dur = f"{int(time.monotonic() - t0)}s"
        log_run("confirm", repo=REPO, merged_checked=len(merged_issues),
                solved=0, unsolved=0, inconclusive=0,
                sessions=session_count, in_tokens=in_tokens, out_tokens=out_tokens,
                duration=dur, exit=1)
        return 1

    # 5. Parse verdicts.
    verdicts = _parse_verdicts(confirm.stdout)
    merged_nums = {mi["number"] for mi in merged_issues}
    merged_by_num = {mi["number"]: mi for mi in merged_issues}

    solved = 0
    unsolved = 0
    inconclusive = 0

    for issue_num, status, reasoning in verdicts:
        if issue_num not in merged_nums:
            continue
        mi = merged_by_num[issue_num]
        if status == "solved":
            cat = _get_issue_category(mi)
            prior_attempts = len(_fetch_previous_fix_attempts(issue_num))
            _log_outcome(issue_num, cat, "solved", prior_attempts)
            current_labels = [lbl["name"] for lbl in mi.get("labels", [])]
            fire_trigger(
                issue_num, "merged_to_solved",
                current_labels=current_labels,
                log_prefix="cai confirm",
            )
            close_issue_completed(
                issue_num,
                f"Confirmed solved: {reasoning}",
                log_prefix="cai confirm",
            )
            # Fire-and-forget: let cai-memorize decide whether the solved
            # issue yielded a cross-agent design decision worth recording.
            # Failures must never block the confirm flow.
            try:
                memorize_msg = (
                    f"## Issue\n\n"
                    f"### #{issue_num} — {mi['title']}\n\n"
                    f"{mi.get('body') or '(no body)'}\n\n"
                )
                if mi.get("_pr_diff"):
                    memorize_msg += (
                        f"## Merged PR diff (PR #{mi['_pr_number']})\n\n"
                        f"```diff\n{mi['_pr_diff']}\n```\n"
                    )
                memorize_options = ClaudeAgentOptions(
                    extra_args={"agent": "cai-memorize"},
                )
                mem = run_subagent(
                    memorize_msg,
                    memorize_options,
                    category="confirm",
                    agent="cai-memorize",
                    target_kind="issue",
                    target_number=issue_num,
                )
                if not mem.ok:
                    print(
                        f"[cai confirm] cai-memorize failed (exit 1) — "
                        f"continuing",
                        flush=True,
                    )
                else:
                    first_line = (mem.stdout or "").strip().splitlines()[:1]
                    marker = first_line[0] if first_line else ""
                    print(f"[cai confirm] cai-memorize → {marker or '(empty)'}",
                          flush=True)
            except Exception as e:  # noqa: BLE001 - must not break confirm
                print(f"[cai confirm] cai-memorize invocation error: {e}",
                      flush=True)
            print(f"[cai confirm] #{issue_num}: solved — closed", flush=True)
            solved += 1
        elif status == "unsolved":
            cat = _get_issue_category(mi)
            prior_attempts = len(_fetch_previous_fix_attempts(issue_num))
            _log_outcome(issue_num, cat, "unsolved", prior_attempts)

            # Parse re-queue count from issue body (use findall to handle
            # multiple appended blocks; take the last match's count).
            issue_body = mi.get("body") or ""
            requeue_matches = re.findall(
                r"## Confirm re-queue \(attempt (\d+)\)", issue_body
            )
            requeue_count = int(requeue_matches[-1]) if requeue_matches else 0

            if requeue_count < 3:
                new_count = requeue_count + 1
                requeue_block = (
                    f"\n\n## Confirm re-queue (attempt {new_count})\n\n"
                    f"Fix confirmed unsolved. Re-queued for another attempt."
                )
                _run(
                    ["gh", "issue", "edit", str(issue_num),
                     "--repo", REPO,
                     "--body", issue_body + requeue_block],
                    capture_output=True,
                )
                _set_labels(
                    issue_num,
                    add=[LABEL_REFINED],
                    remove=[LABEL_MERGED],
                    log_prefix="cai confirm",
                )
                _run(
                    ["gh", "issue", "comment", str(issue_num),
                     "--repo", REPO,
                     "--body",
                     f"Confirm check: fix did not resolve the issue. "
                     f"Re-queued as `auto-improve:refined` (attempt {new_count}/3)."],
                    capture_output=True,
                )
                print(f"[cai confirm] #{issue_num}: unsolved — re-queued (attempt {new_count}/3)", flush=True)
            else:
                # Cap reached — escalate to human.
                _set_labels(
                    issue_num,
                    add=[LABEL_PR_NEEDS_HUMAN],
                    remove=[LABEL_MERGED],
                    log_prefix="cai confirm",
                )
                _run(
                    ["gh", "issue", "comment", str(issue_num),
                     "--repo", REPO,
                     "--body",
                     "Confirm check: fix did not resolve the issue after 3 re-queue attempts. "
                     "Escalating to `needs-human-review`."],
                    capture_output=True,
                )
                print(f"[cai confirm] #{issue_num}: unsolved — max re-queues reached, needs-human", flush=True)
            unsolved += 1
        elif status == "inconclusive":
            cat = _get_issue_category(mi)
            prior_attempts = len(_fetch_previous_fix_attempts(issue_num))
            _log_outcome(issue_num, cat, "inconclusive", prior_attempts)
            # Post reasoning to the issue so humans can see why, but
            # avoid duplicate comments if the same reasoning was already
            # posted in the most recent comment.
            body = f"Confirm check: inconclusive — {reasoning}"
            try:
                comments = _gh_json([
                    "issue", "view", str(issue_num),
                    "--repo", REPO,
                    "--json", "comments",
                ]) or {}
                last_body = ""
                clist = comments.get("comments") or []
                if clist:
                    last_body = (clist[-1].get("body") or "").strip()
            except subprocess.CalledProcessError:
                last_body = ""
            if last_body != body.strip():
                _run(
                    ["gh", "issue", "comment", str(issue_num),
                     "--repo", REPO,
                     "--body", body],
                    capture_output=True,
                )
            print(f"[cai confirm] #{issue_num}: inconclusive — {reasoning}", flush=True)
            inconclusive += 1

    dur = f"{int(time.monotonic() - t0)}s"
    print(
        f"[cai confirm] merged_checked={len(merged_issues)} "
        f"solved={solved} unsolved={unsolved} inconclusive={inconclusive} "
        f"sessions={session_count} in_tokens={in_tokens} out_tokens={out_tokens}",
        flush=True,
    )
    log_run("confirm", repo=REPO, merged_checked=len(merged_issues),
            solved=solved, unsolved=unsolved, inconclusive=inconclusive,
            sessions=session_count, in_tokens=in_tokens, out_tokens=out_tokens,
            duration=dur, exit=0)
    return 0
