"""cai_lib.config — shared constants and path definitions."""

import os
from pathlib import Path


REPO = "damien-robotsix/robotsix-cai"
SMOKE_PROMPT = "Say hello in one short sentence."

# Root of claude-code's per-cwd transcript dirs. claude-code writes
# `~/.claude/projects/<sanitized-cwd>/<session-id>.jsonl` for every
# session, so this directory contains one subdir per cwd:
#   * `-app/`            — sessions started by cai.py inside /app
#   * `-tmp-cai-implement-<N>/` — sessions started by the implement subagent in
#                          its per-issue clone under /tmp
# The analyzer parses *all* of them so the implement subagent's tool-rich
# sessions feed back into the next analyzer cycle.
#
# Path is /home/cai/... because the container runs as the non-root
# `cai` user (uid 1000) — see Dockerfile.
TRANSCRIPT_DIR = Path("/home/cai/.claude/projects")

# Files baked into the image alongside cai.py.
PARSE_SCRIPT = Path("/app/parse.py")
PUBLISH_SCRIPT = Path("/app/publish.py")
# Persistent memory file for the code-audit agent. Stored in the
# named-volume log directory so it survives container restarts.
CODE_AUDIT_MEMORY = Path("/var/log/cai/code-audit-memory.md")
# Persistent memory file for the propose agent (same pattern).
PROPOSE_MEMORY = Path("/var/log/cai/propose-memory.md")
# Persistent memory file for the update-check agent.
UPDATE_CHECK_MEMORY = Path("/var/log/cai/update-check-memory.md")
# Persistent memory file for the cost-optimize agent.
COST_OPTIMIZE_MEMORY = Path("/var/log/cai/cost-optimize-memory.md")
# Persistent memory file for the agent-audit agent.
AGENT_AUDIT_MEMORY = Path("/var/log/cai/agent-audit-memory.md")

# Persistent per-agent memory directory. Each declarative subagent
# has `memory: project` in its frontmatter, which Claude Code stores
# under `.claude/agent-memory/<agent-name>/MEMORY.md` relative to
# the project root. This directory is bind-mounted from the
# `cai_agent_memory` named volume so the memory survives container
# restarts. ALL subagents (both /app agents and the cloned-worktree
# agents) now read/write this path directly because they're all
# invoked with `cwd=/app`. The cloned-worktree agents
# (cai-implement, cai-revise, cai-rebase, cai-review-pr, cai-review-docs, cai-code-audit, cai-propose,
# cai-propose-review, cai-update-check, cai-plan, cai-select, cai-git) operate
# on a clone elsewhere via absolute paths —
# see `_work_directory_block` for the user-message section that
# tells them where the clone is.
AGENT_MEMORY_DIR = Path("/app/.claude/agent-memory")

# Issue lifecycle labels.
LABEL_RAISED = "auto-improve:raised"
LABEL_IN_PROGRESS = "auto-improve:in-progress"
LABEL_PR_OPEN = "auto-improve:pr-open"
LABEL_MERGED = "auto-improve:merged"
LABEL_SOLVED = "auto-improve:solved"
# LABEL_NO_ACTION retired — replaced by gh issue close --reason "not planned"
LABEL_NEEDS_EXPLORATION = "auto-improve:needs-exploration"
LABEL_REFINED = "auto-improve:refined"
LABEL_REVISING = "auto-improve:revising"
LABEL_PARENT = "auto-improve:parent"
LABEL_MERGE_BLOCKED = "merge-blocked"
LABEL_PLANNED = "auto-improve:planned"
LABEL_PLAN_APPROVED = "auto-improve:plan-approved"
# Transient "actively working" states — the driver sets these while the
# corresponding agent runs. Confidence gates on their exit transitions
# divert to :human-needed instead of the nominal target.
LABEL_REFINING = "auto-improve:refining"
LABEL_PLANNING = "auto-improve:planning"
LABEL_APPLYING = "auto-improve:applying"
LABEL_APPLIED  = "auto-improve:applied"
LABEL_HUMAN_NEEDED    = "auto-improve:human-needed"    # IssueState.HUMAN_NEEDED
LABEL_PR_HUMAN_NEEDED = "auto-improve:pr-human-needed" # PRState.PR_HUMAN_NEEDED
# Explicit "admin is done, resume the FSM" signal. An issue/PR parked at
# :human-needed is only considered for resume when this label is *also*
# present — the admin applies it once their comment(s) fully address the
# divert. Replaces the previous "any admin comment triggers resume" model,
# which fired on incidental questions and ambiguous replies.
LABEL_HUMAN_SOLVED = "human:solved"
LABEL_TRIAGING         = "auto-improve:triaging"
LABEL_KIND_CODE        = "kind:code"
LABEL_KIND_MAINTENANCE = "kind:maintenance"

# PR pipeline-state labels — one per PRState. Set by FSM transitions
# (apply_pr_transition) and read by dispatch.
LABEL_PR_REVIEWING_CODE   = "pr:reviewing-code"    # PRState.REVIEWING_CODE
LABEL_PR_REVISION_PENDING = "pr:revision-pending"  # PRState.REVISION_PENDING
LABEL_PR_REVIEWING_DOCS   = "pr:reviewing-docs"    # PRState.REVIEWING_DOCS
LABEL_PR_APPROVED         = "pr:approved"          # PRState.APPROVED
LABEL_PR_REBASING         = "pr:rebasing"          # PRState.REBASING
LABEL_PR_CI_FAILING       = "pr:ci-failing"        # PRState.CI_FAILING

# PR-level label applied by `cai merge` when the verdict is below the
# auto-merge threshold. Lets a human filter open PRs that are waiting
# on their decision (`label:needs-human-review`). Issue #216.
LABEL_PR_NEEDS_HUMAN = "needs-human-review"


# ---------------------------------------------------------------------------
# Run log
# ---------------------------------------------------------------------------

LOG_PATH = Path("/var/log/cai/cai.log")
COST_LOG_PATH = Path("/var/log/cai/cai-cost.jsonl")
REVIEW_PR_PATTERN_LOG = Path("/var/log/cai/review-pr-patterns.jsonl")
OUTCOME_LOG_PATH = Path("/var/log/cai/cai-outcomes.jsonl")


# ---------------------------------------------------------------------------
# Staleness thresholds
# ---------------------------------------------------------------------------

_STALE_IN_PROGRESS_HOURS = 6
_STALE_REVISING_HOURS = 1
_STALE_APPLYING_HOURS = 2
# _STALE_NO_ACTION_DAYS retired — no-action issues are now closed, not relabeled
_STALE_MERGED_DAYS = 14


# ---------------------------------------------------------------------------
# Admin identity
#
# Comma-separated list of GitHub logins whose comments on :human-needed
# issues/PRs are allowed to wake the FSM resume loop. Parsed once at
# import time; empty / unset means no one can unblock via comments (safe
# default).
# ---------------------------------------------------------------------------

ADMIN_LOGINS: frozenset[str] = frozenset(
    login.strip()
    for login in os.environ.get("CAI_ADMIN_LOGINS", "").split(",")
    if login.strip()
)


def is_admin_login(login: str) -> bool:
    """True if *login* is configured as an admin via ``CAI_ADMIN_LOGINS``."""
    return bool(login) and login in ADMIN_LOGINS
