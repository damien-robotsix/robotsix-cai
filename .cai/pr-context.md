# PR Context Dossier
Refs: damien-robotsix/robotsix-cai#457

## Files touched
- `cai.py:223` — Added `ACTIVE_JOB_PATH = Path("/var/log/cai/cai-active.json")`
- `cai.py:226-244` — Added `_write_active_job(cmd, issue)` and `_clear_active_job()` helpers
- `cai.py:3865-3872` — Added `immediate: bool = False` keyword-only param to `_rollback_stale_in_progress()`
- `cai.py:3935` — Changed `threshold = ttl_hours * 3600` to `threshold = 0 if immediate else ttl_hours * 3600`
- `cai.py:7964` — Changed `cmd_cycle` call to `_rollback_stale_in_progress(immediate=True)`
- `cai.py:2188` — Added `_write_active_job("fix", issue_number)` after lock in `cmd_fix`
- `cai.py:2217,2237` — Added `_clear_active_job()` before early returns in pre-screen path of `cmd_fix`
- `cai.py:2618` — Added `_clear_active_job()` in `cmd_fix` finally block
- `cai.py:3146` — Added `_write_active_job("revise", issue_number)` after lock in `cmd_revise`
- `cai.py:3618` — Added `_clear_active_job()` in `cmd_revise` per-target finally block
- `cai.py:7330` — Added `_write_active_job("spike", issue_number)` after lock in `cmd_spike`
- `cai.py:7520` — Added `_clear_active_job()` in `cmd_spike` finally block
- `tests/test_rollback.py` — New test file covering `immediate=True` vs `immediate=False` behavior

## Files read (not touched) that matter
- `cai.py` (lines 3838–3945) — `_rollback_stale_in_progress()` full body
- `cai.py` (lines 2155–2240) — `cmd_fix` lock + pre-screen area
- `cai.py` (lines 3107–3150) — `cmd_revise` per-target lock area
- `cai.py` (lines 7286–7330) — `cmd_spike` lock area

## Key symbols
- `_rollback_stale_in_progress` (`cai.py:3865`) — adds `immediate` kwarg; when True sets threshold=0
- `_write_active_job` (`cai.py:226`) — writes JSON state to ACTIVE_JOB_PATH
- `_clear_active_job` (`cai.py:240`) — writes `{}` to ACTIVE_JOB_PATH
- `ACTIVE_JOB_PATH` (`cai.py:223`) — `/var/log/cai/cai-active.json`
- `cmd_cycle` (`cai.py:7964`) — now passes `immediate=True` for instant restart recovery
- `cmd_audit` (`cai.py:4104`) — unchanged, uses default `immediate=False`

## Design decisions
- `threshold = 0` when `immediate=True`: age > 0 is always true for any non-zero-age issue, so all locks roll back
- `_clear_active_job()` writes `{}` not deletes: easier to parse than missing file, avoids TOCTOU
- `immediate` is keyword-only (`*`): prevents accidental positional usage
- Active-job writes placed AFTER the lock check succeeds but BEFORE the work_dir setup
- Pre-screen early returns in `cmd_fix` also call `_clear_active_job()` since the lock was already acquired

## Out of scope / known gaps
- Did not add `_write_active_job` / `_clear_active_job` to `cmd_explore` (not in scope per issue guardrails)
- Did not change TTL constants `_STALE_IN_PROGRESS_HOURS` / `_STALE_REVISING_HOURS`
- State file is write-only observability; no logic reads it to gate pipeline decisions

## Invariants this change relies on
- `cmd_cycle` is only called from `entrypoint.sh` at container start — `immediate=True` is safe there
- `cmd_audit`'s `_rollback_stale_in_progress()` call continues using default `immediate=False` (TTL-based)
- `/var/log/cai/` directory already exists (created by other log helpers like `OUTCOME_LOG_PATH`)
