# PR Context Dossier
Refs: damien-robotsix/robotsix-cai#486

## Files touched
- `cai_lib/__init__.py` (NEW) — package init re-exporting all symbols from submodules for backward compat
- `cai_lib/config.py` (NEW) — all constants: REPO, LABEL_*, LOG_PATH, path defs, _STALE_* thresholds
- `cai_lib/logging.py` (NEW) — observability helpers: log_run, log_cost, _write_active_job, _clear_active_job, outcome/cost log readers
- `cai_lib/subprocess.py` (NEW) — _run, _run_claude_p (imports log_cost from cai_lib.logging)
- `cai_lib/github.py` (NEW) — _gh_json, check_*_auth, _set_labels, _issue_has_label, _build_issue_block, _build_fix_user_message
- `cai_lib/cmd_lifecycle.py` (NEW) — _rollback_stale_in_progress (imports _gh_json/_set_labels/log_run/LOG_PATH from siblings)
- `cai_lib/cmd_fix.py` (NEW) — _parse_decomposition (stdlib only: re)
- `Dockerfile:109` — added `COPY --chown=cai:cai cai_lib/ /app/cai_lib/`
- `tests/test_multistep.py:9` — `from cai import _parse_decomposition` → `from cai_lib import _parse_decomposition`
- `tests/test_rollback.py:12` — `import cai` → `import cai_lib as cai`; patches changed to target `cai_lib.cmd_lifecycle.*`

## Files read (not touched) that matter
- `cai.py` — source of truth; unchanged in this PR; cai_lib modules copy (not move) the relevant functions as Phase 1 of the split
- `tests/test_rollback.py` — read to understand mock structure before deciding patch target approach

## Key symbols
- `_rollback_stale_in_progress` (`cai_lib/cmd_lifecycle.py:32`) — the function whose mock dependencies must be patched at `cai_lib.cmd_lifecycle.*`
- `_parse_decomposition` (`cai_lib/cmd_fix.py:7`) — stateless regex function; no external deps beyond stdlib `re`
- `_gh_json` (`cai_lib/github.py:17`) — direct `subprocess.run` call (not via `_run`); patched in tests via string path

## Design decisions
- **Copy, don't move (Phase 1 only):** `cai.py` is 8600 lines / 84K tokens; safely removing the originals requires a second PR that can verify with tests. This PR establishes the package structure and updates tests; Phase 2 (a follow-up) will remove from cai.py and add `from cai_lib.X import Y` there.
- **Patch target is submodule, not package:** `patch.object(cai_lib, "_gh_json")` would NOT intercept the call in `cmd_lifecycle.py` because Python resolves global names in the defining module's namespace. Changed test patches to `patch("cai_lib.cmd_lifecycle._gh_json", ...)` etc.
- **No `cai/` vs `cai.py` naming conflict:** Used `cai_lib/` (not `cai/`) to avoid Python import resolution confusion when both `cai.py` and `cai/` exist in the same directory.
- Rejected: moving `_run` helpers to a module named `subprocess.py` under `cai_lib/` risks shadowing stdlib `subprocess` — confirmed safe because Python 3 absolute imports resolve `import subprocess` to stdlib (not the sibling file).

## Out of scope / known gaps
- The remaining ~8000 lines of `cmd_*` functions in `cai.py` are NOT yet in `cai_lib`; that is Phase 2.
- `cai.py` still defines duplicates of every symbol in `cai_lib` — they'll be removed in Phase 2.
- `entrypoint.sh`, `cai.py` CLI, and all cron schedules are unchanged.

## Invariants this change relies on
- `cai_lib.cmd_lifecycle` imports `_gh_json`, `_set_labels`, `log_run`, `LOG_PATH` at module level — patches must target `cai_lib.cmd_lifecycle.<name>`, not `cai_lib.<name>`.
- `cai_lib/__init__.py` re-exports match the exact attribute names the tests use via `cai.<name>` (where `cai` is `cai_lib`).

## Revision 1 (2026-04-13)

### Rebase
- clean

### Files touched this revision
- none (no code changes)

### Decisions this revision
- Created two follow-up issues as requested by reviewer @damien-robotsix:
  - #533: cai-review-pr should receive original issue body in user message context
  - #534: Phase 2 — remove cai.py symbol duplicates and add `from cai_lib.X import Y` imports

### New gaps / deferred
- None; no review comments required code changes.
