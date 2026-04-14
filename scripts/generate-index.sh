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
  ["parse.py"]="Deterministic signal extractor from Claude Code JSONL transcripts"
  ["publish.py"]="GitHub issue publisher with fingerprint dedup"
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
  [".gitignore"]="Git ignore rules"
  [".cai/pr-context.md"]="Per-PR dossier with touched files, key symbols, and design decisions for the CI-fix subagent"
  [".claude/settings.json"]="Claude Code harness configuration"
  [".claude/agents/cai-analyze.md"]="Agent: parse transcript signals and raise auto-improve findings"
  [".claude/agents/cai-audit.md"]="Agent: audit issue queue and lifecycle state machine"
  [".claude/agents/cai-audit-triage.md"]="Agent: triage \`audit:raised\` findings with structured verdicts"
  [".claude/agents/cai-check-workflows.md"]="Agent: analyze recent GitHub Actions workflow failures and emit structured findings"
  [".claude/agents/cai-code-audit.md"]="Agent: read-only source tree audit for inconsistencies and dead code"
  [".claude/agents/cai-confirm.md"]="Agent: verify merged PRs actually resolved their issues"
  [".claude/agents/cai-cost-optimize.md"]="Agent: weekly cost-reduction analysis"
  [".claude/agents/cai-explore.md"]="Agent: autonomous exploration and benchmarking"
  [".claude/agents/cai-fix-ci.md"]="Agent: diagnose and fix failing CI checks on auto-improve PRs"
  [".claude/agents/cai-git.md"]="Agent: lightweight git operations subagent"
  [".claude/agents/cai-implement.md"]="Agent: autonomous code-editing subagent for code-editing tasks"
  [".claude/agents/cai-merge.md"]="Agent: assess PR correctness and emit merge verdict"
  [".claude/agents/cai-plan.md"]="Agent: generate detailed fix plan for an issue"
  [".claude/agents/cai-propose.md"]="Agent: weekly creative improvement proposals"
  [".claude/agents/cai-propose-review.md"]="Agent: review creative proposals for feasibility"
  [".claude/agents/cai-rebase.md"]="Agent: rebase-only conflict resolution"
  [".claude/agents/cai-refine.md"]="Agent: rewrite human-filed issues into structured plans"
  [".claude/agents/cai-review-docs.md"]="Agent: pre-merge documentation review"
  [".claude/agents/cai-review-pr.md"]="Agent: pre-merge ripple-effect review"
  [".claude/agents/cai-revise.md"]="Agent: handle review comments on auto-improve PRs"
  [".claude/agents/cai-select.md"]="Agent: evaluate and select best fix plan"
  [".claude/agents/cai-spike.md"]="Agent: research spike for needs-spike issues"
  [".claude/agents/cai-unblock.md"]="Agent: classify admin comments on :human-needed issues into FSM resume targets"
  [".claude/agents/cai-update-check.md"]="Agent: check for new Claude Code releases"
  [".github/workflows/admin-only-label.yml"]="CI: restrict auto-improve:requested label to admins"
  [".github/workflows/cleanup-pr-context.yml"]="CI: clean up PR context on close"
  [".github/workflows/docker-publish.yml"]="CI: build and publish Docker image to Docker Hub"
  [".github/workflows/regenerate-docs.yml"]="CI: regenerate CODEBASE_INDEX.md and docs/fsm.md, auto-commit drift"
  ["cai_lib/__init__.py"]="Package init for cai_lib library modules"
  ["cai_lib/cmd_implement.py"]="Helpers for the implement-subagent pipeline"
  ["cai_lib/watchdog.py"]="Stale-lock watchdog that rolls back orphaned :in-progress / :revising labels"
  ["cai_lib/cmd_unblock.py"]="Admin-comment-driven FSM resume for :human-needed issues (calls cai-unblock)"
  ["cai_lib/config.py"]="Shared constants and path definitions"
  ["cai_lib/fsm.py"]="FSM data structures + transition application helpers (Confidence enum, apply_transition, divert-to-human, pending markers)"
  ["cai_lib/github.py"]="GitHub/gh CLI helpers and shared label utilities"
  ["cai_lib/logging_utils.py"]="Logging utilities extracted from cai.py"
  ["cai_lib/subprocess_utils.py"]="Subprocess helpers extracted from cai.py"
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
  ["tests/test_fsm.py"]="Tests for cai_lib.fsm — states, transitions, Confidence, divert, marker, resume helpers"
  ["tests/test_unblock.py"]="Tests for cai_lib.cmd_unblock — admin-comment filtering and agent input formatting"
  ["tests/test_lint.py"]="Lint check: ruff must report zero violations"
  ["tests/test_multistep.py"]="Tests for multi-step plan support"
  ["tests/test_parse.py"]="Tests for parse.py signal extraction"
  ["tests/test_publish.py"]="Tests for publish.py issue publishing"
  ["tests/test_rollback.py"]="Tests for rollback functionality"
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

  git -C "$REPO_ROOT" ls-files | sort | while IFS= read -r f; do
    desc="${DESCRIPTIONS[$f]:-TODO: add description}"
    printf '| `%s` | %s |\n' "$f" "$desc"
  done
} > "$OUTPUT"

echo "Written: $OUTPUT"
