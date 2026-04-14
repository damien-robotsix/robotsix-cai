# PR Context Dossier
Refs: damien-robotsix/robotsix-cai#590

## Files touched
- `cai.py:1450` — changed sub-issue title format from `[Step X/Y]` to `[#parent_number Step X/Y]`

## Files read (not touched) that matter
- `cai.py` — contains `_create_sub_issues` function where the change was made

## Key symbols
- `_create_sub_issues` (`cai.py:~1440`) — function that creates GitHub sub-issues for multi-step decomposition
- `parent_number` (`cai.py:~1447`) — already in scope at line 1450, used in body template

## Design decisions
- Added `#{parent_number}` at the leading position in the title bracket: `[#123 Step 1/3]`
- Rejected: Unicode middle-dot separator — unnecessary complexity, deviates from spec

## Out of scope / known gaps
- Body template already links correctly via `_Sub-issue of #{parent_number}` — not changed
- `_find_sub_issue`, `_update_parent_checklist`, checklist/label logic — not touched
- `<!-- parent: #N -->` HTML comment markers — not touched (relied on by `_find_sub_issue` for deduplication)

## Invariants this change relies on
- `parent_number` variable is in scope at line 1450 (confirmed by reading the function)
