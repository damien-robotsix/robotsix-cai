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

## Revision 1 (2026-04-12)

### Rebase
- clean

### Files touched this revision
- .claude/agents/cai-fix.md:4 — added `TodoWrite` to `tools:` frontmatter field

### Decisions this revision
- Added TodoWrite to tools list — reviewer correctly identified the new ## Multi-step plans section references TodoWrite in two places but the tool was absent from the frontmatter; the original issue author's claim that it was available "via the shared deferred-tool set" was incorrect for declarative subagents

### New gaps / deferred
- None

## Revision 2 (2026-04-12)

### Rebase
- clean

### Files touched this revision
- README.md:136 — added new "### Filing issues with multi-step plans" section between the issue lifecycle `:no-action` paragraph and the "### Audit findings" heading

### Decisions this revision
- Added user-facing documentation for the `### Plan` / `### Verification` issue format per reviewer finding; placed in the issue lifecycle section since that's where users learn how issues flow through the system

### New gaps / deferred
- None
