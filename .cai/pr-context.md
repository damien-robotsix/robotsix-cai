# PR Context Dossier
Refs: robotsix/robotsix-cai#412

## Files touched
- .claude/agents/cai-fix.md:206 — changed "by reading only the issue body" → "without opening any code files"
- .claude/agents/cai-fix.md:407-410 — inserted new paragraph documenting the `## Selected Implementation Plan` user-message section

## Files read (not touched) that matter
- .claude/agents/cai-fix.md — the agent definition being updated

## Key symbols
- `## Selected Implementation Plan` (cai.py:1641) — user-message section injected by `cmd_fix` when `selected_plan` is truthy; now documented in the agent prompt

## Design decisions
- Inserted the Selected Implementation Plan paragraph between the intro sentence and the `## Previous Fix Attempts` paragraph — keeps related "user-message sections" documentation together
- Rejected: placing the new paragraph after `## Previous Fix Attempts` — the plan precedes the issue block in the message, so documenting it first is more natural

## Out of scope / known gaps
- Did not change cai.py or any other file — this is purely a documentation/prompt update

## Invariants this change relies on
- The wrapper always injects `## Selected Implementation Plan` before `## Issue` when a plan exists (cai.py:1641–1652)
