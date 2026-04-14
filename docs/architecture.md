# Architecture

## Pipeline Overview

`robotsix-cai` is a self-improving agent system. The continuous loop runs inside a long-lived Docker container and drives GitHub issues through a well-defined lifecycle:

1. **Raise** — `cai analyze`, `cai propose`, `cai code-audit`, `cai audit`, or a human files an issue labeled `auto-improve:raised` (the sole entry point).
2. **Triage** — `cai triage` calls `cai-triage` to classify the issue as REFINE, DISMISS_DUPLICATE, DISMISS_RESOLVED, or HUMAN. DISMISS verdicts at HIGH confidence close the issue; others route to REFINE, setting a `kind:{code,maintenance}` label. Label transitions to `auto-improve:triaging` → `auto-improve:refining` (or `auto-improve:human-needed`).
3. **Refine** — `cai refine` calls `cai-refine` to rewrite the issue into a structured plan with steps, verification, and scope guardrails. Label transitions to `auto-improve:refined`.
5. **Plan** — `cai plan` runs plan-select agents to generate and select an implementation plan. The plan is stored in the issue body. The select agent emits a trailing `Confidence:` line: at `HIGH` the label auto-transitions to `auto-improve:plan-approved`; at `MEDIUM` / `LOW` / missing it diverts to `auto-improve:human-needed` with a pending marker so an admin can review the plan.
6. **Admin Resume (low-confidence only)** — `cai unblock` classifies an admin's comment on a `:human-needed` issue; a HIGH-confidence `PLAN_APPROVED` verdict fires `human_to_plan_approved` and the issue continues. Ambiguous or dissenting admin comments send the issue back through the planner.
7. **Fix** — `cai implement` calls `cai-implement` on `auto-improve:plan-approved` issues in a fresh git worktree. The wrapper commits, pushes, and opens a PR. Label transitions to `auto-improve:in-progress` → `auto-improve:pr-open`.
8. **Review** — `cai review-pr` checks for ripple-effect inconsistencies, posts findings as PR comments, and transitions the PR into `pr:reviewing-docs` (clean) or `pr:revision-pending` (findings). `cai review-docs` only runs on PRs in `pr:reviewing-docs`: it checks for stale documentation, directly fixes issues it can resolve, and commits/pushes those changes to the PR branch. Remaining unfixable issues are posted as `### Finding: stale_docs` comments. Docs review is terminal — on clean the PR stays in `pr:reviewing-docs` so `cai merge` can advance; on a docs push the PR drops back to `pr:reviewing-code` for re-review.
9. **Revise** — `cai revise` calls `cai-revise` or `cai-rebase` to address review comments or rebase conflicts. Label transitions to `auto-improve:revising`.
9.5. **CI Fix** — `cai fix-ci` calls `cai-fix-ci` to diagnose and fix failing GitHub Actions checks on open PRs. The subagent reads the failure log (last 200 lines, up to 2 failing checks), locates the root cause in the clone, and makes the minimal fix. A per-SHA marker comment (`## CI-fix subagent: fix attempt`) is always posted after each run — whether or not a fix was produced — so the loop guard fires on the next tick if CI is still red. PRs with unaddressed review comments are skipped (left for `cai revise`); PRs with `:needs-human-review` or `:merge-blocked` are always skipped.
10. **Merge** — `cai merge` calls `cai-merge` to assess confidence and auto-merges PRs that meet the threshold. Label transitions to `auto-improve:merged`.
11. **Confirm** — `cai confirm` calls `cai-confirm` to verify the merged fix actually resolved the original issue. Label transitions to `auto-improve:solved` or re-queues to `:refined`.

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
| `auto-improve:needs-spike` | Needs research investigation (`cai spike`) |
| `auto-improve:needs-exploration` | Needs autonomous exploration (`cai explore`) |
| `auto-improve:planned` | Plan generated and stored in issue body; confidence gate pending |
| `auto-improve:plan-approved` | Plan approved (HIGH confidence auto-approval or admin resume); ready for implement subagent |
| `auto-improve:parent` | Parent issue; child sub-issues carry the work |
| `audit:raised` | Audit finding awaiting triage by `cai audit-triage` |
| `audit:needs-human` | Audit finding escalated to human |
| `merge-blocked` | PR has a blocking review finding; will not auto-merge |
| `needs-human-review` | Issue or PR requires human attention |
| `pr:reviewing-code` | PR is in code review (`cai review-pr`); a new SHA lands here on any push |
| `pr:revision-pending` | `cai review-pr` posted findings; `cai revise` will address them |
| `pr:reviewing-docs` | Code review clean; docs review is next, and merge fires from this state once a clean docs comment lands at HEAD |
| `pr:ci-failing` | Checks are red; `cai fix-ci` is the action — returns to `pr:reviewing-code` after a push |

## The Cycle Command

`cai cycle` orchestrates the full pipeline in a single blocking run:

1. **Verify + confirm** — sync label state with actual PR/issue state.
2. **Recover stale locks** — roll back `:in-progress` and `:revising` issues past their timeout.
3. **Ingest unlabeled** — attach `auto-improve` to any unlabeled issues that belong to the pipeline.
4. **Drain PRs** — for each open auto-improve PR: revise → fix-ci → review-pr → review-docs → merge.
5. **Implement loop** — repeatedly call `implement`, `spike`, or `explore` on `auto-improve:plan-approved` issues until no eligible work remains, draining PRs after each implementation. `:raised`, `:refined`, and `:planned` issues are not consumed here — they wait on the auto-improve:plan-approved gate.
6. **Plan-all** — run `plan-all` to drain every open `:raised` / `:refined` issue through triage → refine → plan. HIGH-confidence plans auto-promote to `:plan-approved` and feed the implement loop; lower-confidence plans land in `:human-needed` for admin review.
7. **Final confirm** — one last confirm pass.

`plan-all` also runs on its own cron line (default `30 * * * *`) so the `:planned` queue stays current between cycles.

## Agent Execution Modes

### Worktree agents

`cai-code-audit`, `cai-implement`, `cai-git`, `cai-plan`, `cai-propose`, `cai-propose-review`, `cai-rebase`, `cai-review-docs`, `cai-review-pr`, `cai-revise`, `cai-select`, `cai-update-check` run in a **fresh git worktree clone**. The wrapper clones the repo and passes the clone path as the agent's work directory. The agent itself never runs `git push` or `gh` — the wrapper owns all remote state.

For code-editing agents (`cai-implement`, `cai-revise`, `cai-rebase`), the wrapper also:
- Creates an isolated branch (`auto-improve/<issue>-<slug>`)
- Commits all changes, pushes the branch, and opens (or updates) a PR
- Deletes the worktree on completion

For review and planning agents (`cai-code-audit`, `cai-git`, `cai-plan`, `cai-propose`, `cai-propose-review`, `cai-review-pr`, `cai-select`, `cai-update-check`), the clone provides read access to the full repo tree; these agents emit structured output (findings, plans, verdicts) that the wrapper acts on deterministically — no commit or PR is created.

`cai-review-docs` is a special review agent that can edit documentation: it has `Edit` and `Write` tools to fix stale docs directly, and the wrapper automatically commits and pushes any changes to the same PR branch (not to a new isolated branch).

### Clone agents

`cai-explore`, `cai-spike` also operate on a fresh repo clone but follow a different pattern. The wrapper clones the repo and passes it via `--add-dir` (not as `cwd`). These agents post outcomes (Findings, Refined Issue, Blocked) directly to the GitHub issue via `gh issue` commands. They do not create branches or PRs.

### Read-only agents

`cai-analyze`, `cai-audit`, `cai-audit-triage`, `cai-confirm`, `cai-cost-optimize`, `cai-merge`, `cai-refine` receive all context in their prompt or read the live repo without a dedicated clone. They emit structured output (findings, verdicts, label transitions) that the wrapper acts on deterministically.
