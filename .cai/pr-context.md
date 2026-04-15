# PR Context Dossier
Refs: robotsix/robotsix-cai#664

## Files touched
- docs/architecture.md:32 — added one-line note about multi-step sequential processing after the terminal states description

## Files read (not touched) that matter
- cai_lib/dispatcher.py — already contained full implementation: `_SUB_ISSUE_TITLE_RE`, `_parse_sub_issue_step`, `open_sub_steps` set, and filtering logic in `_pick_oldest_actionable_target`
- tests/test_dispatcher.py — already contained `TestSubIssueStepOrderingGate` with all three required test cases

## Key symbols
- `_parse_sub_issue_step` (cai_lib/dispatcher.py:34) — extracts (parent_num, step) from `[#N Step X/Y]` titles
- `open_sub_steps` (cai_lib/dispatcher.py:307) — set built from all open issue titles for O(1) predecessor lookup
- `TestSubIssueStepOrderingGate` (tests/test_dispatcher.py:594) — covers the three required test cases

## Design decisions
- Only added the missing docs line; code and tests were already fully implemented in the repo

## Out of scope / known gaps
- No FSM changes; filter-only approach as specified

## Invariants this change relies on
- The docs line placement assumes the architecture.md dispatcher section is between the PR handler table and Lifecycle Labels section
