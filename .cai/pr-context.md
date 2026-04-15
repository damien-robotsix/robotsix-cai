# PR Context Dossier
Refs: robotsix-cai/robotsix-cai#650

## Files touched
- .claude/agents/cai-implement.md:57-65 — added hard rule #8 requiring contradiction-check Grep before adding rules/config to prompt or settings files

## Files read (not touched) that matter
- .claude/agents/cai-implement.md — source file; rule 7 already existed (cross-reference check), so the new rule became #8

## Key symbols
- Hard rule 8 (.claude/agents/cai-implement.md:57) — new contradiction-checking rule added to ## Hard rules section

## Design decisions
- Numbered as rule 8 (not 7) — rule 7 already existed for cross-reference checks; inserting as 8 preserves existing rules without renumbering
- Rejected: renumbering existing rules — would be a larger diff and break any external references to rule numbers

## Out of scope / known gaps
- Other agent files not updated — the new rule is self-contained guidance for cai-implement only

## Invariants this change relies on
- The staging path `.cai-staging/agents/cai-implement.md` is picked up by the wrapper and copied to `.claude/agents/cai-implement.md`
