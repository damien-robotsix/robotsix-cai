# plugins

Claude Code plugins are reusable skill definitions that extend agent
capabilities. Each plugin is a self-contained package under
`.claude/plugins/` containing a manifest, one or more skill definitions,
and supporting Python implementation code.

## Key entry points

- [`.claude/plugins/cai-skills/manifest.json`](../../.claude/plugins/cai-skills/manifest.json) —
  Plugin package metadata defining the plugin name, version, and skill
  discovery path.
- [`.claude/plugins/cai-skills/skills/cost-audit/`](../../.claude/plugins/cai-skills/skills/cost-audit/) —
  Cost exploration and auditing skill (`cost_query`, with optional
  `issue_number` routing for per-issue lookups) used by
  `cai-audit-cost-reduction` to analyze agent spend patterns.
  Includes `SKILL.md` definition and `cost_audit.py` implementation.

## Inter-module dependencies

- **Consumed by audit** — The `cai-audit-cost-reduction` agent
  (`audit` module) depends on the `cost_query` skill defined in this
  module (per-issue lookups are routed via the `issue_number` parameter).
  The agent frontmatter declares the skill in its `tools:` line.
- **Loaded by Claude Code harness** — The harness automatically discovers
  plugins under `.claude/plugins/` at startup; each plugin's manifest
  is read and skills are registered before any agent invocation.

## Operational notes

- **Skill invocation.** Agents invoke plugin skills via the `Skill` tool,
  passing the skill name and JSON-serialized arguments. The harness
  routes the call to the skill's implementation.
- **Non-user-invocable.** Most plugin skills are marked
  `user-invocable: false` in their SKILL.md metadata, meaning they
  cannot be called directly by humans via Claude Code — only by agents
  during headless execution.
- **Implementation isolation.** Each skill's Python code lives in the
  same directory as its SKILL.md definition. Implementation files are
  not directly editable via agent sessions — they are part of the
  plugin package distribution.
- **Cost sensitivity.** Skill implementations that read large cost-log
  files or perform expensive computations should be careful about
  performance; they are invoked synchronously during agent execution
  and can block the agent's progress if slow.
