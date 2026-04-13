# Agents

Agents are defined in `.claude/agents/*.md` with YAML frontmatter (`name`, `description`, `tools`, `model`). The `cai.py` wrapper selects the appropriate agent for each pipeline phase and passes context via the prompt.

| Agent | Description | Tools | Model | Mode |
|---|---|---|---|---|
| `cai-analyze` | Analyze parsed transcript signals and raise auto-improve findings | Read, Grep, Glob, Skill | sonnet | Read-only |
| `cai-audit` | Audit issue queue and PRs for lifecycle state-machine inconsistencies | Read, Grep, Glob | sonnet | Read-only |
| `cai-audit-triage` | Triage `audit:raised` findings and emit close/passthrough/escalate verdicts | Read | sonnet | Inline-only |
| `cai-code-audit` | Read-only source tree audit for inconsistencies, dead code, and missing cross-file references | Read, Grep, Glob | sonnet | Read-only |
| `cai-confirm` | Verify each `auto-improve:merged` issue is actually resolved | Read, Grep, Glob | sonnet | Read-only |
| `cai-cost-optimize` | Weekly cost-reduction agent — analyzes spending trends, proposes one optimization | Read, Grep, Glob | sonnet | Read-only |
| `cai-explore` | Autonomous exploration and benchmarking of `:needs-exploration` issues | Read, Grep, Glob, Bash, Agent, Write, Edit | opus | Worktree |
| `cai-fix` | Autonomous code-editing subagent — makes the smallest targeted change for an issue | Read, Edit, Write, Grep, Glob, TodoWrite | sonnet | Worktree |
| `cai-git` | Lightweight subagent that executes git operations on behalf of other agents | Bash | haiku | Git ops |
| `cai-merge` | Assess whether a PR correctly implements its linked issue and emit a merge verdict | Read | opus | Inline-only |
| `cai-plan` | Generate a detailed fix plan for an issue (first of two serial planners) | Read, Grep, Glob, Agent | opus | Read-only |
| `cai-propose` | Weekly creative agent that proposes ambitious improvements | Read, Grep, Glob | sonnet | Read-only |
| `cai-propose-review` | Evaluate creative proposals for feasibility and value before filing issues | Read, Grep, Glob | sonnet | Read-only |
| `cai-rebase` | Lightweight rebase conflict resolution for PRs with no unaddressed review comments | Read, Edit, Write, Grep, Glob, Agent | haiku | Worktree |
| `cai-refine` | Rewrite human-filed issues into structured plans with steps, verification, and scope guardrails | Read, Grep, Glob | sonnet | Read-only |
| `cai-review-docs` | Pre-merge documentation review — checks whether PR changes require `/docs` updates | Read, Grep, Glob, Agent | haiku | Read-only |
| `cai-review-pr` | Pre-merge ripple-effect review — finds inconsistencies the PR introduced but didn't update | Read, Grep, Glob, Agent | haiku | Read-only |
| `cai-revise` | Handle PR review comments: resolve rebase conflicts AND address unaddressed reviewer comments | Read, Edit, Write, Grep, Glob, Agent | sonnet | Worktree |
| `cai-select` | Evaluate two fix plans and select the better one | Read | opus | Inline-only |
| `cai-spike` | Research/verification agent for `:needs-spike` issues — produces Findings, Refined Issue, or Blocked output | Read, Grep, Glob, Bash, Agent | opus | Worktree |
| `cai-update-check` | Periodic Claude Code release checker — emits findings for new versions, deprecations, and best-practice changes | Read, Grep, Glob | sonnet | Read-only |

**Inline-only** agents receive all context in the user message and require no file access. **Worktree** agents run in a fresh git clone and can edit files; the `cai.py` wrapper handles commit, push, and PR creation. **Read-only** agents read the repo or external data without making any changes.
