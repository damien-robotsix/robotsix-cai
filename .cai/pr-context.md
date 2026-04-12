# PR Context Dossier
Refs: robotsix-cai#484

## Files touched
- .claude/agents/cai-plan.md:70-75 — Updated Explore guidance to use `Agent(subagent_type="Explore", model="haiku", ...)` with "Do NOT delegate decisions" guard
- .claude/agents/cai-review-pr.md:66-68 — Updated inline Explore mention in "How to work" section to pin haiku
- .claude/agents/cai-review-pr.md:123-128 — Updated efficiency guidance section Explore reference to pin haiku with guard
- .claude/agents/cai-review-docs.md:113-115 — Updated Explore guidance to pin haiku with guard
- .claude/agents/cai-spike.md:65-66 — Updated Explore mention to pin haiku with guard

## Files read (not touched) that matter
- .claude/agents/cai-revise.md — canonical reference for `Agent(subagent_type="Explore", model="haiku", ...)` pattern (lines 254-261)

## Key symbols
- `Agent(subagent_type="Explore", model="haiku", ...)` — the callable syntax used in cai-revise.md that this PR propagates to four more agents

## Design decisions
- Used callable kwarg syntax `model="haiku"` (not YAML-style `subagent_type: Explore`) — matches the established pattern in cai-revise.md
- Added "Do NOT delegate decisions — only reading and search" guard to all updated references — prevents parent agents from accidentally offloading judgment to the haiku Explore subagent
- cai-review-pr.md had TWO Explore references; both were updated

## Out of scope / known gaps
- cai-revise.md already had the haiku pin — not touched
- No infrastructure or cai.py changes needed — prompt-only fix

## Invariants this change relies on
- The Agent tool's `model` parameter is respected by the runtime and overrides the parent agent's model for the subagent invocation
- Haiku Explore preserves 100% retrieval quality for read/search tasks (per spike findings in issue #443)

## Revision 1 (2026-04-12)

### Rebase
- clean

### Files touched this revision
- .claude/agents/cai-review-pr.md:68 — Added "Do NOT delegate decisions — only reading and search." guard to "How to work" item 2 (first Explore reference), matching the guard already present at the second Explore reference in "Agent-specific efficiency guidance"

### Decisions this revision
- Guard added verbatim inline (same wording as efficiency section) — reviewer explicitly requested matching consistency with other agents and with the second reference in the same file

### New gaps / deferred
- None

## Revision 2 (2026-04-12)

### Rebase
- clean

### Files touched this revision
- .claude/agents/cai-revise.md:260 — Changed "Do NOT delegate edits or decisions" to "Do NOT delegate decisions" to match the four updated agents

### Decisions this revision
- Dropped "edits or" from the guard clause in cai-revise.md — reviewer requested consistency with the standardized wording used in the four other agents; "decisions" alone is the canonical form

### New gaps / deferred
- None
