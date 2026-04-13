# CLI Reference

`cai` is the main entry point. Usage: `cai <subcommand> [options]`

Run inside the container: `docker compose exec cai python /app/cai.py <subcommand>`

---

## analyze

Parse prior Claude Code transcripts, invoke the `cai-analyze` agent, and publish findings as GitHub issues labeled `auto-improve:raised`.

No arguments.

## audit

Run the periodic queue/PR consistency audit: roll back stale `:in-progress` locks, clean up orphaned branches, unstick stale `:no-action` issues, recover closed-PR issues, and invoke the `cai-audit` agent for a full state-machine review.

No arguments.

## audit-triage

Autonomously resolve `audit:raised` findings without opening a PR. Calls `cai-audit-triage` which classifies each finding as `close_duplicate`, `close_resolved`, `passthrough`, or `escalate`.

No arguments.

## code-audit

Clone the repo and run `cai-code-audit` to find concrete inconsistencies, dead code, and missing cross-file references.

No arguments.

## confirm

Re-analyze recent transcript signals to verify that `auto-improve:merged` issues are actually resolved. Re-queues unsolved issues (up to 3 attempts) or escalates to `:needs-human-review`.

| Argument | Type | Description |
|---|---|---|
| `--issue INT` | optional | Target a specific issue number instead of queue-based selection |

## cost-optimize

Run the weekly `cai-cost-optimize` agent to analyze spending trends and propose one cost-reduction optimization.

No arguments.

## cost-report

Print a human-readable cost report from `/var/log/cai/cai-cost.jsonl`.

| Argument | Type | Default | Description |
|---|---|---|---|
| `--days INT` | optional | 7 | Window in days to include |
| `--top INT` | optional | 10 | Number of most-expensive invocations to list |
| `--by {category,agent,day}` | optional | category | Aggregation grouping |

## cycle

Continuously run the full pipeline until nothing is left to do: verify + confirm → recover stale locks → drain pending PRs (revise → review-pr → review-docs → merge) → refine one `:raised` or `human:submitted` issue → fix/spike/explore loop → final confirm.

No arguments.

## explore

Run `cai-explore` on the oldest `auto-improve:needs-exploration` issue. Outcomes: close with findings, re-queue to `:raised`, hand off directly to `:refined`, or escalate to `:needs-human-review`.

| Argument | Type | Description |
|---|---|---|
| `--issue INT` | optional | Target a specific issue number |

## fix

Run the `cai-fix` agent against one eligible `auto-improve:refined` issue in a fresh git worktree. The wrapper handles commit, push, and PR creation.

| Argument | Type | Description |
|---|---|---|
| `--issue INT` | optional | Target a specific issue number instead of scoring-based selection |

## health-report

Generate an automated pipeline health report with anomaly detection: cost trends, issue throughput, pipeline stalls, and fix quality metrics. Posts the report as a GitHub issue.

| Argument | Type | Description |
|---|---|---|
| `--dry-run` | flag | Print report to stdout without posting a GitHub issue |

## init

Seed the loop with a smoke test, but only if no prior transcripts exist. If transcripts already exist, exits immediately.

No arguments.

## merge

Confidence-gated auto-merge for bot PRs. Uses `cai-merge` to assess each open PR and merges those meeting the configured confidence threshold.

| Argument | Type | Description |
|---|---|---|
| `--pr INT` | optional | Target a specific PR number |

## propose

Clone the repo and run `cai-propose` (creative improvements) followed by `cai-propose-review` to evaluate feasibility before filing issues.

No arguments.

## refine

Invoke `cai-refine` on the oldest `auto-improve:raised` or `human:submitted` issue to produce a structured implementation plan (transitions to `:refined`).

| Argument | Type | Description |
|---|---|---|
| `--issue INT` | optional | Target a specific issue number |

## plan

Run the plan-select pipeline on the oldest `auto-improve:refined` issue. Clones the repo, runs 2 serial plan agents followed by a select agent, stores the chosen plan in the issue body inside `<!-- cai-plan-start/end -->` markers, and transitions the label to `auto-improve:planned`. Runs on a cron schedule (CAI_PLAN_SCHEDULE); not part of the synchronous startup cycle.

| Argument | Type | Description |
|---|---|---|
| `--issue INT` | optional | Target a specific issue number instead of queue-based selection |

## review-docs

Review open PRs for stale documentation using `cai-review-docs`. Directly fixes stale documentation it finds and pushes commits to the PR branch. Posts `### Fixed: stale_docs` blocks for successfully fixed docs, and `### Finding: stale_docs` blocks for issues that cannot be fixed automatically.

**Note:** `review-docs` skips PRs until `cai review-pr` has reviewed the current HEAD SHA. This enforces the `review-pr` → `review-docs` → `merge` ordering.

| Argument | Type | Description |
|---|---|---|
| `--pr INT` | optional | Target a specific PR number |

## review-pr

Review open PRs for ripple-effect inconsistencies using `cai-review-pr`. Posts `### Finding:` blocks as PR comments.

| Argument | Type | Description |
|---|---|---|
| `--pr INT` | optional | Target a specific PR number |

## revise

Iterate on open PRs that have unaddressed review comments. Runs `cai-revise` (or `cai-rebase` for conflict-only cases) to address comments and push updates.

| Argument | Type | Description |
|---|---|---|
| `--pr INT` | optional | Target a specific PR number |

## spike

Run `cai-spike` on the oldest `auto-improve:needs-spike` issue to investigate unanswered questions. Outcomes mirror `explore`: close, re-queue, refine, or escalate.

| Argument | Type | Description |
|---|---|---|
| `--issue INT` | optional | Target a specific issue number |

## test

Run the project test suite via `unittest discover`.

No arguments.

## update-check

Clone the repo and run `cai-update-check` to compare the current pinned Claude Code version against latest releases and emit findings for new versions or deprecations.

No arguments.

## verify

Walk `auto-improve:pr-open` issues and transition labels based on actual PR state (merged → `:merged`, closed → `:raised`, etc.).

No arguments.
