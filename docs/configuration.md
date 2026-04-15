# Configuration

## Environment Variables

| Variable | Purpose | Default |
|---|---|---|
| `ANTHROPIC_API_KEY` | API authentication for headless `claude` invocations inside the container | Required |
| `CAI_ADMIN_LOGINS` | Comma-separated list of GitHub logins authorized to use the `human:solved` label to unblock stuck issues and PRs. Without this, the `human:solved` workflow is silently ignored and parked tasks remain unblocked. See `cai unblock` in the CLI reference for details. | _(optional; unblock workflow disabled if not set)_ |
| `CAI_MERGE_CONFIDENCE_THRESHOLD` | Minimum confidence level for `cai merge` auto-merge (`high`, `medium`, `disabled`) | `high` |
| `CAI_MERGE_MAX_DIFF_LEN` | Maximum character length for PR diffs passed to the merge agent; test files are prioritised within the budget so they remain visible even for large PRs | `40000` |

`CAI_MERGE_CONFIDENCE_THRESHOLD` controls how aggressively the merge agent promotes PRs:
- `high` — only auto-merge when `cai-merge` emits a `high` confidence verdict
- `medium` — also auto-merge `medium` confidence verdicts
- `disabled` — skip auto-merge entirely (useful during testing)

## Agent Schedules

Cron schedules are configurable via environment variables. Default values are set in `entrypoint.sh`; most are also explicitly configured in `docker-compose.yml`.

`CAI_CYCLE_SCHEDULE` drives the unified dispatcher: each tick runs restart-recover → `dispatch_oldest_actionable()`, which picks the oldest open issue or PR whose lifecycle state has a handler and runs the matching handler in `cai_lib/actions/`. A flock serializes overlapping runs. The planner confidence gate is unchanged — HIGH auto-promotes to `:plan-approved`; MEDIUM / LOW / missing diverts to `:human-needed` with a pending marker and a comment explaining why the plan didn't reach HIGH confidence (e.g., unverified assumptions, ambiguous scope). An admin comment resumes it via `cai unblock`. `cai dispatch --issue N` / `cai dispatch --pr N` remains callable manually or from GitHub Actions for targeted retries. Verify and audit run on their own independent cron schedules (`CAI_VERIFY_SCHEDULE`, `CAI_AUDIT_SCHEDULE`).

| Variable | Default | Description |
|---|---|---|
| `CAI_CYCLE_SCHEDULE` | `0 * * * *` | Restart-recovery + dispatch one actionable issue/PR |
| `CAI_VERIFY_SCHEDULE` | `15 * * * *` | Label-state reconciliation (cmd_verify) — removes deprecated cai-managed labels from open issues, then keeps :pr-open / :merged / etc. consistent with actual GitHub state. |
| `CAI_ANALYZER_SCHEDULE` | `0 0 * * *` | Daily transcript analysis and issue raising |
| `CAI_AUDIT_SCHEDULE` | `0 */6 * * *` | Every 6 hours — queue/PR lifecycle audit |
| `CAI_CODE_AUDIT_SCHEDULE` | `0 3 * * 0` | Weekly (Sunday 03:00 UTC) — source tree audit |
| `CAI_PROPOSE_SCHEDULE` | `0 4 * * 0` | Weekly (Sunday 04:00 UTC) — creative proposals |
| `CAI_COST_OPTIMIZE_SCHEDULE` | `0 5 * * 0` | Weekly (Sunday 05:00 UTC) — cost-reduction analysis |
| `CAI_UPDATE_CHECK_SCHEDULE` | `0 4 * * 1` | Weekly (Monday 04:00 UTC) — Claude Code release check |
| `CAI_HEALTH_REPORT_SCHEDULE` | `0 7 * * 1` | Weekly (Monday 07:00 UTC) — pipeline health report |
| `CAI_CHECK_WORKFLOWS_SCHEDULE` | `0 */6 * * *` | Every 6 hours — GitHub Actions workflow check |

Schedule values use standard cron format: `minute hour day month weekday`. To disable a scheduled agent, set its variable to an empty string or a comment value.

## Transcript Analysis Variables

| Variable | Default | Description |
|---|---|---|
| `CAI_TRANSCRIPT_WINDOW_DAYS` | `7` | Only parse session transcripts from the last N days |
| `CAI_TRANSCRIPT_MAX_FILES` | `50` | Read at most N recent transcript files (0 = no limit) |

## Settings File

`.claude/settings.json` configures Claude Code for both interactive and headless sessions.

Key fields:

- **`permissions.allow`** — tool allowlist rules. Headless (`claude -p`) sessions inherit these rules. The implement/revise/rebase agents run with a restricted allowlist (no `Bash`, no `git push`) enforced by per-agent `tools:` frontmatter.
- **`model`** — default model for interactive sessions (agents override this via their own `model:` frontmatter).
- **`env`** — environment variables injected into every Claude Code session.

Agent-level overrides live in `.claude/agents/<name>.md` YAML frontmatter (`tools:`, `model:`, `description:`).

## Paths and Directories

| Path | Purpose |
|---|---|
| `/home/cai/.claude/projects` | Transcript directory — Claude Code writes `.jsonl` session files here |
| `/app/.claude/agent-memory` | Per-agent persistent memory files (checked into git) |
| `/var/log/cai/cai.log` | Structured run log (JSON lines, one entry per `cai` invocation) |
| `/var/log/cai/cai-cost.jsonl` | Per-invocation cost log (input/output tokens + USD) |
| `/var/log/cai/cai-outcomes.jsonl` | Fix/revise outcome log (issue number, verdict, PR URL) |
| `/var/log/cai/review-pr-patterns.jsonl` | Review-PR finding category log (used by `cai analyze`) |
