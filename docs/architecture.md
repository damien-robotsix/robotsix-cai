# Architecture

## Pipeline Overview

`robotsix-cai` is a self-improving agent system. The continuous loop runs inside a long-lived Docker container and drives GitHub issues through a well-defined lifecycle:

1. **Raise** — `cai analyze`, `cai propose`, `cai code-audit`, `cai audit`, or a human files an issue labeled `auto-improve:raised` or `human:submitted`.
2. **Refine** — `cai refine` calls `cai-refine` to rewrite the issue into a structured plan with steps, verification, and scope guardrails. Label transitions to `auto-improve:refined`.
3. **Plan** — `cai plan` runs plan-select agents to generate and select an implementation plan. The plan is stored in the issue body. Label transitions to `auto-improve:planned` (or `auto-improve:plan-approved` if human-reviewed).
4. **Fix** — `cai fix` calls `cai-fix` in a fresh git worktree. The wrapper commits, pushes, and opens a PR. Label transitions to `auto-improve:in-progress` → `auto-improve:pr-open`.
5. **Review** — `cai review-pr` checks for ripple-effect inconsistencies and posts findings as PR comments. `cai review-docs` then (and only after review-pr completes) checks for stale documentation, directly fixes issues it can resolve, and commits/pushes those changes to the PR branch. Remaining unfixable issues are posted as `### Finding: stale_docs` comments. This ordering is enforced: review-docs skips PRs until review-pr has posted a review comment at the current HEAD SHA.
6. **Revise** — `cai revise` calls `cai-revise` or `cai-rebase` to address review comments or rebase conflicts. Label transitions to `auto-improve:revising`.
7. **Merge** — `cai merge` calls `cai-merge` to assess confidence and auto-merges PRs that meet the threshold. Label transitions to `auto-improve:merged`.
8. **Confirm** — `cai confirm` calls `cai-confirm` to verify the merged fix actually resolved the original issue. Label transitions to `auto-improve:solved` or re-queues to `:refined`.

## Lifecycle Labels

| Label | Meaning |
|---|---|
| `auto-improve:raised` | Newly filed, awaiting refinement |
| `auto-improve:refined` | Has a structured plan, ready for fix |
| `auto-improve:in-progress` | Fix agent is running (lock; 6 h stale timeout) |
| `auto-improve:pr-open` | PR created, awaiting review and merge |
| `auto-improve:revising` | Revise agent is running (lock; 1 h stale timeout) |
| `auto-improve:merged` | PR merged, awaiting confirmation |
| `auto-improve:solved` | Confirmed resolved |
| `auto-improve:no-action` | No fix needed (7 d stale timeout → re-queued to `:raised`) |
| `auto-improve:needs-spike` | Needs research investigation (`cai spike`) |
| `auto-improve:needs-exploration` | Needs autonomous exploration (`cai explore`) |
| `auto-improve:requested` | Explicitly requested by a human |
| `auto-improve:planned` | Plan generated and stored in issue body; awaiting human approval |
| `auto-improve:plan-approved` | Plan approved by human; ready for fix subagent |
| `auto-improve:parent` | Parent issue; child sub-issues carry the work |
| `audit:raised` | Audit finding awaiting triage by `cai audit-triage` |
| `audit:needs-human` | Audit finding escalated to human |
| `human:submitted` | Human-submitted issue awaiting refinement |
| `merge-blocked` | PR has a blocking review finding; will not auto-merge |
| `needs-human-review` | Issue or PR requires human attention |

## The Cycle Command

`cai cycle` orchestrates the full pipeline in a single blocking run:

1. **Verify + confirm** — sync label state with actual PR/issue state.
2. **Recover stale locks** — roll back `:in-progress` and `:revising` issues past their timeout.
3. **Ingest unlabeled** — attach `auto-improve` to any unlabeled issues that belong to the pipeline.
4. **Drain PRs** — for each open auto-improve PR: revise → review-pr → review-docs → merge.
5. **Refine one** — call `refine` on the oldest `:raised` issue to transition it to `:refined` so the fix loop has a candidate.
6. **Fix loop** — repeatedly call `fix`, `spike`, or `explore` until no eligible issues remain, draining PRs after each fix.
7. **Final confirm** — one last confirm pass.

## Agent Execution Modes

### Worktree agents

`cai-code-audit`, `cai-fix`, `cai-git`, `cai-plan`, `cai-propose`, `cai-propose-review`, `cai-rebase`, `cai-review-docs`, `cai-review-pr`, `cai-revise`, `cai-select`, `cai-update-check` run in a **fresh git worktree clone**. The wrapper clones the repo and passes the clone path as the agent's work directory. The agent itself never runs `git push` or `gh` — the wrapper owns all remote state.

For code-editing agents (`cai-fix`, `cai-revise`, `cai-rebase`), the wrapper also:
- Creates an isolated branch (`auto-improve/<issue>-<slug>`)
- Commits all changes, pushes the branch, and opens (or updates) a PR
- Deletes the worktree on completion

For review and planning agents (`cai-code-audit`, `cai-git`, `cai-plan`, `cai-propose`, `cai-propose-review`, `cai-review-pr`, `cai-select`, `cai-update-check`), the clone provides read access to the full repo tree; these agents emit structured output (findings, plans, verdicts) that the wrapper acts on deterministically — no commit or PR is created.

`cai-review-docs` is a special review agent that can edit documentation: it has `Edit` and `Write` tools to fix stale docs directly, and the wrapper automatically commits and pushes any changes to the same PR branch (not to a new isolated branch).

### Clone agents

`cai-explore`, `cai-spike` also operate on a fresh repo clone but follow a different pattern. The wrapper clones the repo and passes it via `--add-dir` (not as `cwd`). These agents post outcomes (Findings, Refined Issue, Blocked) directly to the GitHub issue via `gh issue` commands. They do not create branches or PRs.

### Read-only agents

`cai-analyze`, `cai-audit`, `cai-audit-triage`, `cai-confirm`, `cai-cost-optimize`, `cai-merge`, `cai-refine` receive all context in their prompt or read the live repo without a dedicated clone. They emit structured output (findings, verdicts, label transitions) that the wrapper acts on deterministically.
