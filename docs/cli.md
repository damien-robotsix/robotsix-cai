# CLI Reference

`cai` is the main entry point. Usage: `cai <subcommand> [options]`

Run inside the container: `docker compose exec cai python /app/cai.py <subcommand>`

Subcommands group into three categories: **pipeline drivers** (`cycle`, `dispatch`) drain the auto-improve FSM queue; **audit subcommands** (`audit`) file findings into the loop; **utility commands** (`cost-report`, `init`, `rescue`, `test`, `unblock`, `verify`) are operational helpers.

---

## audit

Run an on-demand audit. `<kind>` selects the audit type: the four per-module kinds (`good-practices`, `code-reduction`, `cost-reduction`, `workflow-enhancement`) iterate every module declared in `docs/modules.yaml`, invoke the matching audit agent on each module in turn, and publish each agent's `findings.json` through the existing dedup/dup-check pipeline — each created issue carries a `<!-- module: <name> -->` body footer so future audit runs can scope dedup by module + fingerprint. Per-module failures (agent exit non-zero, missing findings file, publish failure) are logged to stderr and counted but never abort the loop. The `health` kind instead reads `/var/log/cai/audit/*/*.jsonl` for the last 30 days and raises findings for error conditions, stale audits, cost anomalies, and degenerate zero-findings runs.

```bash
cai audit <kind>
```

| Positional | Type | Description |
|---|---|---|
| `<kind>` | required | One of `good-practices`, `code-reduction`, `cost-reduction`, `workflow-enhancement`, `health`. |

One example per supported kind:

```bash
cai audit good-practices
cai audit code-reduction
cai audit cost-reduction
cai audit workflow-enhancement
cai audit health
```

The flat `cai audit-module --kind <kind>` and `cai audit-health` forms remain as hidden back-compat aliases so stale shell aliases from earlier installers keep working; new documentation and scripts should use `cai audit <kind>`.

## cost-report

Print a human-readable cost report from `/var/log/cai/cai-cost.jsonl`.

| Argument | Type | Default | Description |
|---|---|---|---|
| `--days INT` | optional | 7 | Window in days to include |
| `--top INT` | optional | 10 | Number of most-expensive invocations to list |
| `--by {category,agent,day}` | optional | category | Aggregation grouping |

## cycle

One cycle tick: restart-recover stale locks → drain the actionable queue. The drain loops "pick oldest actionable issue/PR → run its state handler" until the queue is empty (or a loop guard / max-iter cap fires). A flock serializes overlapping runs. No explicit per-phase ordering — the FSM label is the source of truth and the dispatcher picks the handler for whichever state the oldest actionable item is in. Verify runs on its own cron (`CAI_VERIFY_SCHEDULE`).

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

## init

Seed the loop with a smoke test, but only if no prior transcripts exist. If transcripts already exist, exits immediately.

No arguments.

## test

Run the project test suite via `unittest discover`.

No arguments.

## unblock

Scan open issues parked at `auto-improve:human-needed` that an admin has explicitly marked ready for resume by applying the `human:solved` label. For each such issue with a pending-transition marker in its body and at least one comment from an admin login (`CAI_ADMIN_LOGINS`), invokes the `cai-unblock` Haiku agent to classify the comment into a `ResumeTo:` target, then fires the matching `human_to_<state>` transition, strips the marker, and removes the `human:solved` label. Confidence below `HIGH` leaves the issue parked (label stays on so the admin can iterate). Issues without `human:solved` are ignored entirely — the admin is free to discuss or ask questions without waking the classifier.

PR-side (`auto-improve:pr-human-needed`) is not yet wired — follow-up.

No arguments.

## verify

Remove deprecated cai-managed labels from open issues, then walk `auto-improve:pr-open` issues and transition labels based on actual PR state (merged → `:merged`, closed → `:raised`, etc.).

No arguments.
