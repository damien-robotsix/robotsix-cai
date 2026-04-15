# PR Context Dossier
Refs: robotsix-cai/robotsix-cai#649

## Files touched
- .claude/agents/cai-implement.md:54 — Added new hard rule 7 requiring cross-reference Grep before exiting with a non-zero diff

## Files read (not touched) that matter
- .claude/agents/cai-implement.md — The agent definition being modified; read to verify current rule numbering and insertion point

## Key symbols
- Hard rule 7 (.claude/agents/cai-implement.md:54) — New rule mandating cross-file reference checks for renamed/added symbols before PR exit

## Design decisions
- Inserted as rule 7 immediately after rule 6 (Stay inside the repo) — preserves logical grouping of hard constraints before efficiency guidance
- Anchored scope with "within the minimal-change scope" phrase — prevents rule from becoming a license for unbounded edits

## Out of scope / known gaps
- Did not add corresponding guidance to cai-revise.md or other agents — only cai-implement was identified in the issue

## Invariants this change relies on
- The staging path `.cai-staging/agents/` is copied over `.claude/agents/` by the wrapper after the session exits
