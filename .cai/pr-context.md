# PR Context Dossier
Refs: robotsix-cai/cai#500

## Files touched
- `.claude/agents/cai-review-docs.md`:74-83 — replaced silent bailout with explicit `### Finding: stale_docs` blocks for missing/empty docs directory

## Files read (not touched) that matter
- `.claude/agents/cai-review-docs.md` — the only file changed; lines 74–83 replaced

## Key symbols
- `### Finding: stale_docs` (cai-review-docs.md:88) — output format the wrapper detects to mark a PR as having documentation findings

## Design decisions
- Split the old single "does not exist or is empty" condition into two separate cases (missing vs. empty) with distinct finding descriptions for clarity
- Kept the "No documentation updates needed." output valid only when docs exist, contain `.md` files, AND no user-facing changes are present
- Rejected: keeping the conditional user-facing-changes check for the missing/empty case — the issue asks to always emit a finding

## Out of scope / known gaps
- No changes to `cai.py` — `cmd_review_docs` already detects `### Finding:` in agent output

## Invariants this change relies on
- `cmd_review_docs` in `cai.py` (~line 6387) already checks for `### Finding:` in agent output to mark a review as having findings
