# PR Context Dossier
Refs: robotsix-cai/robotsix-cai#592

## Files touched
- cai_lib/config.py:68 — Added 3 new label constants: LABEL_IN_PR, LABEL_HUMAN_NEEDED, LABEL_PR_HUMAN_NEEDED
- cai_lib/fsm.py — New file: IssueState (10-state), PRState (6-state), Transition dataclass, ISSUE_TRANSITIONS (12), PR_TRANSITIONS (7), get_issue_state, get_pr_state, render_fsm_mermaid
- cai_lib/__init__.py:48 — Added 3 new config constants to import block; added fsm import block after cmd_implement import; extended __all__
- tests/test_fsm.py — New file: 5 unit tests for the FSM module

## Files read (not touched) that matter
- cai_lib/__init__.py — Source of truth for import structure and __all__ ordering

## Key symbols
- `IssueState` (cai_lib/fsm.py:21) — 10-state enum replacing the old PR_OPEN+REVISING pair with a single PR state
- `PRState` (cai_lib/fsm.py:34) — 6-state PR submachine enum using "pr:*" descriptor strings (not GitHub labels)
- `Transition` (cai_lib/fsm.py:43) — dataclass representing a labelled FSM edge with confidence gate
- `ISSUE_TRANSITIONS` (cai_lib/fsm.py:53) — 12 transitions covering the full issue lifecycle
- `PR_TRANSITIONS` (cai_lib/fsm.py:79) — 7 transitions for the PR submachine
- `LABEL_IN_PR` (cai_lib/config.py:69) — "auto-improve:in-pr", the label backing IssueState.PR

## Design decisions
- Single IssueState.PR instead of PR_OPEN+REVISING — per project owner revision; PR-internal states belong to PRState only
- PRState uses "pr:*" descriptor strings, not GitHub labels — no new labels needed; pure data model
- LABEL_PR_OPEN, LABEL_REVISING remain in config.py untouched — still used by cai.py's _set_labels calls
- typing.Optional used instead of X | None union — avoids Python 3.9 syntax issues in older containers
- Rejected: adding transitions PR_OPEN→REVISING and REVISING→PR_OPEN to ISSUE_TRANSITIONS — project owner moved these to PRState submachine exclusively

## Out of scope / known gaps
- No behaviour change in cai.py — FSM is pure data model; wiring ISSUE_TRANSITIONS into _set_labels is a future step
- No Python FSM library adopted — dataclasses sufficient for step 1; library adoption deferred
- LABEL_NO_ACTION, LABEL_NEEDS_SPIKE not in IssueState — backward-compatible constants stay in config.py

## Invariants this change relies on
- LABEL_IN_PROGRESS already exists in config.py (for IssueState.IN_PROGRESS enum value)
- LABEL_NEEDS_EXPLORATION already exists in config.py (for IssueState.NEEDS_EXPLORATION enum value)
- All LABEL_* constants imported by fsm.py must exist in config.py before fsm.py is imported
