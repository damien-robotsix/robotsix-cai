"""Phase E entry point — subcommand dispatcher.

Subcommands:

    python cai.py init      Smoke-test claude -p only if the transcript
                            volume has no prior sessions. Used to seed
                            the self-improvement loop on a fresh
                            install; a no-op once transcripts exist.

    python cai.py analyze   Parse prior transcripts with parse.py, pipe
                            the combined analyzer prompt through
                            claude -p, and publish findings via
                            publish.py.

    python cai.py fix       Pick the oldest issue labelled
                            `auto-improve:raised` or `auto-improve:
                            requested`, lock it via the `:in-progress`
                            label, clone the repo into /tmp, run the
                            fix subagent (full tool permissions), and
                            open a PR if the agent produced a diff.
                            Rolls back the label on empty diff or any
                            failure.

    python cai.py verify    Mechanical, no-LLM. Walk issues with
                            `:pr-open`, find their linked PR by `Refs`
                            search, and transition the label:
                            merged → `:merged`,
                            closed-unmerged → `:raised`.

The container runs `entrypoint.sh`, which executes `init`, `analyze`,
`fix`, and `verify` once synchronously at startup, then hands off to
supercronic. Each cron tick is a fresh process.

The gh auth check is done once per subcommand invocation. We want a
clear error message in docker logs if credentials ever disappear from
the cai_gh_config volume.

No third-party Python dependencies — only stdlib.
"""

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path


REPO = "damien-robotsix/robotsix-cai"
SMOKE_PROMPT = "Say hello in one short sentence."

# Root of claude-code's per-cwd transcript dirs. claude-code writes
# `/root/.claude/projects/<sanitized-cwd>/<session-id>.jsonl` for every
# session, so this directory contains one subdir per cwd:
#   * `-app/`            — sessions started by cai.py inside /app
#   * `-tmp-cai-fix-<N>/` — sessions started by the fix subagent in
#                          its per-issue clone under /tmp
# The analyzer parses *all* of them so the fix subagent's tool-rich
# sessions feed back into the next analyzer cycle.
TRANSCRIPT_DIR = Path("/root/.claude/projects")

# Files baked into the image alongside cai.py.
PARSE_SCRIPT = Path("/app/parse.py")
PUBLISH_SCRIPT = Path("/app/publish.py")
ANALYZER_PROMPT = Path("/app/prompts/backend-auto-improve.md")
FIX_PROMPT = Path("/app/prompts/backend-fix.md")

# Issue lifecycle labels.
LABEL_RAISED = "auto-improve:raised"
LABEL_REQUESTED = "auto-improve:requested"
LABEL_IN_PROGRESS = "auto-improve:in-progress"
LABEL_PR_OPEN = "auto-improve:pr-open"
LABEL_MERGED = "auto-improve:merged"


# ---------------------------------------------------------------------------
# Common helpers
# ---------------------------------------------------------------------------

def _run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    """Thin wrapper around subprocess.run with text mode and check=False."""
    return subprocess.run(cmd, text=True, check=False, **kwargs)


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
        print("       Credentials are expected in the cai_gh_config volume.", file=sys.stderr)
        print("       Run the installer's login step, or do it manually:", file=sys.stderr)
        print("         docker compose run --rm cai gh auth login", file=sys.stderr)
        print(file=sys.stderr)
        print(result.stderr.strip() or result.stdout.strip(), file=sys.stderr)
        return 1
    return 0


def _transcript_dir_is_empty() -> bool:
    if not TRANSCRIPT_DIR.exists():
        return True
    return not any(TRANSCRIPT_DIR.rglob("*.jsonl"))


# ---------------------------------------------------------------------------
# init
# ---------------------------------------------------------------------------

def cmd_init(args) -> int:
    """Seed the loop with a smoke test, only if nothing exists yet."""
    if not _transcript_dir_is_empty():
        print("[cai init] transcripts already present; skipping smoke test", flush=True)
        return 0

    print("[cai init] no prior transcripts; running smoke test to seed loop", flush=True)
    result = _run(["claude", "-p", SMOKE_PROMPT])
    if result.returncode != 0:
        print(f"[cai init] smoke test failed (exit {result.returncode})", flush=True)
    return result.returncode


# ---------------------------------------------------------------------------
# analyze
# ---------------------------------------------------------------------------

def cmd_analyze(args) -> int:
    """Parse prior transcripts, ask claude to analyze, publish findings."""
    print("[cai analyze] running self-analyzer", flush=True)

    if not TRANSCRIPT_DIR.exists():
        print(
            f"[cai analyze] no transcript dir at {TRANSCRIPT_DIR}; nothing to analyze",
            flush=True,
        )
        return 0

    parsed = _run(
        ["python", str(PARSE_SCRIPT), str(TRANSCRIPT_DIR)],
        capture_output=True,
    )
    if parsed.returncode != 0:
        print(
            f"[cai analyze] parse.py failed (exit {parsed.returncode}):\n{parsed.stderr}",
            flush=True,
        )
        return parsed.returncode

    parsed_signals = parsed.stdout.strip()
    prompt_text = ANALYZER_PROMPT.read_text()

    full_prompt = (
        f"{prompt_text}\n\n"
        "## Parsed signals\n\n"
        "```json\n"
        f"{parsed_signals}\n"
        "```\n"
    )

    analyzer = _run(
        ["claude", "-p"],
        input=full_prompt,
        capture_output=True,
    )
    print(analyzer.stdout, flush=True)
    if analyzer.returncode != 0:
        print(
            f"[cai analyze] claude -p failed (exit {analyzer.returncode}):\n"
            f"{analyzer.stderr}",
            flush=True,
        )
        return analyzer.returncode

    print("[cai analyze] publishing findings", flush=True)
    published = _run(
        ["python", str(PUBLISH_SCRIPT)],
        input=analyzer.stdout,
    )
    return published.returncode


# ---------------------------------------------------------------------------
# fix
# ---------------------------------------------------------------------------

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slugify(text: str, max_len: int = 50) -> str:
    """Branch-friendly slug — lowercase ascii, dashes, no leading/trailing."""
    slug = _SLUG_RE.sub("-", text.lower()).strip("-")
    if len(slug) > max_len:
        slug = slug[:max_len].rstrip("-")
    return slug or "fix"


def _gh_user_identity() -> tuple[str, str]:
    """Resolve the gh-token owner's git name and email."""
    user = _gh_json(["api", "user"])
    name = user.get("name") or user["login"]
    email = user.get("email") or f"{user['id']}+{user['login']}@users.noreply.github.com"
    return name, email


def _select_fix_target():
    """Return the oldest open issue eligible for the fix subagent.

    Eligible = labelled `:raised` or `:requested`, NOT labelled
    `:in-progress` or `:pr-open`.
    """
    candidates: dict[int, dict] = {}
    for label in (LABEL_RAISED, LABEL_REQUESTED):
        try:
            issues = _gh_json([
                "issue", "list",
                "--repo", REPO,
                "--label", label,
                "--state", "open",
                "--json", "number,title,body,labels,createdAt",
                "--limit", "100",
            ]) or []
        except subprocess.CalledProcessError as e:
            print(
                f"[cai fix] gh issue list (label={label}) failed:\n{e.stderr}",
                file=sys.stderr,
            )
            return None
        for issue in issues:
            label_names = {lbl["name"] for lbl in issue.get("labels", [])}
            if LABEL_IN_PROGRESS in label_names or LABEL_PR_OPEN in label_names:
                continue
            candidates[issue["number"]] = issue

    if not candidates:
        return None

    return min(candidates.values(), key=lambda i: i["createdAt"])


def _set_labels(issue_number: int, *, add: list[str] = (), remove: list[str] = ()) -> bool:
    """Add and/or remove labels on an issue. Returns True on success."""
    args = ["issue", "edit", str(issue_number), "--repo", REPO]
    for label in add:
        args.extend(["--add-label", label])
    for label in remove:
        args.extend(["--remove-label", label])
    result = _run(["gh"] + args, capture_output=True)
    if result.returncode != 0:
        print(
            f"[cai fix] failed to update labels on #{issue_number}:\n{result.stderr}",
            file=sys.stderr,
        )
        return False
    return True


def _build_fix_prompt(issue: dict) -> str:
    prompt = FIX_PROMPT.read_text()
    issue_block = (
        f"## Issue\n\n"
        f"### #{issue['number']} — {issue['title']}\n\n"
        f"{issue.get('body') or '(no body)'}\n"
    )
    return f"{prompt}\n\n{issue_block}"


def _git(work_dir: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    cmd = ["git", "-C", str(work_dir)] + list(args)
    return subprocess.run(cmd, text=True, check=check, capture_output=True)


def cmd_fix(args) -> int:
    """Run the fix subagent against one eligible issue."""
    if args.issue is not None:
        try:
            issue = _gh_json([
                "issue", "view", str(args.issue),
                "--repo", REPO,
                "--json", "number,title,body,labels,state,createdAt",
            ])
        except subprocess.CalledProcessError as e:
            print(f"[cai fix] gh issue view #{args.issue} failed:\n{e.stderr}", file=sys.stderr)
            return 1
        if issue.get("state", "").upper() != "OPEN":
            print(f"[cai fix] issue #{args.issue} is not open; nothing to do", flush=True)
            return 0
        label_names = {lbl["name"] for lbl in issue.get("labels", [])}
        if LABEL_IN_PROGRESS in label_names or LABEL_PR_OPEN in label_names:
            print(
                f"[cai fix] issue #{args.issue} is already locked "
                f"({LABEL_IN_PROGRESS} or {LABEL_PR_OPEN} present); skipping",
                flush=True,
            )
            return 0
    else:
        issue = _select_fix_target()
        if issue is None:
            print("[cai fix] no eligible issues; nothing to do", flush=True)
            return 0

    issue_number = issue["number"]
    title = issue["title"]
    print(f"[cai fix] picked #{issue_number}: {title}", flush=True)

    # 1. Lock — set :in-progress, drop :raised and :requested.
    if not _set_labels(
        issue_number,
        add=[LABEL_IN_PROGRESS],
        remove=[LABEL_RAISED, LABEL_REQUESTED],
    ):
        print(f"[cai fix] could not lock #{issue_number}", file=sys.stderr)
        return 1
    print(f"[cai fix] locked #{issue_number} (label {LABEL_IN_PROGRESS})", flush=True)

    # Make sure git can authenticate over HTTPS via the gh token. This
    # is also done in entrypoint.sh, but redoing it here is cheap and
    # idempotent and lets ad-hoc `docker run` invocations work too.
    _run(["gh", "auth", "setup-git"], capture_output=True)

    work_dir = Path(f"/tmp/cai-fix-{issue_number}")
    locked = True

    def rollback() -> None:
        nonlocal locked
        if not locked:
            return
        _set_labels(
            issue_number,
            add=[LABEL_RAISED],
            remove=[LABEL_IN_PROGRESS],
        )
        locked = False

    try:
        if work_dir.exists():
            shutil.rmtree(work_dir)

        # 2. Clone.
        clone = _run(
            ["gh", "repo", "clone", REPO, str(work_dir), "--", "--depth", "1"],
            capture_output=True,
        )
        if clone.returncode != 0:
            print(f"[cai fix] gh repo clone failed:\n{clone.stderr}", file=sys.stderr)
            rollback()
            return 1

        # 3. Configure git identity from the gh token's owner.
        name, email = _gh_user_identity()
        _git(work_dir, "config", "user.name", name)
        _git(work_dir, "config", "user.email", email)
        print(f"[cai fix] git identity: {name} <{email}>", flush=True)

        # 4. Branch.
        branch = f"auto-improve/{issue_number}-{_slugify(title)}"
        _git(work_dir, "checkout", "-b", branch)

        # 5. Run the fix subagent in the work dir with full permissions.
        prompt = _build_fix_prompt(issue)
        print(f"[cai fix] running fix subagent in {work_dir}", flush=True)
        # `acceptEdits` auto-accepts file edits (Read/Edit/Write/Grep/Glob)
        # without prompting. We don't use `bypassPermissions` because
        # claude-code refuses it when running as root inside the container,
        # and `acceptEdits` is sufficient for code-editing fixes.
        agent = _run(
            ["claude", "-p", "--permission-mode", "acceptEdits"],
            input=prompt,
            cwd=str(work_dir),
            capture_output=True,
        )
        if agent.stdout:
            print(agent.stdout, flush=True)
        if agent.returncode != 0:
            print(
                f"[cai fix] subagent claude -p failed (exit {agent.returncode}):\n"
                f"{agent.stderr}",
                file=sys.stderr,
            )
            rollback()
            return agent.returncode

        # 6. Inspect the working tree. Empty diff = clean exit.
        status = _git(work_dir, "status", "--porcelain", check=False)
        if not status.stdout.strip():
            print(
                f"[cai fix] subagent produced no changes for #{issue_number}; "
                "rolling back to :raised",
                flush=True,
            )
            rollback()
            return 0

        # 7. Commit.
        _git(work_dir, "add", "-A")
        commit_msg = (
            f"auto-improve: {title}\n\n"
            f"Generated by `cai fix` against issue #{issue_number}.\n\n"
            f"Refs damien-robotsix/robotsix-cai#{issue_number}"
        )
        _git(work_dir, "commit", "-m", commit_msg)

        # 8. Push.
        push = _run(
            ["git", "-C", str(work_dir), "push", "-u", "origin", branch],
            capture_output=True,
        )
        if push.returncode != 0:
            print(f"[cai fix] git push failed:\n{push.stderr}", file=sys.stderr)
            rollback()
            return 1

        # 9. Open the PR.
        pr_body = (
            f"Refs damien-robotsix/robotsix-cai#{issue_number}\n\n"
            f"Auto-generated by `cai fix` against the issue above. Please "
            f"review the diff carefully — the fix subagent runs autonomously "
            f"with full tool permissions.\n"
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
            print(f"[cai fix] gh pr create failed:\n{pr.stderr}", file=sys.stderr)
            rollback()
            return 1
        print(f"[cai fix] opened PR: {pr.stdout.strip()}", flush=True)

        # 10. Transition label :in-progress -> :pr-open.
        _set_labels(
            issue_number,
            add=[LABEL_PR_OPEN],
            remove=[LABEL_IN_PROGRESS],
        )
        locked = False
        return 0

    except Exception as e:
        print(f"[cai fix] unexpected failure: {e!r}", file=sys.stderr)
        rollback()
        return 1
    finally:
        if work_dir.exists():
            shutil.rmtree(work_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# verify
# ---------------------------------------------------------------------------

def _find_linked_pr(issue_number: int):
    """Search PRs whose body references this issue. Returns the most recent."""
    try:
        prs = _gh_json([
            "pr", "list",
            "--repo", REPO,
            "--state", "all",
            "--search", f'"Refs damien-robotsix/robotsix-cai#{issue_number}" in:body',
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


def cmd_verify(args) -> int:
    """Walk :pr-open issues and transition labels based on PR state."""
    print("[cai verify] checking pr-open issues", flush=True)
    try:
        issues = _gh_json([
            "issue", "list",
            "--repo", REPO,
            "--label", LABEL_PR_OPEN,
            "--state", "open",
            "--json", "number,title",
            "--limit", "100",
        ]) or []
    except subprocess.CalledProcessError as e:
        print(f"[cai verify] gh issue list failed:\n{e.stderr}", file=sys.stderr)
        return 1

    if not issues:
        print("[cai verify] no pr-open issues; nothing to do", flush=True)
        return 0

    transitioned = 0
    for issue in issues:
        num = issue["number"]
        pr = _find_linked_pr(num)
        if pr is None:
            print(f"[cai verify] #{num}: no linked PR found, leaving as-is", flush=True)
            continue
        state = (pr.get("state") or "").upper()
        if state == "MERGED":
            _set_labels(num, add=[LABEL_MERGED], remove=[LABEL_PR_OPEN])
            print(f"[cai verify] #{num}: PR #{pr['number']} merged → :merged", flush=True)
            transitioned += 1
        elif state == "CLOSED":
            _set_labels(num, add=[LABEL_RAISED], remove=[LABEL_PR_OPEN])
            print(
                f"[cai verify] #{num}: PR #{pr['number']} closed unmerged → :raised",
                flush=True,
            )
            transitioned += 1
        else:
            print(f"[cai verify] #{num}: PR #{pr['number']} still {state}", flush=True)

    print(f"[cai verify] done ({transitioned} transitioned)", flush=True)
    return 0


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(prog="cai")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="Smoke test if no transcripts exist")
    sub.add_parser("analyze", help="Run the analyzer + publish findings")

    fix_parser = sub.add_parser("fix", help="Run the fix subagent")
    fix_parser.add_argument(
        "--issue", type=int, default=None,
        help="Target a specific issue number instead of picking the oldest",
    )

    sub.add_parser("verify", help="Update labels based on PR merge state")

    args = parser.parse_args()

    auth_rc = check_gh_auth()
    if auth_rc != 0:
        return auth_rc

    handlers = {
        "init": cmd_init,
        "analyze": cmd_analyze,
        "fix": cmd_fix,
        "verify": cmd_verify,
    }
    return handlers[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
