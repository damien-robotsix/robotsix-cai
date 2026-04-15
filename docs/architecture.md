# Architecture

## Pipeline Overview

`robotsix-cai` is a self-improving agent system. The continuous loop runs inside a long-lived Docker container and drives GitHub issues through a well-defined lifecycle.

**The FSM label is the program counter.** Each lifecycle state (issue or PR) has one handler registered in `cai_lib/actions/<name>.py`. A single FSM dispatcher (`cai dispatch`, implemented in `cai_lib/dispatcher.py`) reads the current label on an issue or PR and invokes the matching handler. There are no explicit per-phase entry points — `cai triage`, `cai refine`, `cai plan`, `cai implement`, `cai explore`, `cai confirm`, `cai review-pr`, `cai revise`, `cai review-docs`, `cai fix-ci`, and `cai merge` no longer exist as CLI subcommands. All work flows through `cai dispatch`, which picks up whatever state an issue or PR is parked in and runs the corresponding handler. Handlers are written to be safely re-enterable, so a crashed run is resumed on the next tick simply by dispatching the same state again.

Handler registry (issue states):

| State | Handler file | Role |
|---|---|---|
| `RAISED` / `TRIAGING` | `cai_lib/actions/triage.py` | Classify the issue (REFINE / DISMISS / PLAN_APPROVE / APPLY / HUMAN). DISMISS at HIGH closes; PLAN_APPROVE / APPLY at HIGH skips ahead to `:plan-approved` or `:applying`. |
| `REFINING` | `cai_lib/actions/refine.py` | Rewrite the issue into a structured plan with steps, verification, and scope guardrails. |
| `NEEDS_EXPLORATION` | `cai_lib/actions/explore.py` | Run `cai-explore` to investigate an under-specified issue and route back to `:refining`. |
| `REFINED` / `PLANNING` / `PLANNED` | `cai_lib/actions/plan.py` | Run plan + select, store the plan in the issue body, then apply the confidence gate: HIGH auto-promotes to `:plan-approved`; MEDIUM / LOW / missing diverts to `:human-needed` with a pending marker. |
| `PLAN_APPROVED` / `IN_PROGRESS` | `cai_lib/actions/implement.py` | Run `cai-implement` in a fresh worktree; commit, push, and open a PR. |
| `PR` | `cai_lib/actions/pr_bounce.py` | Bounce to the linked PR's dispatcher. |
| `MERGED` | `cai_lib/actions/confirm.py` | Verify the merged fix actually resolved the issue; transition to `:solved` or re-queue to `:refined`. |

Handler registry (PR states):

| State | Handler file | Role |
|---|---|---|
| `OPEN` | `cai_lib/actions/open_pr.py` | Tag a fresh PR into `pr:reviewing-code`. |
| `REVIEWING_CODE` | `cai_lib/actions/review_pr.py` | Run `cai-review-pr` for ripple-effect findings. |
| `REVISION_PENDING` | `cai_lib/actions/revise.py` | Run `cai-revise` (or `cai-rebase`) to address review comments / rebase conflicts. |
| `REVIEWING_DOCS` | `cai_lib/actions/review_docs.py` | Run `cai-review-docs`; directly fix stale docs and push, or post unfixable findings. On clean, transition to `pr:approved`. |
| `APPROVED` | `cai_lib/actions/merge.py` | Final confidence-gated merge step. This PR state was introduced between `REVIEWING_DOCS` and `MERGED` so the merge action is its own handler rather than a tail on docs review. |
| `CI_FAILING` | `cai_lib/actions/fix_ci.py` | Run `cai-fix-ci` to diagnose and fix failing checks. |

Terminal / parked states (`SOLVED`, `HUMAN_NEEDED`, `PR_HUMAN_NEEDED`, PR `MERGED`) have no handler; the dispatcher returns without doing anything.

Issues still enter the pipeline the same way: `cai analyze`, `cai propose`, `cai code-audit`, `cai audit`, or a human files an issue labeled `auto-improve:raised`. Low-confidence planner outcomes still park at `:human-needed`; the admin resolves the issue in comments and then applies the `human:solved` label, which `cai unblock` picks up to resume the FSM.

## Lifecycle Labels

| Label | Meaning |
|---|---|
| `auto-improve:raised` | Newly filed, awaiting triage |
| `auto-improve:triaging` | Triage agent is running (transient) |
| `auto-improve:refining` | Refine agent is running (transient) |
| `auto-improve:refined` | Has a structured plan, ready for the planning pipeline |
| `auto-improve:in-progress` | Implement agent is running (lock; 6 h stale timeout) |
| `auto-improve:pr-open` | PR created, awaiting review and merge |
| `auto-improve:revising` | Revise agent is running (lock; 1 h stale timeout) |
| `auto-improve:merged` | PR merged, awaiting confirmation |
| `auto-improve:solved` | Confirmed resolved |
| `auto-improve:no-action` | No fix needed (7 d stale timeout → re-queued to `:raised`) |
| `auto-improve:needs-exploration` | Needs autonomous exploration (explore handler) |
| `auto-improve:planned` | Plan generated and stored in issue body; confidence gate pending |
| `auto-improve:plan-approved` | Plan approved (HIGH confidence auto-approval or admin resume); ready for implement subagent |
| `auto-improve:applying` | Maintenance ops are being applied (transient; Step 3 agent drains this state) |
| `auto-improve:applied` | Maintenance ops applied; awaiting verification |
| `auto-improve:parent` | Parent issue; child sub-issues carry the work |
| `audit:raised` | Audit finding awaiting triage by `cai audit-triage` |
| `audit:needs-human` | Audit finding escalated to human |
| `merge-blocked` | PR has a blocking review finding; will not auto-merge |
| `needs-human-review` | Issue or PR requires human attention |
| `pr:reviewing-code` | PR is in code review (review-pr handler); a new SHA lands here on any push |
| `pr:revision-pending` | Review-pr handler posted findings; revise handler will address them |
| `pr:reviewing-docs` | Code review clean; docs review is next |
| `pr:approved` | Docs review clean; merge handler runs the final confidence-gated merge from here |
| `pr:rebasing` | Mergeable=CONFLICTING with main; rebase handler runs cai-rebase, posts an outcome comment, and always bounces back to `pr:reviewing-code` so the rebased SHA is re-reviewed |
| `pr:ci-failing` | Checks are red; fix-ci handler is the action — returns to `pr:reviewing-code` after a push |

## The Cycle Command

`cai cycle` is one tick of the dispatcher loop. The implementation has three phases:

1. **Restart recovery** — roll back `:in-progress`, `:revising`, and `:applying` locks past their stale-timeout.
2. **Drain** — call `dispatch_drain()`, which loops `pick oldest actionable → dispatch handler` until the queue is empty (no more issues/PRs in any handler-backed state). Each `(kind, number)` target is dispatched at most once per drain pass — after dispatch (success or failure) it's added to a per-drain skip set so the picker moves on. A `max_iter=50` cap is the hard upper bound. The cron interval is the wall-clock rate limit and the flock prevents overlapping ticks.
3. **Maintenance apply** — if any `:applying` issues remain (transient state during maintenance operations), call `cai maintain` to drain them by executing the declared operations and transitioning to `:applied` or `:human-needed` based on Confidence.

A flock serializes overlapping runs so two cron ticks cannot dispatch the same item concurrently.

Verify (`cai verify`) and audit (`cai audit`) are **independent cron jobs** — they run on their own schedules (`CAI_VERIFY_SCHEDULE`, `CAI_AUDIT_SCHEDULE`) rather than inside the cycle. Verify syncs label state with actual PR/issue state (merged → `:merged`, closed → `:raised`, etc.); audit runs the queue/PR consistency audit. Maintain operations (Phase 3) are drained within the cycle rather than on a separate schedule because they are transient states that should unblock quickly.

## Agent Execution Modes

### Worktree agents

`cai-code-audit`, `cai-implement`, `cai-git`, `cai-maintain`, `cai-plan`, `cai-propose`, `cai-propose-review`, `cai-rebase`, `cai-review-docs`, `cai-review-pr`, `cai-revise`, `cai-select`, `cai-update-check` run in a **fresh git worktree clone**. The wrapper clones the repo and passes the clone path as the agent's work directory. The agent itself never runs `git push` or `gh` — the wrapper owns all remote state.

For code-editing agents (`cai-implement`, `cai-revise`, `cai-rebase`), the wrapper also:
- Creates an isolated branch (`auto-improve/<issue>-<slug>`)
- Commits all changes, pushes the branch, and opens (or updates) a PR
- Deletes the worktree on completion

For review and planning agents (`cai-code-audit`, `cai-git`, `cai-plan`, `cai-propose`, `cai-propose-review`, `cai-review-pr`, `cai-select`, `cai-update-check`), the clone provides read access to the full repo tree; these agents emit structured output (findings, plans, verdicts) that the wrapper acts on deterministically — no commit or PR is created.

`cai-review-docs` is a special review agent that can edit documentation: it has `Edit` and `Write` tools to fix stale docs directly, and the wrapper automatically commits and pushes any changes to the same PR branch (not to a new isolated branch).

`cai-maintain` is a maintenance agent that executes operations (label mutations, bulk-close, workflow YAML edits) declared in the `Ops:` block of a `kind:maintenance` issue. It runs `gh` CLI commands to perform administrative tasks and emits a Confidence level for transition gating.

### Clone agents

`cai-explore` also operates on a fresh repo clone but follows a different pattern. The wrapper clones the repo and passes it via `--add-dir` (not as `cwd`). This agent posts outcomes (Findings, Refined Issue, Blocked) directly to the GitHub issue via `gh issue` commands. It does not create branches or PRs.

### Read-only agents

`cai-analyze`, `cai-audit`, `cai-audit-triage`, `cai-confirm`, `cai-cost-optimize`, `cai-merge`, `cai-refine` receive all context in their prompt or read the live repo without a dedicated clone. They emit structured output (findings, verdicts, label transitions) that the wrapper acts on deterministically.
