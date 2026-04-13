# PR Context Dossier
Refs: damien-robotsix/robotsix-cai#498

## Files touched
- `publish.py:89` — Added `auto-improve:parent` label to LABELS list
- `publish.py:102` — Added `audit:needs-human` label to AUDIT_LABELS list
- `publish.py:279` — Added new `ensure_all_labels()` function that creates labels from all namespaces with deduplication
- `cai.py:147` — Added `from publish import ensure_all_labels` import
- `cai.py:8574` — Added `ensure_all_labels()` call in `main()` after auth checks, before handler dispatch

## Files read (not touched) that matter
- `publish.py` — Contains LABELS, AUDIT_LABELS, CODE_AUDIT_LABELS, UPDATE_CHECK_LABELS constants and existing `ensure_labels()` pattern
- `cai.py` — Entry point with `main()` function; auth checks at lines 8565-8571

## Key symbols
- `ensure_all_labels` (`publish.py:279`) — New function; iterates all 4 label sets with deduplication, calls `gh label create` for each unique label
- `ensure_labels` (`publish.py:262`) — Existing per-namespace function; `ensure_all_labels` follows same `check=False` pattern
- `LABELS`, `AUDIT_LABELS`, `CODE_AUDIT_LABELS`, `UPDATE_CHECK_LABELS` (`publish.py:77-132`) — All 4 label set constants iterated by `ensure_all_labels`

## Design decisions
- Called unconditionally in `main()` before handler dispatch — ensures all labels exist regardless of subcommand
- `seen` set deduplicates labels that appear in multiple sets (e.g. `auto-improve`, `auto-improve:raised` appear in LABELS, CODE_AUDIT_LABELS, UPDATE_CHECK_LABELS)
- Rejected: calling `ensure_all_labels()` lazily in each handler — would miss handlers and add complexity

## Out of scope / known gaps
- Performance: adds ~30 `gh label create` calls per invocation; acceptable for background cron jobs
- No TTL cache to skip the check — can be added later if latency becomes a concern

## Invariants this change relies on
- `publish.py` does not import from `cai.py` (no circular dependency)
- `gh label create` returns non-zero when label already exists; `check=False` handles this correctly

## Revision 1 (2026-04-12)

### Rebase
- clean

### Files touched this revision
- `README.md:179` — Added `audit:needs-human` row to audit label table

### Decisions this revision
- Added `audit:needs-human` to README label table to match the new label added in `publish.py:AUDIT_LABELS` — label was already referenced in cai-audit-triage.md but missing from README

### New gaps / deferred
- None

## Revision 2 (2026-04-12)

### Rebase
- clean

### Files touched this revision
- `README.md:164` — Added paragraph documenting `auto-improve:parent` label behavior and sub-issue creation after "Filing issues with multi-step plans" section

### Decisions this revision
- Added user-facing description of what happens when a `### Plan` issue is accepted by the refine subagent: parent gets `auto-improve:parent` label, sub-issues created, checklist added

### New gaps / deferred
- None

## Revision 3 (2026-04-13)

### Rebase
- clean

### Files touched this revision
- `README.md:165-170` — Corrected description of sub-issue creation: it happens on `## Multi-Step Decomposition` output (refine detects multi-step), not when a `### Plan` section already exists (which causes refine to skip refinement); added note that pre-existing `### Plan` issues skip refinement and sub-issues are not created

### Decisions this revision
- Adopted reviewer's suggested text verbatim — accurately describes actual cai-refine.md behavior (early exit on structured headings vs. Multi-Step Decomposition output)

### New gaps / deferred
- None
