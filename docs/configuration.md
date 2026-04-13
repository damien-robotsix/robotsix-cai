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

All pipeline agents run on cron schedules configurable via environment variables. Default values are set in `entrypoint.sh`; most are also explicitly configured in `docker-compose.yml`.

| Variable | Default | Description |
|---|---|---|
| `CAI_ANALYZER_SCHEDULE` | `0 0 * * *` | Daily transcript analysis and issue raising |
| `CAI_FIX_SCHEDULE` | `15 * * * *` | Hourly fix agent run |
| `CAI_REFINE_SCHEDULE` | `10 * * * *` | Hourly issue refinement |
| `CAI_REVISE_SCHEDULE` | `30 * * * *` | Hourly PR revision (review comments + rebase) |
| `CAI_VERIFY_SCHEDULE` | `45 * * * *` | Hourly label sync with PR state |
| `CAI_REVIEW_PR_SCHEDULE` | `20 * * * *` | Hourly pre-merge consistency review |
| `CAI_MERGE_SCHEDULE` | `35 * * * *` | Hourly confidence-gated auto-merge |
| `CAI_CONFIRM_SCHEDULE` | `0 2 * * *` | Daily post-merge confirmation |
| `CAI_AUDIT_SCHEDULE` | `0 */6 * * *` | Every 6 hours — queue/PR lifecycle audit |
| `CAI_AUDIT_TRIAGE_SCHEDULE` | `10 */6 * * *` | Every 6 hours — resolve `audit:raised` findings |
| `CAI_SPIKE_SCHEDULE` | `0 */2 * * *` | Every 2 hours — research `:needs-spike` issues |
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
| `/var/log/cai/cai-active.json` | Active job lock — prevents concurrent `cai fix`/`revise` runs |
