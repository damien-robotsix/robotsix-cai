# agents

Declarative Claude subagent definitions. Each `.md` file under
`.claude/agents/` is a headless subagent with YAML frontmatter (name,
description, tools, model) and a prompt body. Organised by phase:
`audit/`, `implementation/`, `lifecycle/`, `ops/`, `review/`, `utility/`.
Invoked by handlers in `cai_lib/actions/` via the Claude Code `-p` mode.

## Entry points
- `.claude/agents/audit/**` — Periodic audit agents (cost, code, workflows, confirm, analyze, agent-audit).
- `.claude/agents/implementation/**` — Plan, implement, fix-ci, rebase, revise, select.
- `.claude/agents/lifecycle/**` — Triage, refine, explore, propose, propose-review, dup-check, rescue, unblock.
- `.claude/agents/ops/**` — Check-workflows, maintain, update-check.
- `.claude/agents/review/**` — Review-pr, review-docs, merge, comment-filter.
- `.claude/agents/utility/**` — Memorize, git, cost-optimize, external-scout.
- `.claude/settings.json` — Claude Code harness configuration.

## Dependencies
- `cai-lib` — handlers in `cai_lib/actions/` drive these agents.
