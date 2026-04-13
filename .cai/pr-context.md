# PR Context Dossier
Refs: robotsix-cai/cai#493

## Files touched
- .claude/agents/cai-review-docs.md:74-80 — replaced unconditional `No documentation updates needed.` bailout with conditional: no user-facing changes → silent pass; user-facing changes present → emit `### Finding: stale_docs` referencing the existing output format section

## Files read (not touched) that matter
- .claude/agents/cai-review-docs.md — the agent definition being modified; lines 74-75 had the unconditional bailout; lines 81-90 have the existing finding template

## Key symbols
- `### Finding: stale_docs` (.claude/agents/cai-review-docs.md:82) — existing output format template the new conditional references rather than duplicating

## Design decisions
- Cross-reference existing "Output format" section instead of duplicating the finding template inline — keeps the file DRY and avoids a markdown heading collision
- Rejected: Plan 1's approach of embedding the full finding template at lines 74-75 — duplicates the template already at lines 81-90 and introduces a raw `### Finding:` heading in the middle of the "How to work" section

## Out of scope / known gaps
- Did not touch `cai.py` (`cmd_review_docs`) — the Python wrapper just invokes the agent; logic lives in the agent definition
- Did not restructure or add sections to the agent file

## Invariants this change relies on
- The "Output format" section (now at lines 82-95 after the 5-line insertion) continues to define the `### Finding: stale_docs` template that the new conditional references
- The "Changes that do NOT warrant documentation review" section at lines 59-64 remains the authoritative list the new conditional cross-references
