# PR Context Dossier
Refs: robotsix-cai/robotsix-cai#538

## Files touched
- .claude/agents/cai-select.md:57-79 — replaced `## Output format` section to emit only raw plan text, removing `## Selection`, `### Chosen plan`, `### Reasoning`, and `### Plan to implement` wrapper

## Files read (not touched) that matter
- .claude/agents/cai-select.md — the only file changed; lines 1-56 unchanged

## Key symbols
- `## Output format` (.claude/agents/cai-select.md:57) — section replaced entirely

## Design decisions
- Output bare plan text with no wrapper headings — cleanest result after wrapper prepends `## Selected Implementation Plan\n\n`
- Added optional `> **Note:** …` blockquote escape hatch for edge-case commentary
- Rejected: keeping a `## Selected Plan` heading — unnecessary nesting noise

## Out of scope / known gaps
- `cai.py` untouched — wrapper's `_extract_stored_plan` and `selection.strip()` guard both work correctly with bare plan output
- `cai-implement.md` untouched — it looks for `## Selected Implementation Plan` injected by the wrapper, not the select agent's output

## Invariants this change relies on
- The wrapper prepends `## Selected Implementation Plan\n\n` before storing; `_extract_stored_plan` strips exactly that heading — bare plan output starts with plan's own `## Plan` heading after stripping
