# CLI Reference

`cai` is the main entry point. Usage: `cai <subcommand> [options]`

Run inside the container: `docker compose exec cai python /app/cai.py <subcommand>`

---

## analyze

Parse prior Claude Code transcripts, invoke the `cai-analyze` agent, and publish findings as GitHub issues labeled `auto-improve:raised`.

No arguments.

## audit

Run the periodic queue/PR consistency audit: roll back stale `:in-progress` (6-hour TTL), `:revising` (1-hour TTL), and `:applying` (2-hour TTL) locks; clean up orphaned branches; migrate open `:no-action` issues (deprecated label) to closed-as-not-planned; recover `:pr-open` issues whose linked PR was closed; retroactively close closed issues lacking terminal labels (as 'not planned'); and invoke the `cai-audit` agent for a full state-machine review. The audit checks for inconsistencies including duplicates, stuck loops, label corruption, and human-needed issues (pipeline jams, abandoned tasks, repeated diversions, missing divert reasons). Findings are pre-screened for duplicates/resolved via `cai-dup-check` before publishing; only survivors create issues.

```bash
cai audit
```

No arguments.

## audit-module

Run an on-demand per-module audit that dispatches the matching audit agent over selected modules. Module manifests are loaded from `docs/modules.yaml`.

```bash
cai audit-module --kind <kind>
```

| Option | Type | Description |
|---|---|---|
| `--kind` | required | Audit type. Choices: `good-practices`, `code-reduction`, `cost-reduction`, `workflow-enhancement`. |

The command iterates over all modules defined in `docs/modules.yaml` and publishes findings via the existing dedup/dup-check pipeline.

## code-audit

Clone the repo and run `cai-code-audit` to find concrete inconsistencies, dead code, and missing cross-file references.

No arguments.

## agent-audit

Run the weekly agent inventory audit to check `.claude/agents/**/*.md` files for Claude Code best-practice violations, unused agents, and near-duplicate purposes.

No arguments.

## check-workflows

Monitor GitHub Actions for recent workflow failures and publish findings as issues. Fetches recent failed workflow runs (last 24 hours), filters out bot branches, and runs a Haiku agent to identify and group related failures. Findings are published with the `check-workflows` namespace and integrated into the unified auto-improve pipeline.

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

## external-scout

Clone the repo and run `cai-external-scout` to scout for mature open-source libraries that could replace in-house plumbing. The agent walks the codebase, picks one category of in-house utility per run, searches the open-source ecosystem for mature alternatives, and emits a single adoption proposal (or `No findings.` if no candidate passes the fit check). Uses project-scope memory to avoid re-proposing the same category or library.

No arguments.

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

Terminal or parked states (SOLVED, HUMAN_NEEDED, PR_HUMAN_NEEDED, PR MERGED) have no handler — the dispatcher returns without doing anything. Issues reaching the SOLVED state are automatically closed in GitHub as "completed".

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

Scan open issues parked at `auto-improve:human-needed` that an admin has explicitly marked ready for resume by applying the `human:solved` label. For each such issue with a pending-transition marker in its body and at least one comment from an admin login (`CAI_ADMIN_LOGINS`), invokes the `cai-unblock` Haiku agent to classify the comment into a `ResumeTo:` target, then fires the matching `human_to_<state>` transition, strips the marker, and removes the `human:solved` label. Confidence below `HIGH` leaves the issue parked (label stays on so the admin can iterate). Issues without `human:solved` are ignored entirely — the admin is free to discuss or ask questions without waking the classifier.

PR-side (`auto-improve:pr-human-needed`) is not yet wired — follow-up.

No arguments.

## update-check

Clone the repo and run `cai-update-check` to compare the current pinned Claude Code version against latest releases and emit findings for new versions or deprecations.

No arguments.

## verify

Remove deprecated cai-managed labels from open issues, then walk `auto-improve:pr-open` issues and transition labels based on actual PR state (merged → `:merged`, closed → `:raised`, etc.).

No arguments.
