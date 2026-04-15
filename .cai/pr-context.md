# PR Context Dossier
Refs: robotsix/robotsix-cai#628

## Files touched
- `cai.py`:187 — added `_ALL_MANAGED_ISSUE_LABELS` frozenset and `_MANAGED_ISSUE_PREFIXES` tuple constants after `_STALE_NO_ACTION_DAYS` import block
- `cai.py`:653 — added `_issue_label_sweep()` function before `cmd_verify()`
- `cai.py`:703 — wired `_issue_label_sweep()` call into `cmd_verify()` before `try:` block
- `cai.py`:2819 — wired `_issue_label_sweep()` call into `_cmd_cycle_inner()` as Phase 0.5

## Files read (not touched) that matter
- `cai.py`:186 — wildcard import `from cai_lib.config import *` brings all `LABEL_*` constants into scope before the new frozenset

## Key symbols
- `_ALL_MANAGED_ISSUE_LABELS` (`cai.py`:196) — frozenset of all valid cai-managed issue labels; stale detection compares against this
- `_MANAGED_ISSUE_PREFIXES` (`cai.py`:208) — tuple of prefix strings identifying cai-owned labels; no trailing colons
- `_issue_label_sweep` (`cai.py`:653) — main sweep function; returns (issues_scanned, labels_removed)
- `_set_labels` (imported from `cai_lib.github`) — used to remove stale labels; already available

## Design decisions
- Used `lbl == p or lbl.startswith(p + ":")` matching — avoids false positives (e.g., `"kind"` prefix won't match `"kindness"`)
- Three separate `gh issue list` calls (one per base label) with dedup via `seen` dict — simpler than a combined query
- `_issue_label_sweep()` returns counts tuple — allows callers to log or react to results
- Rejected: modifying `cai_lib/config.py` or `publish.py` — scope guardrails explicitly forbid it

## Revision 1 (2026-04-15)

### Rebase
- clean

### Files touched this revision
- `publish.py`:340 — added `CHECK_WORKFLOWS_LABELS` to `ensure_all_labels()` label-set tuple

### Decisions this revision
- Reviewer requested publish.py change; scope guardrail was about not sourcing accepted-label set from publish.py, not an absolute prohibition on touching it — pre-existing omission of CHECK_WORKFLOWS_LABELS from ensure_all_labels() is a legitimate co-change

### New gaps / deferred
- None

## Revision 2 (2026-04-15)

### Rebase
- clean

### Files touched this revision
- `cai.py`:205 — added `"check-workflows:raised"` to `_ALL_MANAGED_ISSUE_LABELS` frozenset

### Decisions this revision
- `"check-workflows:raised"` is created by `ensure_all_labels()` (via `CHECK_WORKFLOWS_LABELS` in publish.py) but had no corresponding `LABEL_*` constant in `cai_lib/config.py`, so the string literal was added directly to the frozenset alongside the three base labels on line 205

### New gaps / deferred
- `category:workflow_*` labels from `CHECK_WORKFLOWS_LABELS` are not swept (prefix `"category"` is not in `_MANAGED_ISSUE_PREFIXES`) — intentional, same as existing `LABEL_KIND_*` pattern

## Out of scope / known gaps
- PR objects are NOT swept — `_issue_label_sweep` only calls `gh issue list`; PR label lifecycle remains separate
- `LABEL_PR_*` labels (pr:reviewing-code etc.) are absent from `_ALL_MANAGED_ISSUE_LABELS` intentionally — they are valid on PRs but stale if found on issues, and `"pr"` prefix in `_MANAGED_ISSUE_PREFIXES` handles removal

## Invariants this change relies on
- `from cai_lib.config import *` (line 186) precedes the `_ALL_MANAGED_ISSUE_LABELS` frozenset definition — all `LABEL_*` names must be in scope at module parse time
- `_set_labels` signature accepts `remove=` kwarg and `log_prefix=` kwarg — no changes made to it
