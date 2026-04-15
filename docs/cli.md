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

One cycle tick: restart-recover stale locks → drain the actionable queue. The drain loops "pick oldest actionable issue/PR → run its state handler" until the queue is empty (or a loop guard / max-iter cap fires). A flock serializes overlapping runs. No explicit per-phase ordering — the FSM label is the source of truth and the dispatcher picks the handler for whichever state the oldest actionable item is in. Verify and audit run on their own crons (`CAI_VERIFY_SCHEDULE`, `CAI_AUDIT_SCHEDULE`).

No arguments.

## dispatch

Single entry point into the FSM dispatcher. Fetches an issue or PR, reads its lifecycle state from labels, and invokes the handler registered for that state in `cai_lib/actions/`. If the handler crashes, the next tick picks the same state and runs the same handler — resume is free because every handler is written to be safely re-enterable.

Three modes:

| Invocation | Behavior |
|---|---|
| `cai dispatch` | Drain the actionable queue: dispatch the oldest open issue/PR, repeat until empty (used by `cai cycle`). |
| `cai dispatch --issue N` | Dispatch a specific issue by number. |
| `cai dispatch --pr N` | Dispatch a specific PR by number. |

Terminal or parked states (SOLVED, HUMAN_NEEDED, PR_HUMAN_NEEDED, PR MERGED) have no handler — the dispatcher returns without doing anything.

## health-report

Generate an automated pipeline health report with anomaly detection: cost trends, issue throughput, pipeline stalls, and fix quality metrics. Posts the report as a GitHub issue.

| Argument | Type | Description |
|---|---|---|
| `--dry-run` | flag | Print report to stdout without posting a GitHub issue |

## init

Seed the loop with a smoke test, but only if no prior transcripts exist. If transcripts already exist, exits immediately.

No arguments.

## propose

Clone the repo and run `cai-propose` (creative improvements) followed by `cai-propose-review` to evaluate feasibility before filing issues.

No arguments.

## test

Run the project test suite via `unittest discover`.

No arguments.

## unblock

Scan open issues labelled `auto-improve:human-needed` and attempt to resume the FSM via admin comments. For each issue with a pending-transition marker in its body and at least one comment from an admin login (`CAI_ADMIN_LOGINS`), invokes the `cai-unblock` Haiku agent to classify the comment into a `ResumeTo:` target, then fires the matching `human_to_<state>` transition and strips the marker. Confidence below `HIGH` leaves the issue parked.

PR-side (`auto-improve:pr-human-needed`) is not yet wired — follow-up.

No arguments.

## update-check

Clone the repo and run `cai-update-check` to compare the current pinned Claude Code version against latest releases and emit findings for new versions or deprecations.

No arguments.

## verify

Walk `auto-improve:pr-open` issues and transition labels based on actual PR state (merged → `:merged`, closed → `:raised`, etc.).

No arguments.
