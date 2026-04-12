---
title: CLI Reference
nav_order: 2
---

# CLI Reference

All subcommands are invoked as `cai <subcommand> [options]`.

Every subcommand requires:
- `gh auth login` completed (GitHub CLI authenticated)
- Either `claude` OAuth session active (in-container login) **or** `ANTHROPIC_API_KEY` set

---

## Core pipeline

### `cai init`
Smoke test — verifies the Claude API connection is working. Prints a
short greeting from Claude and exits. Run this first after install to
confirm auth is configured correctly.

### `cai analyze`
Reads recent Claude Code session transcripts from `~/.claude/projects/`,
passes them to the analyzer agent, and publishes any findings as GitHub
issues labelled `auto-improve:raised`.

### `cai fix [--issue N]`
Selects the highest-scoring `auto-improve:refined` (or `:requested`)
issue, generates dual fix plans, runs the fix subagent, and opens a pull
request.

| Option | Default | Description |
|--------|---------|-------------|
| `--issue N` | auto-select | Target a specific issue number instead of the scoring-based queue |

### `cai revise [--pr N]`
Iterates on open PRs that have unaddressed review comments (label
`:revising`). Resolves merge conflicts and addresses reviewer feedback.

| Option | Default | Description |
|--------|---------|-------------|
| `--pr N` | auto-select | Target a specific PR number |

### `cai verify`
Scans all open auto-improve issues and PRs and updates labels to reflect
the current merge state (e.g. transitions `:pr-open` → `:merged` when
the PR is merged).

### `cai confirm [--issue N]`
Verifies that each `auto-improve:merged` issue has actually been resolved
by the merged PR. Closes the issue or re-raises it as needed.

| Option | Default | Description |
|--------|---------|-------------|
| `--issue N` | auto-select | Target a specific issue number |

### `cai cycle`
Runs the full pipeline in one command: `verify` → `fix` → `revise` →
`review-pr` → `review-docs` → `merge` → `confirm`. Used by the cron
scheduler for unattended operation.

---

## Review & merge

### `cai review-pr [--pr N]`
Pre-merge ripple-effect review. Walks the PR diff and checks the broader
codebase for inconsistencies the PR introduced but did not update.
Posts findings as a PR comment.

| Option | Default | Description |
|--------|---------|-------------|
| `--pr N` | auto-select | Target a specific PR number |

### `cai review-docs [--pr N]`
Pre-merge documentation review. Checks whether changes to user-facing
behavior, CLI interface, or configuration require updates to `docs/`.
Posts findings as a PR comment.

| Option | Default | Description |
|--------|---------|-------------|
| `--pr N` | auto-select | Target a specific PR number |

### `cai merge [--pr N]`
Confidence-gated auto-merge. Merges a bot PR when the `cai-merge` agent
reports confidence ≥ the configured threshold (default: `high`). Skips
PRs labelled `needs-human-review`.

| Option | Default | Description |
|--------|---------|-------------|
| `--pr N` | auto-select | Target a specific PR number |

---

## Refinement & research

### `cai refine [--issue N]`
Rewrites a human-filed `auto-improve:raised` issue into a structured
auto-improve plan (problem, steps, verification, scope guardrails, likely
files) so the fix agent can act on it.

| Option | Default | Description |
|--------|---------|-------------|
| `--issue N` | auto-select | Target a specific issue number |

### `cai spike [--issue N]`
Investigates open-ended questions for issues labelled
`auto-improve:needs-spike`. Produces structured findings, a refined
issue, or a `Blocked` outcome.

| Option | Default | Description |
|--------|---------|-------------|
| `--issue N` | auto-select | Target a specific issue number |

### `cai explore [--issue N]`
Autonomous exploration and benchmarking for issues labelled
`auto-improve:needs-exploration`. Runs concrete measurements and feeds
findings back into the pipeline.

| Option | Default | Description |
|--------|---------|-------------|
| `--issue N` | auto-select | Target a specific issue number |

---

## Audit & observability

### `cai audit`
Runs the queue/PR consistency audit agent. Checks for issues stuck in
invalid label states and posts an audit report as a GitHub issue.

### `cai audit-triage`
Autonomously resolves `audit:raised` findings — closes duplicates,
confirms resolved issues, or escalates to human review. No PRs are
opened.

### `cai code-audit`
Static audit of the `robotsix-cai` source tree for concrete
inconsistencies, dead code, and missing cross-file references.

### `cai cost-optimize`
Weekly cost-reduction proposal. Analyzes spending trends in
`cai-cost.jsonl` and proposes one optimization per run.

### `cai cost-report [--days N] [--top N] [--by {category|agent|day}]`
Prints a human-readable summary of Claude API costs from the cost log.

| Option | Default | Description |
|--------|---------|-------------|
| `--days N` | `7` | Window in days to include |
| `--top N` | `10` | Number of most-expensive invocations to list |
| `--by` | `category` | Aggregation grouping (`category`, `agent`, or `day`) |

### `cai health-report [--dry-run]`
Automated pipeline health report with anomaly detection. Posts the
report as a GitHub issue unless `--dry-run` is passed.

| Option | Default | Description |
|--------|---------|-------------|
| `--dry-run` | off | Print to stdout without posting a GitHub issue |

---

## Creative & maintenance

### `cai propose`
Weekly creative improvement proposal. Explores the codebase and proposes
ambitious improvements, from small wins to full architectural reworks.

### `cai update-check`
Checks for new Claude Code releases and emits findings for new versions,
feature adoptions, deprecations, and best-practice changes.

---

## Development

### `cai test`
Runs the project test suite (`pytest tests/`).
