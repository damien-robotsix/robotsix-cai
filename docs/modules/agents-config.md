# agents-config

Claude Code harness configuration for the agents pipeline. Defines
the permission allowlist, hooks, environment variables, and
defaults that apply whenever the headless `-p` mode invokes any of
the subagents under `.claude/agents/`.

## Key entry points
- [`.claude/settings.json`](../../.claude/settings.json) —
  Claude Code harness configuration. Currently a minimal
  `$schema`-only file; project-level permissions, hooks, and env
  vars would be added here (see
  `/update-config` skill for conventions).

## Inter-module dependencies
- Consumed by **every subagent** under `.claude/agents/` — the
  harness loads `settings.json` before invoking any agent, so
  permissions and hooks apply to all of
  agents-implementation / agents-lifecycle / agents-review /
  agents-ops / agents-utility / audit uniformly.
- Referenced implicitly by **cli** — `cai_lib/subagent/legacy.py`
  `_run_claude_p` launches the headless harness which in turn
  reads this file.
- Imported by **tests** — `tests/test_agent_staging.py` depends
  on the staging-directory conventions that complement the harness
  block on `.claude/agents/*.md` and `CLAUDE.md` writes.

## Operational notes
- **Write block.** Headless `-p` mode hardcodes a write block on
  `.claude/agents/*.md`, `.claude/plugins/`, and `CLAUDE.md`
  paths; edits to these must go through the `.cai-staging/`
  directory (see root `CLAUDE.md`). This file itself is NOT
  blocked, but changes are high-impact and must be reviewed
  carefully.
- **Permissions.** Adding an entry here changes behaviour for
  every subagent globally; prefer narrowing permissions via
  per-agent `tools:` frontmatter when possible.
- **CI implications.** None directly; misconfiguration surfaces
  as runtime errors in the live pipeline. The on-demand
  `cai-audit-good-practices` auditor flags frontmatter drift that
  this file's defaults cannot catch.
- **Cost sensitivity.** Indirect — hooks or default models set
  here can change cost across the entire agent fleet.
