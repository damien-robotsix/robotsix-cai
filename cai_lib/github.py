"""cai_lib.github — GitHub/gh CLI helpers and shared label utilities."""

import json
import os
import re
import subprocess
import sys
import time

from datetime import datetime, timezone

from cai_lib.config import (
    REPO, TRANSCRIPT_DIR,
    LABEL_IN_PROGRESS, LABEL_PR_OPEN, LABEL_MERGE_BLOCKED,
    LABEL_REVISING, LABEL_RAISED, LABEL_REFINED,
    LABEL_LOCKED, INSTANCE_ID, CAI_LOCK_COMMENT_RE,
    BLOCKED_ON_LABEL_RE,
)
from cai_lib.logging_utils import log_run
from cai_lib.subprocess_utils import _run


def _gh_json(args: list[str]):
    """Run a gh command that prints JSON; return the parsed result.

    Raises on non-zero exit (gh failures should be loud).
    """
    result = subprocess.run(
        ["gh"] + args,
        text=True,
        check=True,
        capture_output=True,
    )
    return json.loads(result.stdout) if result.stdout.strip() else None


def check_gh_auth() -> int:
    """Fail fast if `gh` is not authenticated."""
    result = _run(["gh", "auth", "status"], capture_output=True)
    if result.returncode != 0:
        print("[cai] ERROR: gh is not authenticated in this container.", file=sys.stderr)
        print("       Credentials are expected in the cai_home volume.", file=sys.stderr)
        print("       Run the installer's login step, or do it manually:", file=sys.stderr)
        print("         docker compose run --rm cai gh auth login", file=sys.stderr)
        print(file=sys.stderr)
        print(result.stderr.strip() or result.stdout.strip(), file=sys.stderr)
        return 1
    return 0


def check_claude_auth() -> int:
    """Fail fast if `claude` is not authenticated.

    Two valid auth modes for the headless container:
      1. OAuth: credentials sit in the `cai_home` named volume
         (under `/home/cai/.claude/.credentials.json` plus the
         `/home/cai/.claude.json` runtime config sibling file).
         Verified by `claude auth status`.
      2. API key: `ANTHROPIC_API_KEY` is set in the env. claude-code
         uses it directly without needing the OAuth credentials file.

    If neither mode is configured, claude-code will 401 on the first
    API call with a confusing error. Catch the misconfiguration up
    front so the user gets a clear next-step instruction.
    """
    # API-key mode is checked first because it's a single env-var test
    # and doesn't require shelling out.
    if os.environ.get("ANTHROPIC_API_KEY"):
        return 0

    result = _run(["claude", "auth", "status", "--text"], capture_output=True)
    if result.returncode != 0:
        print("[cai] ERROR: claude is not authenticated in this container.", file=sys.stderr)
        print("       Credentials are expected in the cai_home volume,", file=sys.stderr)
        print("       OR set ANTHROPIC_API_KEY in your .env file.", file=sys.stderr)
        print("       Authenticate by opening the claude REPL — it auto-prompts", file=sys.stderr)
        print("       for OAuth login on first start:", file=sys.stderr)
        print("         docker compose run --rm -it cai claude", file=sys.stderr)
        print("       Then exit the REPL gracefully (/exit or Ctrl-D).", file=sys.stderr)
        print(file=sys.stderr)
        print(result.stderr.strip() or result.stdout.strip(), file=sys.stderr)
        return 1
    return 0


def _transcript_dir_is_empty() -> bool:
    if not TRANSCRIPT_DIR.exists():
        return True
    return not any(TRANSCRIPT_DIR.rglob("*.jsonl"))


def _set_labels(issue_number: int, *, add: list[str] = (), remove: list[str] = (), log_prefix: str = "cai implement") -> bool:
    """Add and/or remove labels on an issue. Returns True on success."""
    # Auto-add the base label for any state-prefixed label being added.
    # This is defensive: create_issue already applies base labels, but
    # auto-adding here self-heals issues that lost theirs.
    _BASE_NAMESPACES = {"auto-improve", "audit", "check-workflows"}
    auto_added_bases: set[str] = set()
    for label in add:
        if ":" in label:
            base = label.split(":", 1)[0]
            if base in _BASE_NAMESPACES and base not in add:
                auto_added_bases.add(base)
    effective_add = list(add) + sorted(auto_added_bases)

    args = ["issue", "edit", str(issue_number), "--repo", REPO]
    for label in effective_add:
        args.extend(["--add-label", label])
    for label in remove:
        args.extend(["--remove-label", label])
    result = _run(["gh"] + args, capture_output=True)
    if result.returncode != 0:
        print(
            f"[{log_prefix}] failed to update labels on #{issue_number}:\n{result.stderr}",
            file=sys.stderr,
        )
        return False
    return True


def _set_pr_labels(pr_number: int, *, add: list[str] = (), remove: list[str] = (), log_prefix: str = "cai") -> bool:
    """Add and/or remove labels on a PR. Returns True on success."""
    args = ["pr", "edit", str(pr_number), "--repo", REPO]
    for label in add:
        args.extend(["--add-label", label])
    for label in remove:
        args.extend(["--remove-label", label])
    result = _run(["gh"] + args, capture_output=True)
    if result.returncode != 0:
        print(
            f"[{log_prefix}] failed to update labels on PR #{pr_number}:\n{result.stderr}",
            file=sys.stderr,
        )
        return False
    return True


def _post_issue_comment(issue_number: int, body: str, *, log_prefix: str = "cai") -> bool:
    """Post a comment on an issue. Returns True on success.

    Kept permissive — a failure here is logged but does not abort the
    caller's state transition. The comment is informational context for
    the admin.
    """
    result = _run(
        ["gh", "issue", "comment", str(issue_number),
         "--repo", REPO, "--body", body],
        capture_output=True,
    )
    if result.returncode != 0:
        print(
            f"[{log_prefix}] failed to post comment on #{issue_number}:\n{result.stderr}",
            file=sys.stderr,
        )
        return False
    return True


def _post_pr_comment(pr_number: int, body: str, *, log_prefix: str = "cai") -> bool:
    """Post a comment on a PR. Returns True on success. See :func:`_post_issue_comment`."""
    result = _run(
        ["gh", "pr", "comment", str(pr_number),
         "--repo", REPO, "--body", body],
        capture_output=True,
    )
    if result.returncode != 0:
        print(
            f"[{log_prefix}] failed to post comment on PR #{pr_number}:\n{result.stderr}",
            file=sys.stderr,
        )
        return False
    return True


def _issue_has_label(issue_number: int, label: str) -> bool:
    """Re-fetch an issue's labels and check for *label*. Avoids stale-snapshot races."""
    try:
        issue = _gh_json([
            "issue", "view", str(issue_number),
            "--repo", REPO,
            "--json", "labels",
        ])
    except subprocess.CalledProcessError:
        return False
    return label in [l["name"] for l in (issue or {}).get("labels", [])]  # noqa: E741


def _build_issue_block(issue: dict) -> str:
    """Build the issue block shared by plan, select, and fix agents."""
    block = (
        f"## Issue\n\n"
        f"### #{issue['number']} — {issue['title']}\n\n"
        f"{issue.get('body') or '(no body)'}\n"
    )
    comments = issue.get("comments") or []
    if comments:
        block += "\n### Comments\n\n"
        for c in comments:
            author = c.get("author", {}).get("login", "unknown")
            body = c.get("body", "")
            block += f"**{author}:**\n{body}\n\n"
    return block


def _fetch_linked_issue_block(pr_body: str) -> str:
    """Return an '## Original issue' block if the PR body contains a Refs link.

    Auto-improve PRs include a ``Refs <REPO>#<N>`` line in their body.
    Parse it, fetch the issue, and format the block. Returns "" on any
    failure (missing link, deleted issue, network error).
    """
    if not pr_body:
        return ""
    m = re.search(rf"Refs\s+{re.escape(REPO)}#(\d+)", pr_body)
    if not m:
        return ""
    issue_num = int(m.group(1))
    try:
        issue_data = _gh_json([
            "issue", "view", str(issue_num),
            "--repo", REPO,
            "--json", "number,title,body",
        ])
    except subprocess.CalledProcessError:
        return ""
    if not issue_data:
        return ""
    return (
        f"## Original issue\n\n"
        f"### #{issue_data['number']} — {issue_data.get('title', '')}\n\n"
        f"{issue_data.get('body') or '(no body)'}\n\n"
    )


def _build_implement_user_message(issue: dict, attempt_history_block: str = "") -> str:
    """Build the dynamic per-run user message for the cai-implement agent.

    The system prompt, tool allowlist, and hard rules live in
    `.claude/agents/cai-implement.md`; durable per-agent learnings live
    in its `memory: project` pool. The wrapper passes the issue
    body, reviewer comments, and (when available) a summary of
    prior closed PRs for this issue.
    """
    return _build_issue_block(issue) + attempt_history_block


def close_issue_not_planned(
    issue_number: int,
    comment: str,
    log_prefix: str = "cai",
) -> bool:
    """Close a GitHub issue as 'not planned' with a marker comment.

    Posts the marker via `gh issue comment` first, then closes with
    `--reason "not planned"`. The two calls are split because
    `gh issue close --comment X` silently drops the comment when the
    issue is already closed — splitting guarantees the audit-trail
    marker is persisted regardless of the issue's initial state.

    Returns True when the close call succeeds (posting the comment
    is best-effort and only logs a warning on failure).
    """
    comment_result = subprocess.run(
        ["gh", "issue", "comment", str(issue_number),
         "--repo", REPO,
         "--body", comment],
        capture_output=True,
        text=True,
    )
    if comment_result.returncode != 0:
        print(
            f"[{log_prefix}] WARNING: gh issue comment failed for "
            f"#{issue_number}: {comment_result.stderr.strip()}",
            file=sys.stderr, flush=True,
        )
    result = subprocess.run(
        ["gh", "issue", "close", str(issue_number),
         "--repo", REPO,
         "--reason", "not planned"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(
            f"[{log_prefix}] WARNING: gh issue close failed for "
            f"#{issue_number}: {result.stderr.strip()}",
            file=sys.stderr, flush=True,
        )
        return False
    return True


# ---------------------------------------------------------------------------
# PR-linked-issue helpers (shared by cmd_verify and cmd_audit)
# ---------------------------------------------------------------------------

def _find_linked_pr(issue_number: int):
    """Search PRs whose body references this issue. Returns the most recent."""
    try:
        prs = _gh_json([
            "pr", "list",
            "--repo", REPO,
            "--state", "all",
            "--search", f'"Refs {REPO}#{issue_number}" in:body',
            "--json", "number,state,mergedAt,headRefName,createdAt",
            "--limit", "20",
        ]) or []
    except subprocess.CalledProcessError as e:
        print(
            f"[cai verify] gh pr list (issue #{issue_number}) failed:\n{e.stderr}",
            file=sys.stderr,
        )
        return None
    if not prs:
        return None
    # Most recently created first.
    prs.sort(key=lambda p: p["createdAt"], reverse=True)
    return prs[0]


def _recover_stale_pr_open(issues: list[dict], *, log_prefix: str = "cai") -> list[dict]:
    """Transition :pr-open issues whose linked PR was closed (unmerged) back to :refined.

    Also recovers issues with no linked PR at all (dangling :pr-open).
    Returns the list of issues that were successfully recovered.
    """
    recovered: list[dict] = []
    subcmd = log_prefix.split()[-1]
    for issue in issues:
        if LABEL_IN_PROGRESS in {lbl["name"] for lbl in issue.get("labels", [])}:
            continue
        pr = _find_linked_pr(issue["number"])
        issue_labels = {lbl["name"] for lbl in issue.get("labels", [])}
        remove_labels = [LABEL_PR_OPEN, LABEL_MERGE_BLOCKED, LABEL_REVISING]
        if pr is None:
            if _set_labels(issue["number"], add=[LABEL_RAISED], remove=remove_labels, log_prefix=log_prefix):
                comment = (
                    "## Auto-improve: rolling back to :raised\n\n"
                    "No linked PR found for this `:pr-open` issue. "
                    "Resetting to `:raised` so the refine subagent can re-structure it "
                    "and the implement subagent can then attempt a fresh fix.\n\n"
                    f"---\n_Rolled back automatically by `{log_prefix}`._"
                )
                _run(["gh", "issue", "comment", str(issue["number"]),
                      "--repo", REPO, "--body", comment], capture_output=True)
                log_run(subcmd, repo=REPO, issue=issue["number"],
                        pr=0, result="rollback_no_pr", exit=0)
                print(
                    f"[{log_prefix}] recovered stale :pr-open on #{issue['number']} "
                    f"(no linked PR found)",
                    flush=True,
                )
                recovered.append(issue)
            continue
        state = (pr.get("state") or "").upper()
        if state == "CLOSED":
            if _set_labels(issue["number"], add=[LABEL_REFINED], remove=remove_labels, log_prefix=log_prefix):
                comment = (
                    "## Auto-improve: rolling back to :refined\n\n"
                    f"Linked PR #{pr['number']} was closed without merging. "
                    "Resetting this issue to `:refined` so it can flow through "
                    "the refinement and planning cycle again before a human "
                    "can re-approve it for the implement subagent.\n\n"
                    f"---\n_Rolled back automatically by `{log_prefix}`._"
                )
                _run(["gh", "issue", "comment", str(issue["number"]),
                      "--repo", REPO, "--body", comment], capture_output=True)
                log_run(subcmd, repo=REPO, issue=issue["number"],
                        pr=pr["number"], result="rollback_closed_pr", exit=0)
                print(
                    f"[{log_prefix}] recovered stale :pr-open on #{issue['number']} "
                    f"(PR #{pr['number']} closed unmerged)",
                    flush=True,
                )
                recovered.append(issue)
    return recovered


def _close_orphaned_prs(*, log_prefix: str = "cai") -> list[dict]:
    """Close open auto-improve PRs whose linked issue has been closed.

    If the linked issue is CLOSED, the revise handler silently skips
    the PR, and the merge handler cannot land it if it has conflicts,
    so the PR sits open forever accumulating conflict with main. This
    recovery step closes such orphaned PRs and strips the stale
    ``:pr-open`` / ``:revising`` labels from the closed issue so the
    state machine converges.

    Shared by ``cai audit`` (periodic sweep) and ``cai revise``
    (per-PR sweep). Returns the list of closed entries as
    ``[{"pr": int, "issue": int}, …]`` so callers can surface counts.
    """
    subcmd = log_prefix.split()[-1]
    try:
        prs = _gh_json([
            "pr", "list",
            "--repo", REPO,
            "--state", "open",
            "--limit", "100",
            "--json", "number,headRefName",
        ])
    except subprocess.CalledProcessError:
        return []

    closed_rows: list[dict] = []
    for pr in prs or []:
        branch = pr.get("headRefName", "")
        m = re.match(r"auto-improve/(\d+)-", branch)
        if not m:
            continue
        issue_number = int(m.group(1))
        pr_number = pr["number"]

        try:
            issue = _gh_json([
                "issue", "view", str(issue_number),
                "--repo", REPO,
                "--json", "state",
            ])
        except subprocess.CalledProcessError:
            continue
        if not issue or issue.get("state", "").upper() != "CLOSED":
            continue

        print(
            f"[{log_prefix}] PR #{pr_number}: linked issue #{issue_number} "
            f"is CLOSED; closing orphaned PR",
            flush=True,
        )

        comment = (
            "## Orphaned PR: closing automatically\n\n"
            f"Linked issue #{issue_number} is closed, so this PR has "
            "no tracking issue to drive it forward. Closing "
            "automatically to prevent it from blocking the auto-improve "
            "loop (revise skips PRs whose issue is closed; merge cannot "
            "land it if it conflicts with `main`).\n\n"
            "---\n"
            f"_Closed automatically by `{log_prefix}` orphan recovery. "
            "Reopen the issue if you want the implement subagent to retry._"
        )
        close_res = _run(
            ["gh", "pr", "close", str(pr_number),
             "--repo", REPO, "--delete-branch", "--comment", comment],
            capture_output=True,
        )
        if close_res.returncode != 0:
            print(
                f"[{log_prefix}] PR #{pr_number}: gh pr close failed:\n"
                f"{close_res.stderr}",
                file=sys.stderr,
            )
            continue

        _set_labels(
            issue_number,
            remove=[LABEL_PR_OPEN, LABEL_REVISING],
            log_prefix=log_prefix,
        )
        log_run(subcmd, repo=REPO, pr=pr_number, issue=issue_number,
                result="closed_orphaned_pr", exit=0)
        closed_rows.append({"pr": pr_number, "issue": issue_number})

    return closed_rows


# ---------------------------------------------------------------------------
# Cross-instance ownership lock — auto-improve:locked
# ---------------------------------------------------------------------------
#
# Problem: nothing prevents two cai containers (different hosts, or two
# containers on the same host) from picking up the same issue/PR in the
# same tick. Local flock is per-container, FSM label transitions are
# non-atomic gh edits. Solution: a first-writer-wins remote lock keyed
# on the LABEL_LOCKED label plus a ``<!-- cai-lock owner=... -->`` claim
# comment, with the oldest claim comment as the arbiter.
#
# _HELD_LOCKS is a refcount map (NOT a set) because the drain driver
# wraps the whole drive in a lock, then dispatch_issue/dispatch_pr also
# wrap their own dispatch — when the inner wrapper releases, the outer
# drive must still hold. Refcount lets the inner acquire/release pair
# bump and decrement without touching GitHub.
_HELD_LOCKS: dict[tuple[str, int], int] = {}

# Stabilization-poll constants. After posting our claim comment we poll
# the comment list until two consecutive fetches return the same ordered
# set of cai-lock comments, mitigating GitHub's read-after-write replica
# lag (seen as both instances thinking they are the oldest claim).
_LOCK_STABILIZE_TIMEOUT_S = 3.0
_LOCK_STABILIZE_INTERVAL_S = 0.5


def _delete_issue_comment(comment_id: int, *, log_prefix: str = "cai lock") -> bool:
    """Best-effort delete of an issue/PR comment via the REST API.

    GitHub uses a single ``/repos/{owner}/{repo}/issues/comments/{id}``
    endpoint for both issue comments and the issue-level comments on
    PRs (the ``cai-lock`` claims are always posted there). Failures are
    logged but swallowed — the watchdog finishes any cleanup the
    release path could not.
    """
    result = _run(
        ["gh", "api", "-X", "DELETE",
         f"/repos/{REPO}/issues/comments/{comment_id}"],
        capture_output=True,
    )
    if result.returncode != 0:
        print(
            f"[{log_prefix}] failed to delete comment {comment_id}: "
            f"{result.stderr.strip()}",
            file=sys.stderr,
        )
        return False
    return True


def _list_lock_comments(number: int) -> list[dict]:
    """Return ``cai-lock`` claim comments on issue/PR ``number``, ordered oldest-first.

    Uses the issues comments endpoint (works for both issues and PRs —
    the issue-level comments on a PR live there). Sort key is
    ``(created_at, id)`` so a same-millisecond tie still has a
    deterministic winner via the monotonic comment id.
    """
    try:
        comments = _gh_json([
            "api", f"/repos/{REPO}/issues/{number}/comments",
            "--paginate",
        ]) or []
    except subprocess.CalledProcessError:
        return []
    out: list[dict] = []
    for c in comments:
        body = c.get("body", "") or ""
        m = CAI_LOCK_COMMENT_RE.search(body)
        if not m:
            continue
        out.append({
            "id": c.get("id"),
            "owner": m.group("owner"),
            "created_at": c.get("created_at", ""),
        })
    out.sort(key=lambda c: (c.get("created_at", ""), c.get("id", 0)))
    return out


def _acquire_remote_lock(kind: str, number: int) -> bool:
    """Attempt to acquire the cross-instance lock on issue/PR ``number``.

    Returns True when this process now owns the lock, False when another
    instance won the race (the caller must yield without dispatching).
    Re-entry is idempotent: a second call from the same process bumps a
    refcount and returns True without any GitHub round-trip.

    Protocol:
      1. Post a ``<!-- cai-lock owner=INSTANCE_ID acquired=... -->``
         claim comment. If this fails, no label is ever applied, so
         a comment-post failure cannot strand an orphan ``:locked``
         label behind a missing claim (the ``stale_hours=inf``
         signature seen in the watchdog audit).
      2. Add LABEL_LOCKED via the appropriate label helper. If this
         fails, delete the just-posted claim comment and abort so
         neither artifact is left on the target.
      3. Poll the lock-comment list until two consecutive snapshots
         agree (or the timeout elapses), to dampen GitHub
         read-after-write replica lag.
      4. The owner of the oldest comment wins; losers strip
         LABEL_LOCKED and delete their losing claim comment.
    """
    key = (kind, number)
    if key in _HELD_LOCKS:
        _HELD_LOCKS[key] += 1
        return True

    # Post the claim comment BEFORE applying the label so a comment
    # failure cannot leave an orphan ``:locked`` label behind — an
    # orphan label presents as ``stale_hours=inf`` in the watchdog
    # (see _lock_claim_age_seconds) and wastes a TTL cycle before
    # being cleaned up.
    acquired = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    body = f"<!-- cai-lock owner={INSTANCE_ID} acquired={acquired} -->"
    posted = (_post_issue_comment if kind == "issue" else _post_pr_comment)(
        number, body, log_prefix="cai lock"
    )
    if not posted:
        return False

    if kind == "issue":
        labelled = _set_labels(number, add=[LABEL_LOCKED], log_prefix="cai lock")
    else:
        labelled = _set_pr_labels(number, add=[LABEL_LOCKED], log_prefix="cai lock")
    if not labelled:
        # Couldn't label — delete the claim comment we just posted so
        # we don't leave a stranded comment with no label either.
        for entry in _list_lock_comments(number):
            if entry.get("owner") == INSTANCE_ID and entry.get("id") is not None:
                _delete_issue_comment(int(entry["id"]))
        return False

    locks = _stabilize_lock_comments(number)

    if not locks:
        # Replica still lagging or list endpoint failed entirely. Treat
        # as "did not acquire" rather than risk two instances both thinking
        # they own the target — and clean up so the watchdog isn't needed.
        if kind == "issue":
            _set_labels(number, remove=[LABEL_LOCKED], log_prefix="cai lock")
        else:
            _set_pr_labels(number, remove=[LABEL_LOCKED], log_prefix="cai lock")
        return False

    winner = locks[0]["owner"]
    if winner == INSTANCE_ID:
        _HELD_LOCKS[key] = 1
        print(f"[cai lock] acquired {kind} #{number}", flush=True)
        return True

    # Lost the race. Delete only our own claim comment; do NOT remove
    # LABEL_LOCKED — the winner's comment is still the oldest and the
    # label belongs to them. The winner removes the label on release;
    # the watchdog removes it after _STALE_LOCKED_HOURS if the winner
    # crashes.
    for entry in locks:
        if entry.get("owner") == INSTANCE_ID and entry.get("id") is not None:
            _delete_issue_comment(int(entry["id"]))
    print(
        f"[cai lock] lost {kind} #{number} to {winner}",
        flush=True,
    )
    return False


def _stabilize_lock_comments(number: int) -> list[dict]:
    """Poll the cai-lock comment list until two consecutive snapshots agree.

    Closes GitHub's read-after-write replica-lag window between posting
    a claim and reading the ordered set of claims. Returns the last
    snapshot when the timeout elapses without convergence.
    """
    deadline = time.monotonic() + _LOCK_STABILIZE_TIMEOUT_S
    prev: list[dict] = _list_lock_comments(number)
    while time.monotonic() < deadline:
        time.sleep(_LOCK_STABILIZE_INTERVAL_S)
        curr = _list_lock_comments(number)
        # Compare ordered (id, owner) tuples — created_at can drift if
        # GitHub rewrites timestamps server-side, but ids are stable.
        prev_key = [(c.get("id"), c.get("owner")) for c in prev]
        curr_key = [(c.get("id"), c.get("owner")) for c in curr]
        if prev_key == curr_key and curr:
            return curr
        prev = curr
    return prev


def _release_remote_lock(kind: str, number: int) -> bool:
    """Release a previously-acquired ownership lock. Idempotent.

    Decrements the refcount; only performs GitHub cleanup (delete claim
    comment + remove label) when the count reaches zero, which lets the
    drain-level outer acquire keep the lock while inner dispatch_*
    wrappers acquire/release around their own work.
    """
    key = (kind, number)
    if key not in _HELD_LOCKS:
        return True
    _HELD_LOCKS[key] -= 1
    if _HELD_LOCKS[key] > 0:
        return True

    locks = _list_lock_comments(number)
    for entry in locks:
        if entry.get("owner") == INSTANCE_ID and entry.get("id") is not None:
            _delete_issue_comment(int(entry["id"]))

    if kind == "issue":
        _set_labels(number, remove=[LABEL_LOCKED], log_prefix="cai lock")
    else:
        _set_pr_labels(number, remove=[LABEL_LOCKED], log_prefix="cai lock")

    del _HELD_LOCKS[key]
    return True


def blocking_issue_numbers(labels) -> set:
    """Return the set of blocker issue numbers declared via
    ``blocked-on:<N>`` labels on *labels*.

    Accepts both gh-JSON dict shapes (``{"name": "…"}``) and raw
    string shapes, matching the two shapes seen elsewhere in this
    module (``_set_labels`` vs. ``_list_*``).
    """
    out: set = set()
    for lb in labels or []:
        name = lb.get("name") if isinstance(lb, dict) else lb
        if not name:
            continue
        m = BLOCKED_ON_LABEL_RE.match(name)
        if m:
            out.add(int(m.group(1)))
    return out


def open_blockers(blocker_numbers, *, cache=None) -> set:
    """Resolve *blocker_numbers* to the subset that are still open.

    A blocker that doesn't exist (gh 404), is closed, or cannot be
    resolved (network failure) is treated as NOT blocking — err on
    the side of letting work proceed rather than stranding a
    candidate forever. When *cache* is provided it is a
    ``dict[int, bool]`` that maps blocker number → is_open; the
    helper populates missing entries in place so callers amortise
    lookups across loop iterations.
    """
    if cache is None:
        cache = {}
    still_open: set = set()
    for n in blocker_numbers:
        if n not in cache:
            try:
                data = _gh_json([
                    "issue", "view", str(n),
                    "--repo", REPO,
                    "--json", "number,state",
                ])
                cache[n] = bool(data and data.get("state") == "OPEN")
            except subprocess.CalledProcessError:
                cache[n] = False  # unresolvable → not blocking
        if cache[n]:
            still_open.add(n)
    return still_open


def close_issue_completed(
    issue_number: int,
    comment: str,
    log_prefix: str = "cai",
) -> bool:
    """Close a GitHub issue as 'completed' with an audit comment.

    Use for terminal SOLVED transitions where the work was actually done
    (maintenance drain, human-resume to SOLVED). For dismissals/no-action
    closes, use ``close_issue_not_planned`` instead.
    """
    result = subprocess.run(
        ["gh", "issue", "close", str(issue_number),
         "--repo", REPO,
         "--reason", "completed",
         "--comment", comment],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(
            f"[{log_prefix}] WARNING: gh issue close failed for "
            f"#{issue_number}: {result.stderr.strip()}",
            file=sys.stderr, flush=True,
        )
        return False
    return True
