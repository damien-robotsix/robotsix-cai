# PR Context Dossier
Refs: robotsix/robotsix-cai#312

## Files touched
- .claude/agents/cai-fix.md:315 — inserted new `## Multi-step plans` section (22 lines) between `## When to make changes` and `## Raising complementary issues`

## Files read (not touched) that matter
- .claude/agents/cai-fix.md — source file; read to determine exact insertion point at line 314/315 boundary

## Key symbols
- `## Multi-step plans` (.claude/agents/cai-fix.md:315) — new section added; guides fix agent to execute numbered plan steps sequentially with per-step Read/Grep verification

## Design decisions
- Inserted via staging path `.cai-staging/agents/cai-fix.md` — direct writes to `.claude/agents/` are blocked by claude-code sensitive-file protection
- Placed between `## When to make changes` and `## Raising complementary issues` — logically follows the "when to act" guidance and precedes the "side effects" guidance

## Out of scope / known gaps
- No frontmatter changes (tools, model, memory fields unchanged)
- No changes to any other agent definitions or wrapper code

## Invariants this change relies on
- The wrapper copies `.cai-staging/agents/*.md` to `.claude/agents/` after a successful exit
- The new section only activates when the issue body contains a `### Plan` section; issues without one are unaffected
