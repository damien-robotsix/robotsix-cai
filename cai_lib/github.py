"""cai_lib.github — GitHub/gh CLI helpers and shared label utilities."""

import json
import os
import subprocess
import sys

from cai_lib.config import REPO, TRANSCRIPT_DIR
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


def _set_labels(issue_number: int, *, add: list[str] = (), remove: list[str] = (), log_prefix: str = "cai fix") -> bool:
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
    return label in [l["name"] for l in (issue or {}).get("labels", [])]


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


def _build_fix_user_message(issue: dict, attempt_history_block: str = "") -> str:
    """Build the dynamic per-run user message for the cai-fix agent.

    The system prompt, tool allowlist, and hard rules live in
    `.claude/agents/cai-fix.md`; durable per-agent learnings live
    in its `memory: project` pool. The wrapper passes the issue
    body, reviewer comments, and (when available) a summary of
    prior closed PRs for this issue.
    """
    return _build_issue_block(issue) + attempt_history_block
