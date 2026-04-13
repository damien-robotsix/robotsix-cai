# PR Context Dossier
Refs: robotsix/robotsix-cai#461

## Files touched
- `.claude/agents/cai-review-pr.md`:4 — removed `Agent` from `tools:` frontmatter
- `.claude/agents/cai-review-pr.md`:15 — removed "and the `Agent` tool" from body description
- `.claude/agents/cai-review-pr.md`:65-68 — removed "use the Agent tool with `subagent_type: Explore`" from step 2
- `.claude/agents/cai-review-pr.md`:141-145 — deleted entire item 5 "Use Agent for broad exploration" from Efficiency guidance
- `cai.py`:5754 — updated comment to remove `/Agent` from tool allowlist description
- `cai.py`:5779-5781 — added `--max-budget-usd 0.50` to the `claude -p` invocation

## Files read (not touched) that matter
- `.claude/agents/cai-review-pr.md` — source of all agent changes (via staging)

## Key symbols
- `cmd_review_pr` (`cai.py`:5664) — function that invokes the cai-review-pr agent
- `_run_claude_p` (`cai.py`:5778) — call site where `--max-budget-usd` was added

## Design decisions
- Budget cap set to $0.50 (matching the plan's recommendation) — below the observed worst-case $0.78 but above the median, giving a meaningful cap without cutting off normal runs
- `Agent` tool removed entirely rather than rate-limited — nested sub-sessions are the root cause of high turn counts, not just usage frequency
- Items 1–4 of efficiency guidance retained — Grep/Glob/batch-Read patterns are still valid and encourage efficient direct tool use

## Out of scope / known gaps
- Did not change review categories or SHA-idempotency logic

## Revision 1 (2026-04-12)

### Rebase
- clean

### Files touched this revision
- `.claude/agents/cai-review-docs.md`:4 — removed `Agent` from `tools:` frontmatter
- `.claude/agents/cai-review-docs.md`:14 — removed "and the `Agent` tool" from body description
- `.claude/agents/cai-review-docs.md`:117-119 — replaced "Use Agent for broad exploration" item 3 with "Batch independent Grep calls" item 3
- `cai.py`:5987-5989 — added `--max-budget-usd 0.50` to cai-review-docs invocation

### Decisions this revision
- Applied identical Agent-removal + budget-cap fix to cai-review-docs — same root cause (nested sub-agent turns) as cai-review-pr; reviewer's finding was valid

### New gaps / deferred
- None

## Revision 2 (2026-04-13)

### Rebase
- resolved: `.claude/agents/cai-review-pr.md` (conflict 1), `.claude/agents/cai-review-docs.md` (conflict 2, second commit)

### Files touched this revision
- `.claude/agents/cai-review-pr.md` — rebase conflict resolved: took PR's version (no Agent, 4 Grep/Read/Glob guidance items) over HEAD's haiku-Explore version
- `.claude/agents/cai-review-docs.md` — rebase conflict resolved: took HEAD's version (Agent+Edit+Write, direct-fix workflow) over PR's read-only version

### Decisions this revision
- cai-review-docs conflict resolved in favor of HEAD: main has evolved this agent to have direct-fix capability (Edit/Write tools + Agent for exploration); taking the PR's read-only version would have regressed that functionality. Original scope guardrail ("Do NOT modify cai-review-docs") also supports this choice.
- Reviewer comment (@damien-robotsix) asked whether removing Agent increases token cost since Explore runs on haiku. No code change made: cai-review-pr itself runs on `model: claude-haiku-4-5`, so spawning a haiku child Explore session saves no tokens vs having the haiku parent do direct Grep/Glob calls — it only adds sub-session overhead (context initialization, turn counts). The haiku-delegation pattern is designed for opus-class parents where the ≈10× model cost difference justifies the sub-session overhead; it does not apply when the parent is already haiku.

### New gaps / deferred
- Reviewer comment (@damien-robotsix) is a clarification question, not a code-change request — no action taken beyond explanation above.

## Invariants this change relies on
- `--max-budget-usd` is a valid Claude CLI flag supported by the current claude-code version
- Removing `Agent` from tools list still leaves `Read`, `Grep`, `Glob` sufficient for ripple-effect checking
