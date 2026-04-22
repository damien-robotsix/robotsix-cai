# agents-utility

Utility subagents — memory curator, git command runner, cost
optimiser, and weekly external-library scout. These agents are
called from other agents or from `cmd_*` functions when a focused,
small-scope capability is needed.

## Key entry points
- [`.claude/agents/utility/cai-memorize.md`](../../.claude/agents/utility/cai-memorize.md)
  — post-solved memory curator (sonnet). Reads a solved issue +
  merged PR diff and decides whether a cross-cutting design
  decision should land in `.claude/agent-memory/shared/`. Emits
  `NO_MEMORY` when nothing qualifies.
- [`.claude/agents/utility/cai-git.md`](../../.claude/agents/utility/cai-git.md)
  — lightweight haiku subagent that runs git commands on behalf
  of other subagents. Never modifies code.
- [`.claude/agents/utility/cai-cost-optimize.md`](../../.claude/agents/utility/cai-cost-optimize.md)
  — weekly opus cost-reduction proposer. Analyses spending trends
  and proposes one optimisation per run.
- [`.claude/agents/utility/cai-external-scout.md`](../../.claude/agents/utility/cai-external-scout.md)
  — weekly opus scout for mature OSS libraries that could replace
  in-house plumbing. Writes one adoption proposal per run.

## Inter-module dependencies
- Invoked by **actions** — `handle_confirm` launches
  `cai-memorize` on merged PRs; several handlers delegate git
  operations to `cai-git`.
- Invoked by **cli** — `cmd_cost_optimize` (cai-cost-optimize),
  `cmd_external_scout` (cai-external-scout).
- Consumes **docs** — root `CLAUDE.md`; `cai-memorize` also
  writes to `.claude/agent-memory/shared/`.
- Uses **audit** — `cai-cost-optimize` relies on cost helpers
  (`cai_lib/audit/cost.py`) via the findings consumer.
- Uses **agents-config** — permission/hook settings.

## Operational notes
- **Memory guardrails.** `cai-memorize` writes rarely (the
  `NO_MEMORY` default is intentional); a flood of shared memory
  entries would blow up every downstream subagent's token usage.
- **Git agent is pure plumbing.** `cai-git` has only Bash and is
  never asked to reason about code — keep its prompts tight.
- **Cost tiers.** `cai-cost-optimize` and `cai-external-scout` are
  opus weekly (proposal quality matters); their output is graded
  by `cai-propose-review` before a human ever sees it.
- **CI implications.** None — these agents have no dedicated
  tests; behaviour is observed via live runs and on-demand
  `cai-audit-good-practices` sweeps.
