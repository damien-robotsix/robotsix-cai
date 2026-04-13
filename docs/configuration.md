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
