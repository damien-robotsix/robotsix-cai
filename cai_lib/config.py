"""cai_lib.config — shared constants and path definitions."""

import os
import re
import socket
import uuid
from pathlib import Path


REPO: str = os.environ.get("CAI_REPO", "damien-robotsix/robotsix-cai")
SMOKE_PROMPT = "Say hello in one short sentence."


def _repo_slug(repo: str) -> str:
    """Turn ``owner/repo`` into a filesystem-safe slug for server paths."""
    return repo.replace("/", "_")


REPO_SLUG: str = _repo_slug(REPO)

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

# When cross-host transcript sync is enabled (CAI_TRANSCRIPT_SYNC_URL set),
# the analyzer/confirm handlers read from this aggregate mirror — populated
# by `cai transcript-sync` via rsync — instead of the local-only
# TRANSCRIPT_DIR. The mirror holds one subdir per machine-id:
#
#   /home/cai/.claude/projects-aggregate/<machine-id>/<encoded-cwd>/<session>.jsonl
#
# `parse.py` walks .jsonl files recursively, so the extra level of
# nesting is transparent to it.
TRANSCRIPT_AGGREGATE_DIR = Path("/home/cai/.claude/projects-aggregate")

# Cross-host transcript-sync configuration. The feature is a no-op when
# ``CAI_TRANSCRIPT_SYNC_URL`` is unset, so existing single-host
# deployments behave exactly as before. See cai_lib.transcript_sync and
# docs/configuration.md for the full design.
TRANSCRIPT_SYNC_URL: str = os.environ.get("CAI_TRANSCRIPT_SYNC_URL", "").strip()
TRANSCRIPT_SYNC_SSH_KEY = Path(
    os.environ.get("CAI_TRANSCRIPT_SYNC_SSH_KEY", "/home/cai/.ssh/cai_transcript_key")
)
# Bind-mounted from the host's /etc/machine-id — see docker-compose.yml.
# Container's own /etc/machine-id is the container ID and rotates on every
# `docker compose up`, so it's unusable as a stable bucket key.
_HOST_MACHINE_ID_PATH = Path("/etc/host-machine-id")


def _resolve_machine_id() -> str:
    """Resolve the stable per-host identifier used for server bucket paths.

    Resolution order:
      1. ``CAI_MACHINE_ID`` env var (human-readable override — e.g. ``laptop``).
      2. First 12 chars of the host's ``/etc/machine-id`` (bind-mounted at
         ``/etc/host-machine-id`` by docker-compose.yml).
      3. Empty string — callers must treat this as "sync disabled for this
         container" and surface a clear error. We do NOT fall back to the
         container's own hostname: it's usually a random container ID that
         rotates on every restart and would silently create a new server
         bucket on every reboot.
    """
    explicit = os.environ.get("CAI_MACHINE_ID", "").strip()
    if explicit:
        return explicit
    try:
        host_mid = _HOST_MACHINE_ID_PATH.read_text().strip()
    except (FileNotFoundError, PermissionError):
        return ""
    return host_mid[:12] if host_mid else ""


MACHINE_ID: str = _resolve_machine_id()


def _resolve_instance_id() -> str:
    """Globally-unique owner tag for ``auto-improve:locked`` claims.

    Three parts so two cai containers on the same host never generate
    the same tag: ``<machine_id>-<hostname>-<pid>``. Falls back to a
    uuid4 hex when ``MACHINE_ID`` is empty so we never emit an
    ambiguous empty prefix.
    """
    mid = MACHINE_ID or uuid.uuid4().hex[:12]
    return f"{mid}-{socket.gethostname()}-{os.getpid()}"


# Per-process owner tag posted in ``cai-lock`` claim comments. Resolved
# at import time so a single container has one stable identity for its
# lifetime (used by both the cycle process and any direct dispatch_*
# callers within the same process).
INSTANCE_ID: str = _resolve_instance_id()


def transcript_sync_enabled() -> bool:
    """True when transcript-sync is configured. Missing MACHINE_ID disables it."""
    return bool(TRANSCRIPT_SYNC_URL) and bool(MACHINE_ID)


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
# cai-propose-review, cai-update-check, cai-plan, cai-select, cai-git, cai-agent-audit, cai-external-scout) operate
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
# Single-use marker that `cai rescue` sets when it escalates a stuck
# issue to an Opus-backed re-run of the implement phase. Signals the
# downstream `cai-implement` handler to pass `--model claude-opus-4-7`,
# and prevents a second escalation on the same issue if the Opus run
# also parks at :human-needed.
LABEL_OPUS_ATTEMPTED = "auto-improve:opus-attempted"
# Dependency-suppression label. Applied as `blocked-on:<N>` where
# <N> is the issue number of another open GitHub issue. The
# dispatcher's target picker and `cai rescue` both skip any
# issue/PR carrying this label while the referenced blocker
# remains open, so the implement handler's in-session prerequisite
# gate never has to re-divert. Multiple blockers may be declared
# by applying the label once per blocker.
LABEL_BLOCKED_ON_PREFIX = "blocked-on:"
LABEL_TRIAGING         = "auto-improve:triaging"
LABEL_KIND_CODE        = "kind:code"
LABEL_KIND_MAINTENANCE = "kind:maintenance"
LABEL_DEPTH_PREFIX = "depth:"
MAX_DECOMPOSITION_DEPTH: int = int(os.environ.get("CAI_MAX_DECOMPOSITION_DEPTH", "2"))

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

# Cross-instance ownership lock. Orthogonal to the FSM state labels
# (:in-progress, :applying, :revising, …) — :locked marks which cai
# instance currently owns the issue/PR and serializes work across
# containers/hosts. Acquired via _acquire_remote_lock at dispatch entry
# and released via _release_remote_lock; expired by the watchdog after
# _STALE_LOCKED_HOURS so a crashed handler can't strand a target forever.
LABEL_LOCKED = "auto-improve:locked"

# Marker comment used as the first-writer-wins arbiter for :locked. The
# oldest comment matching this regex on an issue/PR identifies the lock
# owner; ties are broken by GitHub comment id (monotonic).
CAI_LOCK_COMMENT_RE = re.compile(
    r"<!--\s*cai-lock\s+owner=(?P<owner>\S+)\s+acquired=(?P<acquired>\S+)\s*-->"
)

BLOCKED_ON_LABEL_RE = re.compile(r"^blocked-on:(\d+)$")


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
# Time-to-live for a remote ownership lock (LABEL_LOCKED). The watchdog
# expires :locked after this many hours so a crashed handler cannot
# strand a target indefinitely. 1h is short enough that recovery is
# operational; long enough that legitimate handlers reliably finish first.
_STALE_LOCKED_HOURS = 1
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
