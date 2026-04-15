# PR Context Dossier
Refs: damien-robotsix/robotsix-cai#625

## Files touched
- `publish.py:124-140` — Added `audit:raised`, `audit:needs-human`, `audit:solved` to `LABELS_TO_DELETE`; removed those three entries from `AUDIT_LABELS`; changed `namespace=="audit"` label emission to `auto-improve,auto-improve:raised,audit,category:*`
- `cai_lib/config.py:63-64` — Deleted `LABEL_AUDIT_RAISED` and `LABEL_AUDIT_NEEDS_HUMAN` constants
- `cai_lib/__init__.py:40-41,103` — Removed `LABEL_AUDIT_RAISED`/`LABEL_AUDIT_NEEDS_HUMAN` imports and `__all__` entries; added `_migrate_audit_raised_labels` export
- `cai_lib/watchdog.py:16-28` — Removed `LABEL_AUDIT_RAISED` import; simplified in-progress rollback to always use `LABEL_REFINED`; added `_migrate_audit_raised_labels` migration helper
- `cai.py:531,554` — Removed `LABEL_AUDIT_RAISED` conditionals in `_recover_stale_pr_open`; now always uses `LABEL_RAISED`/`LABEL_REFINED`
- `cai.py:696` — Removed `LABEL_AUDIT_RAISED` from verify recovery remove-list
- `cai.py:206` — Added `_migrate_audit_raised_labels` to import from `cai_lib.watchdog`
- `cai.py:2690-2698` — Added Phase 0 migration call in `_cmd_cycle_inner`
- `cai.py:1208-1509` — Rewired `cmd_audit_triage` to query `auto-improve:raised + audit` labels; updated escalate/passthrough logic; added deprecation TODO
- `entrypoint.sh:19` — Updated audit-triage comment to reflect unified label scheme
- `.cai-staging/agents/cai-audit-triage.md` — Added deprecation notice; updated instructions to reference `auto-improve:raised + audit`

## Files read (not touched) that matter
- `cai_lib/github.py` — Confirms `_set_labels` accepts `list[str]` (can use string literals for migration)
- `tests/test_rollback.py` — Verified no `LABEL_AUDIT_RAISED` references; tests unaffected
- `tests/test_fsm.py` — Verified no `LABEL_AUDIT_RAISED` references; tests unaffected

## Key symbols
- `_migrate_audit_raised_labels` (`cai_lib/watchdog.py`) — New idempotent migration helper; relabels open `audit:raised` issues to `auto-improve:raised + audit`
- `cmd_audit_triage` (`cai.py:~1208`) — Transitional: now queries `LABEL_RAISED + "audit"` AND filter; escalate uses `LABEL_HUMAN_NEEDED`
- `AUDIT_LABELS` (`publish.py:~138`) — Now only contains `("audit", ...)` source-tag entry; state labels removed

## Design decisions
- Migration helper placed in `watchdog.py` (not a new `cmd_lifecycle.py`) — that's where `_rollback_stale_in_progress` lives; no need for a new file
- Passthrough action now a no-op label-wise — issues already carry `auto-improve:raised` so the refine subagent picks them up naturally
- Escalate now uses `LABEL_HUMAN_NEEDED` (= `auto-improve:human-needed`) — unified FSM state; removed `audit:needs-human`
- Rejected: deleting `cmd_audit_triage` — `cmd_triage` is not yet merged; function kept as interim wrapper per scope guardrails

## Out of scope / known gaps
- `cmd_triage` (unified triage function) is not yet in the codebase; TODO comment marks where to remove `cmd_audit_triage` once it lands
- `check-workflows` pipeline untouched (Step 5 of #621)
- `cai-audit.md` agent logic unchanged — only the triage/dispatch layer changed

## Revision 1 (2026-04-15)

### Rebase
- clean

### Files touched this revision
- `.github/workflows/admin-only-label.yml:22` — Removed `"audit:raised"` from restricted labels `contains()` guard; label is retired by this PR so the check was dead code

### Decisions this revision
- Removed `audit:raised` from workflow guard — label is added to `LABELS_TO_DELETE` in publish.py; once `ensure_all_labels()` runs it will no longer exist, making the condition permanently false

### New gaps / deferred
- None

## Revision 2 (2026-04-15)

### Rebase
- clean

### Files touched this revision
- `.cai-staging/agents/cai-audit-triage.md` — no edit needed; staging file already had correct `auto-improve:raised + audit` description; wrapper copies it to `.claude/agents/cai-audit-triage.md` on exit

### Decisions this revision
- No code edit was made; the staging file written in the original fix session already contained the correct frontmatter description. The wrapper's copy-on-exit mechanism resolves the inconsistency between `.cai-staging/` (correct) and `.claude/agents/` (stale).

### New gaps / deferred
- None

## Invariants this change relies on
- `gh issue list --label A --label B` performs an AND filter (both labels must be present)
- `_set_labels` accepts raw string labels, not just named constants
- The `audit` source label remains on all audit-originated issues after migration
