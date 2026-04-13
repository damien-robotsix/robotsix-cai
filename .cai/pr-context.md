# PR Context Dossier
Refs: damien-robotsix/robotsix-cai#544

## Files touched
- `cai_lib/cmd_implement.py`:new — renamed from cmd_fix.py with updated docstring
- `cai_lib/cmd_fix.py`:1 — replaced with deprecated shim (imports from cmd_implement)
- `cai_lib/__init__.py`:79,84,108,112 — updated imports/exports to use cmd_implement and _build_implement_user_message
- `cai_lib/github.py`:82,171-181 — log_prefix default and _build_implement_user_message rename
- `cai_lib/config.py`:14,43 — comment references updated
- `cai_lib/subprocess_utils.py`:38 — docstring example updated
- `cai.py`:all — 8 bulk replace_all passes + targeted edits; subparser "fix"→"implement", handlers dict, _BOT_COMMENT_MARKERS compat entry added
- `publish.py`:87-93 — label descriptions updated
- `docs/architecture.md`:all — cai fix/cai-fix/fix subagent references updated
- `docs/agents.md`:14,25,29 — cai-fix references updated
- `docs/cli.md`:71-77 — fix section renamed to implement
- `README.md`:multiple — all fix subagent/cai fix/cai-fix references updated
- `.github/workflows/admin-only-label.yml`:7,46 — cai fix subagent → cai implement subagent
- `.github/workflows/cleanup-pr-context.yml`:5 — cai-fix → cai-implement
- `entrypoint.sh`:78 — fix subagent → implement subagent

## Files read (not touched) that matter
- `cai_lib/logging_utils.py` — log_run format determines what cai-audit.md patterns should look for

## Key symbols
- `cmd_implement` (`cai.py`:1919) — renamed from cmd_fix; main entrypoint for the implement subagent
- `_build_implement_user_message` (`cai_lib/github.py`:171) — renamed from _build_fix_user_message
- `_BOT_COMMENT_MARKERS` (`cai.py`:2450) — kept "## Fix subagent:" for backward compat with existing PR comments
- `cai_lib/cmd_fix.py` — kept as deprecated shim (no git delete without Bash)

## Design decisions
- Hard-cut CLI verb: `cai implement` replaces `cai fix`; no deprecated alias since only internal callers exist
- Backward compat: `_BOT_COMMENT_MARKERS` retains `"## Fix subagent:"` entry so old bot comments on open PRs still match
- `cmd_fix.py` kept as shim rather than deleted (no Bash available for git rm)
- `cai-fix.md` replaced via staging with deprecated stub; `cai-implement.md` staged as new agent definition
- Analytics key break: `log_run("implement", ...)` replaces `log_run("fix", ...)`; historical rows keyed "fix" become orphaned — accepted
- `return slug or "fix"` on line 521 intentionally unchanged — fallback branch slug unrelated to subagent naming
- Sub-categories `fix.plan`, `fix.select`, `fix.pre-screen` → `implement.plan`, `implement.select`, `implement.pre-screen` for consistency

## Out of scope / known gaps
- `failed_fix_issues` local variable and `_select_fix_target` function in cai.py not renamed (internal implementation, not user-facing)
- No CHANGELOG.md (file doesn't exist in repo)
- `.claude/agent-memory/cai-fix/` directory doesn't exist in repo (confirmed via Glob); no rename needed

## Invariants this change relies on
- `cai implement` is only invoked internally by `cycle` and the handlers dict — both updated atomically
- The `cai-implement.md` staging mechanism correctly creates the new agent file via `.cai-staging/agents/`
- The `cai-fix.md` staging mechanism overwrites the old protected file with a deprecated stub

## Revision 1 (2026-04-13)

### Rebase
- clean

### Files touched this revision
- none — all changes already applied

### Decisions this revision
- Review comment from @damien-robotsix (cai review-docs) was an informational summary, not a change request — all four fixes (README.md:590, publish.py:97+138, Dockerfile:63, cai.py:717) were already applied and committed before the review comment was posted.

### New gaps / deferred
- none

## Revision 2 (2026-04-13)

### Rebase
- clean

### Files touched this revision
- `cai_lib/cmd_lifecycle.py`:258 — `"fix"` key → `"implement"` in `_reconcile_interrupted` handler dict
- `tests/test_reconcile.py`:25,36,47,54 — four `_reconcile_interrupted("fix", ...)` calls → `"implement"`

### Decisions this revision
- `_reconcile_fix` function itself not renamed — it's an internal helper; only the dispatch key matters for correctness
- Tests updated to exercise the renamed command verb, matching the active-job value written by `cmd_implement()`

### New gaps / deferred
- none

## Revision 3 (2026-04-13)

### Rebase
- clean

### Files touched this revision
- none

### Decisions this revision
- All three review findings (cross_cutting_ref on _build_implement_user_message, cmd_implement import, and cleanup-pr-context.yml comment) were already applied in a prior commit; reviewer posted against an earlier commit state. No edits needed.

### New gaps / deferred
- none

## Revision 4 (2026-04-13)

### Rebase
- resolved: cai.py (4 conflict regions in cmd_cycle — HEAD added outer loop / plan-all re-entry logic; PR renamed fix→implement; merged by keeping HEAD structure with implement naming)

### Files touched this revision
- none (review comment already addressed)

### Decisions this revision
- Review comment from @damien-robotsix (cai review-docs) claimed docs/architecture.md:49 and README.md:54 still said "fix loop"; both were already updated in prior commits. No edits needed.

### New gaps / deferred
- none
