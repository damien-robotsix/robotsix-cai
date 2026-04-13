# Configuration

## Environment Variables

| Variable | Purpose | Default |
|---|---|---|
| `ANTHROPIC_API_KEY` | API authentication for headless `claude` invocations inside the container | Required |
| `CAI_MERGE_CONFIDENCE_THRESHOLD` | Minimum confidence level for `cai merge` auto-merge (`high`, `medium`, `disabled`) | `high` |

`CAI_MERGE_CONFIDENCE_THRESHOLD` controls how aggressively the merge agent promotes PRs:
- `high` — only auto-merge when `cai-merge` emits a `high` confidence verdict
- `medium` — also auto-merge `medium` confidence verdicts
- `disabled` — skip auto-merge entirely (useful during testing)

## Agent Schedules

Cron schedules are configurable via environment variables. Default values are set in `entrypoint.sh`; most are also explicitly configured in `docker-compose.yml`.

The issue-solving pipeline (refine → plan → fix → revise → review-pr → merge → confirm) is driven by a single `CAI_CYCLE_SCHEDULE` line. A flock in `cmd_cycle` serializes overlapping runs, so issues are processed one at a time — each cycle refines, plans, fixes, drains PRs, and only moves to the next issue when the current one is solved or has reached a blocking point (human review requested, `:merge-blocked`, etc.). Individual pipeline subcommands (`fix`, `refine`, `plan`, `spike`, `revise`, `review-pr`, `merge`, `verify`, `confirm`) remain callable manually or from GitHub Actions but no longer have their own cron lines.

| Variable | Default | Description |
|---|---|---|
| `CAI_CYCLE_SCHEDULE` | `0 * * * *` | Hourly full issue-solving pipeline (flock-serialized) |
| `CAI_ANALYZER_SCHEDULE` | `0 0 * * *` | Daily transcript analysis and issue raising |
| `CAI_AUDIT_SCHEDULE` | `0 */6 * * *` | Every 6 hours — queue/PR lifecycle audit |
| `CAI_AUDIT_TRIAGE_SCHEDULE` | `10 */6 * * *` | Every 6 hours — resolve `audit:raised` findings |
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

- **`permissions.allow`** — tool allowlist rules. Headless (`claude -p`) sessions inherit these rules. The fix/revise/rebase agents run with a restricted allowlist (no `Bash`, no `git push`) enforced by per-agent `tools:` frontmatter.
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
| `/var/log/cai/cai-active.json` | Active job state — tracks the current long-running subcommand, target type (issue/pr/none), and start timestamp for observability monitoring. Contains `{pid, cmd, target_type, target_id, start_ts}`. Cleared when the subcommand completes. |
