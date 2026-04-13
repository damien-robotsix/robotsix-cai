# PR Context Dossier
Refs: robotsix/robotsix-cai#524

## Files touched
- `cai.py:8031-8043` — removed `if not has_fix_target:` guard so `has_pending_prs` is checked unconditionally every cycle
- `cai.py:8111` — changed `if not has_fix_target and has_pending_prs:` to `if has_pending_prs:` so drain counter tracks all pending-PR situations
- `cai.py:8126` — updated drain reset comment to "reset when no pending PRs"
- `cai.py:8128` — changed `if has_fix_target:` to `if has_fix_target and not has_pending_prs:` to gate new PRs on drain state
- `cai.py:8138-8142` — added `elif has_fix_target and has_pending_prs:` log message when fix is skipped

## Files read (not touched) that matter
- `cai.py:8025-8165` — `cmd_cycle` main loop body; understood full control flow

## Key symbols
- `has_pending_prs` (`cai.py:8031`) — boolean flag, now set unconditionally each iteration
- `drain_only_passes` (`cai.py:8018`) — counter, now increments whenever pending PRs exist (not just when no fix target)
- `_MAX_DRAIN_ONLY_PASSES` (`cai.py:8019`) — unchanged limit of 3

## Design decisions
- Made `has_pending_prs` unconditional rather than moving the entire block — minimal change, existing structure preserved
- Updated drain counter to trigger on any `has_pending_prs=True` (not just when no fix target) — ensures counter tracks all stuck-PR situations
- Avoided referencing `len(pending)` in log message since `pending` may not be defined if exception was raised
- Rejected: restructuring the loop body order (Plan 2 approach) — unnecessary complexity

## Out of scope / known gaps
- `_select_fix_target()` unchanged — already excludes `:pr-open` issues from candidates
- `_drain_pending_prs()` unchanged — works correctly
- `has_spike` and `has_exploration` checks still gated on `not has_fix_target` — not related to this issue
- Loop exit condition at line 8106 unchanged — still correctly exits when nothing to do

## Invariants this change relies on
- One extra `gh issue list` call per cycle is acceptable overhead
- `_drain_pending_prs()` is idempotent if there are no pending PRs to drain
- `_MAX_DRAIN_ONLY_PASSES=3` guard prevents infinite loops when PRs get stuck
