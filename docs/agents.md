---
title: Agents
nav_order: 4
---

# Agents

Agents are defined in `.claude/agents/*.md` with YAML frontmatter (`name`, `description`, `tools`, `model`). The FSM dispatcher (`cai dispatch`) selects the appropriate agent based on the current lifecycle state of an issue or PR: each state has one handler in `cai_lib/actions/<name>.py` that invokes the matching subagent and passes context via the prompt.

## When each agent runs

> If you add a new FSM state or agent, update this table. A future auto-generation pass is tracked in issue TBD.

State transitions between these rows are rendered in [the lifecycle FSM diagram](fsm.md). This table answers the complementary question: "which agent runs in each state?"

### Issue pipeline

| State | Handler | Subagent(s) invoked |
|---|---|---|
| `RAISED` | [`handle_triage`](https://github.com/damien-robotsix/robotsix-cai/blob/main/cai_lib/actions/triage.py) | `cai-dup-check` (inline pre-check), then `cai-triage` |
| `TRIAGING` | [`handle_triage`](https://github.com/damien-robotsix/robotsix-cai/blob/main/cai_lib/actions/triage.py) (resume) | `cai-triage` |
| `APPLYING` | [`handle_maintain`](https://github.com/damien-robotsix/robotsix-cai/blob/main/cai_lib/actions/maintain.py) | `cai-maintain` |
| `APPLIED` | [`handle_applied`](https://github.com/damien-robotsix/robotsix-cai/blob/main/cai_lib/actions/maintain.py) | *(no subagent)* |
| `REFINING` | [`handle_refine`](https://github.com/damien-robotsix/robotsix-cai/blob/main/cai_lib/actions/refine.py) | `cai-refine` |
| `NEEDS_EXPLORATION` | [`handle_explore`](https://github.com/damien-robotsix/robotsix-cai/blob/main/cai_lib/actions/explore.py) | `cai-explore` |
| `REFINED` | [`handle_plan`](https://github.com/damien-robotsix/robotsix-cai/blob/main/cai_lib/actions/plan.py) | `cai-plan` ×2 (serial) + `cai-select` |
| `PLANNING` | [`handle_plan`](https://github.com/damien-robotsix/robotsix-cai/blob/main/cai_lib/actions/plan.py) (resume) | `cai-plan` / `cai-select` |
| `PLANNED` | [`handle_plan_gate`](https://github.com/damien-robotsix/robotsix-cai/blob/main/cai_lib/actions/plan.py) | *(confidence gate; no subagent)* |
| `PLAN_APPROVED` | [`handle_implement`](https://github.com/damien-robotsix/robotsix-cai/blob/main/cai_lib/actions/implement.py) | `cai-implement` |
| `IN_PROGRESS` | [`handle_implement`](https://github.com/damien-robotsix/robotsix-cai/blob/main/cai_lib/actions/implement.py) (resume) | `cai-implement` |
| `PR` | [`handle_pr_bounce`](https://github.com/damien-robotsix/robotsix-cai/blob/main/cai_lib/actions/pr_bounce.py) | *(label transition only; no subagent)* |
| `MERGED` | [`handle_confirm`](https://github.com/damien-robotsix/robotsix-cai/blob/main/cai_lib/actions/confirm.py) | `cai-confirm` (and `cai-memorize` post-verification) |
| `HUMAN_NEEDED` | [`handle_human_needed`](https://github.com/damien-robotsix/robotsix-cai/blob/main/cai_lib/cmd_unblock.py) | `cai-unblock` (only when `human:solved` label present) |
| `SOLVED` | *terminal* | *(no handler)* |

### PR pipeline

| State | Handler | Subagent(s) invoked |
|---|---|---|
| `OPEN` | [`handle_open_to_review`](https://github.com/damien-robotsix/robotsix-cai/blob/main/cai_lib/actions/open_pr.py) | *(label transition only)* |
| `REVIEWING_CODE` | [`handle_review_pr`](https://github.com/damien-robotsix/robotsix-cai/blob/main/cai_lib/actions/review_pr.py) | `cai-review-pr` |
| `REVISION_PENDING` | [`handle_revise`](https://github.com/damien-robotsix/robotsix-cai/blob/main/cai_lib/actions/revise.py) | `cai-revise` (or `cai-rebase` when conflict-only) + inline `cai-comment-filter` |
| `REVIEWING_DOCS` | [`handle_review_docs`](https://github.com/damien-robotsix/robotsix-cai/blob/main/cai_lib/actions/review_docs.py) | `cai-review-docs` |
| `CI_FAILING` | [`handle_fix_ci`](https://github.com/damien-robotsix/robotsix-cai/blob/main/cai_lib/actions/fix_ci.py) | `cai-fix-ci` |
| `APPROVED` | [`handle_merge`](https://github.com/damien-robotsix/robotsix-cai/blob/main/cai_lib/actions/merge.py) | `cai-merge` |
| `REBASING` | [`handle_rebase`](https://github.com/damien-robotsix/robotsix-cai/blob/main/cai_lib/actions/rebase.py) | `cai-rebase` |
| `PR_HUMAN_NEEDED` | [`handle_pr_human_needed`](https://github.com/damien-robotsix/robotsix-cai/blob/main/cai_lib/cmd_unblock.py) | `cai-unblock` (only when `human:solved` present) |
| `MERGED` | *terminal* | *(no handler)* |

## Agent catalog

| Agent | Description | Tools | Model | Lifecycle trigger | Mode |
|---|---|---|---|---|---|
| `cai-analyze` | Analyze parsed signals from the cai container's own Claude Code session transcripts and raise auto-improve findings for code, prompt, or workflow issues; writes findings to findings.json | Read, Grep, Glob, Skill, Write | sonnet | Scheduled (cron) | Read-only |
| `cai-agent-audit` | Weekly Opus audit of `.claude/agents/*.md` for Claude Code best-practice violations, unused agents, and near-duplicate purposes. Read-only; writes findings to findings.json plus a memory update | Read, Grep, Glob, Write | opus | Scheduled (weekly, cron) | Read-only |
| `cai-audit` | Audit the current GitHub issue queue, recent PRs, and log tail to find inconsistencies in the auto-improve lifecycle state machine. Findings are pre-screened for duplicates/resolved at publish time via cai-dup-check; survivors enter the standard auto-improve:raised cycle. Writes findings to findings.json | Read, Grep, Glob, Write | opus | Scheduled (cron) | Read-only |
| `cai-check-workflows` | Analyze recent GitHub Actions workflow failures and write structured findings to findings.json for new, unreported failures. Groups related failures and identifies root causes | Read, Grep, Glob, Write | haiku | Scheduled (cron) | Read-only |
| `cai-comment-filter` | Classify PR comments as resolved or unresolved, replacing the commit-timestamp watermark in the revise handler | None | haiku | Inline, invoked by handle_revise (REVISION_PENDING) | Inline-only |
| `cai-code-audit` | Read-only audit of the `robotsix-cai` source tree for concrete inconsistencies, dead code, and missing cross-file references the session-based analyzer cannot catch. Runs in a fresh clone and writes findings to findings.json plus a memory update for the next run | Read, Grep, Glob, Write | sonnet | Scheduled (weekly, cron) | Worktree |
| `cai-confirm` | Verify each `auto-improve:merged` issue is actually resolved; close resolved issues in GitHub as "completed" | Read, Grep, Glob | sonnet | Issue state MERGED | Read-only |
| `cai-cost-optimize` | Weekly cost-reduction agent — analyzes spending trends, proposes one optimization | Read, Grep, Glob | sonnet | Scheduled (weekly, cron) | Read-only |
| `cai-dup-check` | Check whether an issue is a duplicate of another open issue or has already been resolved by a recent commit/PR. Inline-only — all context (target issue, other open issues, recent commits/PRs) is provided in the user message. Minimal tool use. | Read | haiku | Helper, invoked inline by handle_triage | Inline-only |
| `cai-explore` | Autonomous exploration and benchmarking of `:needs-exploration` issues | Read, Grep, Glob, Bash, Agent, Write, Edit | opus | Issue state NEEDS_EXPLORATION | Clone |
| `cai-external-scout` | Weekly agent that scouts mature open-source libraries to replace in-house plumbing and writes one adoption proposal per run to findings.json | Read, Grep, Glob, WebSearch, WebFetch, Write | opus | Scheduled (weekly, cron) | Worktree |
| `cai-fix-ci` | Diagnose and fix failing GitHub Actions checks on open PRs | Read, Edit, Write, Grep, Glob, Agent | sonnet | PR state CI_FAILING | Worktree |
| `cai-implement` | Autonomous code-editing subagent — makes the smallest targeted change for an issue | Read, Edit, Write, Grep, Glob, TodoWrite | sonnet | Issue states PLAN_APPROVED / IN_PROGRESS | Worktree |
| `cai-git` | Lightweight subagent that executes git operations on behalf of other agents | Bash | haiku | Helper (spawned by other agents via Agent tool) | Worktree |
| `cai-maintain` | Read the Ops block from a kind:maintenance issue, execute each declared operation via gh CLI, and emit a Confidence level | Bash, Read | sonnet | Issue state APPLYING | Worktree |
| `cai-merge` | Assess whether a PR correctly implements its linked issue and emit a merge verdict; `docs/**` and `CODEBASE_INDEX.md` are automatically exempt from scope checks | Read | opus | PR state APPROVED | Inline-only |
| `cai-memorize` | Post-solved memory curator — decides whether a solved issue settled a cross-cutting design decision worth persisting to the shared agent memory pool | Read, Write, Edit, Glob | sonnet | Inline, invoked by handle_confirm after solved | Inline-only |
| `cai-plan` | Generate a detailed fix plan for an issue (first of two serial planners) | Read, Grep, Glob, Agent | sonnet | Issue states REFINED / PLANNING (serial ×2) | Worktree |
| `cai-propose` | Weekly creative agent that explores the codebase and proposes ambitious improvements — from small wins to full architectural reworks | Read, Grep, Glob | sonnet | Scheduled (weekly, cron) | Worktree |
| `cai-propose-review` | Evaluate creative proposals for feasibility and value before filing issues | Read, Grep, Glob | sonnet | Helper, invoked by cai-propose before filing | Worktree |
| `cai-rebase` | Lightweight rebase conflict resolution for PRs with no unaddressed review comments | Read, Edit, Write, Grep, Glob, Agent | haiku | PR state REBASING (and conflict-only REVISION_PENDING) | Worktree |
| `cai-refine` | Rewrite human-filed issues into structured plans with steps, verification, and scope guardrails; `docs/**` is implicitly allowed and cannot be forbidden in scope guardrails | Read, Grep, Glob | sonnet | Issue state REFINING | Read-only |
| `cai-review-docs` | Pre-merge documentation review — checks whether PR changes require `/docs` updates, directly fixes stale documentation, and posts findings for issues that cannot be fixed automatically | Read, Grep, Glob, Edit, Write | haiku | PR state REVIEWING_DOCS | Worktree |
| `cai-review-pr` | Pre-merge ripple-effect review — finds inconsistencies the PR introduced but didn't update | Read, Grep, Glob | haiku | PR state REVIEWING_CODE | Worktree |
| `cai-revise` | Handle PR review comments: resolve rebase conflicts AND address unaddressed reviewer comments | Read, Edit, Write, Grep, Glob, Agent | sonnet | PR state REVISION_PENDING (when comments need addressing) | Worktree |
| `cai-select` | Evaluate multiple fix plans and select the best one | Read | opus | Helper, invoked by handle_plan after 2× cai-plan | Worktree |
| `cai-triage` | Triage `auto-improve:raised` issues one at a time — classify as REFINE, PLAN_APPROVE, APPLY, or HUMAN. Inline-only — full issue body is provided in the user message. Minimal tool use. | (none) | haiku | Issue states RAISED / TRIAGING | Inline-only |
| `cai-unblock` | Classify an admin's GitHub comment on an issue or PR parked in the human-needed state into a FSM resume target so the auto-improve pipeline can continue. | Read | haiku | States HUMAN_NEEDED / PR_HUMAN_NEEDED (gated by human:solved) | Inline-only |
| `cai-update-check` | Periodic Claude Code release checker that compares the current pinned version against the latest releases and writes findings to findings.json for new versions, feature adoptions, deprecations, and best-practice changes | Read, Grep, Glob, Write | sonnet | Scheduled (cron) | Worktree |

**Inline-only** agents receive all context in the user message and require no file access. **Worktree** agents run in a fresh git clone provided by the wrapper; code-editing agents (`cai-implement`, `cai-revise`, `cai-rebase`) commit changes and open PRs, while review/planning agents (`cai-code-audit`, `cai-git`, `cai-plan`, `cai-propose`, `cai-propose-review`, `cai-review-docs`, `cai-review-pr`, `cai-select`, `cai-update-check`) read from the clone and emit structured output. The plan-selector (`cai-select`) is additionally invoked with Claude Code's `--json-schema` flag so its final output is a validated JSON object the wrapper can consume without regex extraction. **Clone** agents (`cai-explore`) also run against a fresh repo clone but post outcomes directly to GitHub issues rather than opening PRs. **Read-only** agents read the repo or external data without writing anything.

## Scheduled / on-demand agents

These agents run on a recurring schedule rather than being triggered by issue or PR state changes. Cadence: see each agent's own `.claude/agents/<name>.md` description field and any cron configuration in `cai.py` / `cai_lib/`.

| Agent | Cadence |
|---|---|
| `cai-analyze` | Cron-scheduled (parses session transcripts) |
| `cai-agent-audit` | Weekly (cron) |
| `cai-audit` | Cron-scheduled (issue queue audit) |
| `cai-check-workflows` | Cron-scheduled (GitHub Actions failure analysis) |
| `cai-code-audit` | Weekly (cron) |
| `cai-cost-optimize` | Weekly (cron) |
| `cai-external-scout` | Weekly (cron) |
| `cai-propose` | Weekly (cron) |
| `cai-update-check` | Periodic / cron-scheduled |


## See also

- [Lifecycle FSM transition diagrams](fsm.md)
- [Architecture overview](architecture.md)
- Per-agent prompt sources: [`.claude/agents/`](../.claude/agents/)
