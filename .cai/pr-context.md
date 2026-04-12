# PR Context Dossier
Refs: robotsix-cai/robotsix-cai#444

## Files touched
- `.claude/agents/cai-revise.md`:full file — condensed from 575 to ~246 lines per issue #444

## Files read (not touched) that matter
- `.claude/agents/cai-revise.md` — source file being condensed

## Key symbols
- `cai-revise.md` (`.claude/agents/cai-revise.md`:1) — agent definition file being trimmed

## Design decisions
- Kept all behavioral rules intact; removed only redundant examples and duplicated content
- Removed duplicate `old_string` uniqueness rule from Efficiency guidance (kept only in Hard rules — editing)
- Removed introspection mode subsection (rarely triggered; agent handles via general instruction-following)
- Condensed staging directory section from 42 lines to 7 (one clear rule, no redundant examples)
- Condensed rebase section from 62 lines to ~20 (one-line-per-step, no full Agent() call examples)
- Condensed efficiency guidance from 46 lines to 3 (kept fail-fast, grep-before-read, batch-edits)
- Condensed PR dossier section from 52 lines to ~8 (kept trust-the-clone rule, dropped 3 fallback paths)
- Condensed "Context provided below" from 30 lines to 3 (numbered summary)
- Rejected: removing the dossier update template — kept for structural clarity in revision cycles

## Out of scope / known gaps
- No wrapper changes
- No other agent files touched

## Invariants this change relies on
- All behavioral rules from original file are preserved in condensed form
- Staging directory mechanism for agent self-modification is preserved
- Memory format (one-line entries, collapse at 200 lines) is preserved
