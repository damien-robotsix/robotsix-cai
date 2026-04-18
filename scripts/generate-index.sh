#!/usr/bin/env bash
# Regenerates CODEBASE_INDEX.md from the tracked file list.
# Descriptions are defined in the associative array below.
# Run from anywhere; the script always operates on the repo root.
set -euo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel)"
OUTPUT="$REPO_ROOT/CODEBASE_INDEX.md"

# ---------------------------------------------------------------------------
# Single source of truth for file descriptions.
# Add a new entry here whenever a file is added to the repo.
# ---------------------------------------------------------------------------
declare -A DESCRIPTIONS=(
  ["cai.py"]="Main CLI dispatcher — 16+ subcommands for the self-improvement loop"
  ["parse.py"]="Wrapper shim — real implementation in cai_lib/parse.py"
  ["publish.py"]="Wrapper shim — real implementation in cai_lib/publish.py"
  ["entrypoint.sh"]="Docker entrypoint — templates crontab, runs initial cycle, execs supercronic"
  ["install.sh"]="Interactive installer for end-users"
  ["Dockerfile"]="Container image definition (Python 3.12 + Node + claude-code CLI)"
  ["docker-compose.yml"]="Multi-service orchestration with named volumes"
  ["CLAUDE.md"]="Shared efficiency guidance loaded by all subagents"
  ["CODEBASE_INDEX.md"]="This file — static file-level index for fast agent orientation"
  ["README.md"]="Project documentation and usage guide"
  ["LICENSE"]="MIT license"
  ["pyproject.toml"]="Python project configuration (ruff lint settings)"
  [".env.example"]="Template for required environment variables"
  ["workspaces.json.example"]="Template for multi-workspace configuration with per-repo cycle schedules"
  [".gitignore"]="Git ignore rules"
  [".claude/settings.json"]="Claude Code harness configuration"
  [".claude/agents/cai-agent-audit.md"]="Agent: weekly audit of .claude/agents/*.md for best-practice violations and unused agents"
  [".claude/agents/cai-analyze.md"]="Agent: parse transcript signals and raise auto-improve findings"
  [".claude/agents/cai-audit.md"]="Agent: audit issue queue and lifecycle state machine"
  [".claude/agents/cai-check-workflows.md"]="Agent: analyze recent GitHub Actions workflow failures and emit structured findings"
  [".claude/agents/cai-code-audit.md"]="Agent: read-only source tree audit for inconsistencies and dead code"
  [".claude/agents/cai-confirm.md"]="Agent: verify merged PRs actually resolved their issues"
  [".claude/agents/cai-cost-optimize.md"]="Agent: weekly cost-reduction analysis"
  [".claude/agents/cai-dup-check.md"]="Agent: cheap haiku pre-check for duplicate / already-resolved issues"
  [".claude/agents/cai-explore.md"]="Agent: autonomous exploration and benchmarking"
  [".claude/agents/cai-external-scout.md"]="Agent: weekly scout for open-source libraries that could replace in-house plumbing"
  [".claude/agents/cai-fix-ci.md"]="Agent: diagnose and fix failing CI checks on auto-improve PRs"
  [".claude/agents/cai-git.md"]="Agent: lightweight git operations subagent"
  [".claude/agents/cai-implement.md"]="Agent: autonomous code-editing subagent for code-editing tasks"
  [".claude/agents/cai-merge.md"]="Agent: assess PR correctness and emit merge verdict"
  [".claude/agents/cai-memorize.md"]="Agent: post-solved memory curator for cross-cutting design decisions"
  [".claude/agents/cai-plan.md"]="Agent: generate detailed fix plan for an issue"
  [".claude/agents/cai-propose.md"]="Agent: weekly creative improvement proposals"
  [".claude/agents/cai-propose-review.md"]="Agent: review creative proposals for feasibility"
  [".claude/agents/cai-rebase.md"]="Agent: rebase-only conflict resolution"
  [".claude/agents/cai-refine.md"]="Agent: rewrite human-filed issues into structured plans"
  [".claude/agents/cai-review-docs.md"]="Agent: pre-merge documentation review"
  [".claude/agents/cai-review-pr.md"]="Agent: pre-merge ripple-effect review"
  [".claude/agents/cai-revise.md"]="Agent: handle review comments on auto-improve PRs"
  [".claude/agents/cai-select.md"]="Agent: evaluate and select best fix plan"
  [".claude/agents/cai-maintain.md"]="Agent: apply approved maintenance ops — runs IaC/config changes and reports Confidence"
  [".claude/agents/cai-triage.md"]="Agent: triage \`auto-improve:raised\` issues one at a time — classify as REFINE, PLAN_APPROVE, APPLY, or HUMAN. Inline-only — full issue body is provided in the user message. No tool use needed."
  [".claude/agents/cai-unblock.md"]="Agent: classify admin comments on :human-needed issues into FSM resume targets"
  [".claude/agents/cai-update-check.md"]="Agent: check for new Claude Code releases"
  [".github/workflows/admin-only-label.yml"]="CI: restrict auto-improve:requested label to admins"
  [".github/workflows/cleanup-pr-context.yml"]="CI: clean up PR context on close"
  [".github/workflows/docker-publish.yml"]="CI: build and publish Docker image to Docker Hub"
  [".github/workflows/regenerate-docs.yml"]="CI: regenerate CODEBASE_INDEX.md and docs/fsm.md, auto-commit drift"
  ["cai_lib/__init__.py"]="Package init for cai_lib library modules"
  ["cai_lib/actions/__init__.py"]="Per-state action handlers for the FSM dispatcher"
  ["cai_lib/actions/confirm.py"]="Handler for IssueState.MERGED — verifies remediation and transitions to :solved"
  ["cai_lib/actions/explore.py"]="Handler for IssueState.NEEDS_EXPLORATION — runs cai-explore"
  ["cai_lib/actions/fix_ci.py"]="Handler for PRState.CI_FAILING — runs cai-fix-ci"
  ["cai_lib/actions/implement.py"]="Handler for IssueState.PLAN_APPROVED / IN_PROGRESS — runs cai-implement"
  ["cai_lib/actions/merge.py"]="Handler for PRState.APPROVED — final merge step"
  ["cai_lib/actions/open_pr.py"]="Handler for PRState.OPEN — tags a fresh PR into :reviewing-code"
  ["cai_lib/actions/rebase.py"]="Handler for PRState.REBASING — runs cai-rebase, posts outcome comment, bounces to REVIEWING_CODE"
  ["cai_lib/actions/plan.py"]="Handler for IssueState.REFINED / PLANNING / PLANNED — runs cai-plan + confidence gate"
  ["cai_lib/actions/pr_bounce.py"]="Handler for IssueState.PR — dispatches the linked PR"
  ["cai_lib/actions/refine.py"]="Handler for IssueState.REFINING — runs cai-refine"
  ["cai_lib/actions/review_docs.py"]="Handler for PRState.REVIEWING_DOCS — runs cai-review-docs"
  ["cai_lib/actions/review_pr.py"]="Handler for PRState.REVIEWING_CODE — runs cai-review-pr"
  ["cai_lib/actions/revise.py"]="Handler for PRState.REVISION_PENDING — runs cai-revise"
  ["cai_lib/actions/maintain.py"]="Handler for IssueState.APPLYING / APPLIED — runs cai-maintain and handles maintenance ops application"
  ["cai_lib/actions/triage.py"]="Handler for IssueState.RAISED / TRIAGING — runs cai-triage"
  ["cai_lib/cmd_agents.py"]="Agent-launch cmd_* functions: analyze, audit, propose, code-audit, agent-audit, update-check, cost-optimize, external-scout"
  ["cai_lib/cmd_implement.py"]="Helpers for the implement-subagent pipeline"
  ["cai_lib/cmd_misc.py"]="CLI subcommands extracted from cai.py: init, verify, cost-report, health-report, check-workflows, test"
  ["cai_lib/cmd_helpers.py"]="Cross-command helpers shared between cai.py and cai_lib/actions/*"
  ["cai_lib/cmd_helpers_git.py"]="Git and worktree helpers for cai action wrappers"
  ["cai_lib/cmd_helpers_github.py"]="GitHub API helpers for cai action wrappers"
  ["cai_lib/cmd_helpers_issues.py"]="Issue-lifecycle helpers for cai action wrappers"
  ["cai_lib/dispatcher.py"]="FSM dispatcher — routes issues/PRs to the handler registered for their state"
  ["cai_lib/watchdog.py"]="Stale-lock watchdog that rolls back orphaned :in-progress / :revising labels"
  ["cai_lib/cmd_unblock.py"]="Admin-comment-driven FSM resume for :human-needed issues (calls cai-unblock)"
  ["cai_lib/dup_check.py"]="Pre-triage duplicate / already-resolved check (calls cai-dup-check haiku subagent)"
  ["cai_lib/config.py"]="Shared constants and path definitions"
  ["cai_lib/fsm.py"]="FSM re-exporter — states, transitions, confidence parsing (implementation split into fsm_states, fsm_transitions, fsm_confidence)"
  ["cai_lib/fsm_confidence.py"]="FSM confidence parsing — Confidence enum and helpers for extracting confidence signals from agent output"
  ["cai_lib/fsm_states.py"]="FSM state enums — IssueState and PRState that represent the auto-improve pipeline states"
  ["cai_lib/fsm_transitions.py"]="FSM transition data and logic — Transition dataclass, transition lists (ISSUE_TRANSITIONS, PR_TRANSITIONS), and apply/query functions"
  ["cai_lib/github.py"]="GitHub/gh CLI helpers and shared label utilities"
  ["cai_lib/logging_utils.py"]="Logging utilities extracted from cai.py"
  ["cai_lib/parse.py"]="Deterministic signal extractor from Claude Code JSONL transcripts"
  ["cai_lib/publish.py"]="GitHub issue publisher with fingerprint dedup"
  ["cai_lib/subprocess_utils.py"]="Subprocess helpers extracted from cai.py"
  ["cai_lib/transcript_sync.py"]="Cross-host transcript sync — push/pull session jsonls to a central SSH server"
  ["scripts/server-cleanup.sh"]="Server-side age/size cleanup for the transcript-sync store (runs on the OVH box, not in the container)"
  ["docs/_config.yml"]="Jekyll configuration for GitHub Pages docs"
  ["docs/agents.md"]="Documentation: agent definitions and pipeline phase mapping"
  ["docs/architecture.md"]="Documentation: pipeline overview and system architecture"
  ["docs/cli.md"]="Documentation: CLI reference for all cai.py subcommands"
  ["docs/configuration.md"]="Documentation: environment variables and configuration"
  ["docs/index.md"]="Documentation site landing page"
  ["scripts/generate-index.sh"]="Generator script for CODEBASE_INDEX.md"
  ["scripts/generate-fsm-docs.py"]="Generator script for docs/fsm.md (renders cai_lib.fsm transitions as Mermaid)"
  ["docs/fsm.md"]="Auto-generated lifecycle FSM diagrams (issue + PR state machines)"
  ["tests/__init__.py"]="Test package init"
  ["tests/test_maintain.py"]="Tests for cai_lib.actions.maintain — handle_maintain confidence routing and FSM transitions"
  ["tests/test_dispatcher.py"]="Tests for the FSM dispatcher and state→handler registries"
  ["tests/test_fsm.py"]="Tests for cai_lib.fsm — states, transitions, Confidence, divert, marker, resume helpers"
  ["tests/test_unblock.py"]="Tests for cai_lib.cmd_unblock — admin-comment filtering and agent input formatting"
  ["tests/test_lint.py"]="Lint check: ruff must report zero violations"
  ["tests/test_multistep.py"]="Tests for multi-step plan support"
  ["tests/test_parse.py"]="Tests for parse.py signal extraction"
  ["tests/test_publish.py"]="Tests for publish.py issue publishing"
  ["tests/test_rollback.py"]="Tests for rollback functionality"
  ["tests/test_transcript_sync.py"]="Tests for cai_lib.transcript_sync — no-op path, parse_source fallback, repo slug"
)

# ---------------------------------------------------------------------------
# Generate the markdown table
# ---------------------------------------------------------------------------
{
  cat <<'HEADER'
# Codebase Index

> Auto-generated by `scripts/generate-index.sh`. Do not edit manually —
> update the descriptions in the generator script instead.

| File | Purpose |
|------|---------|
HEADER

  # Force C locale so ordering is deterministic across machines — GNU
  # sort's default is locale-aware (e.g. underscores collate differently
  # under fr_FR than under C), which otherwise causes churn when contributors
  # regenerate the index on their own machines.
  git -C "$REPO_ROOT" ls-files | grep -v '^\.cai/' | LC_ALL=C sort | while IFS= read -r f; do
    desc="${DESCRIPTIONS[$f]:-TODO: add description}"
    printf '| `%s` | %s |\n' "$f" "$desc"
  done
} > "$OUTPUT"

echo "Written: $OUTPUT"
