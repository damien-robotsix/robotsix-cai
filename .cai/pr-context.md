# PR Context Dossier
Refs: robotsix-cai/robotsix-cai#413

## Files touched
- `cai.py:192` — Added `OUTCOME_LOG_PATH = Path("/var/log/cai/cai-outcomes.jsonl")`
- `cai.py:228-244` — Added `_log_outcome()` helper (appends JSON record to outcome log, never raises)
- `cai.py:246-301` — Added `_load_outcome_counts()` (reads outcome log, returns per-category totals) and `_load_outcome_stats()` (computes success rates, 0.60 prior for <3 obs)
- `cai.py:1086-1111` — Replaced FIFO `min(createdAt)` in `_select_fix_target()` with scored `max()` using age × success_rate × (1/prior_attempts)
- `cai.py:4833-4960` — Rewrote verdict loop in `cmd_confirm()`: added `merged_by_num` lookup dict, `_extract_category()` helper, outcome logging for all three verdicts, and re-queue logic (up to 3 attempts → `:refined`, then `needs-human-review`)
- `cai.py:4142-4159` — Appended Category Success Rates table to `cmd_cost_report()`

## Files read (not touched) that matter
- `cai.py` (log_cost pattern, ~line 210) — followed the same "never raises" pattern for `_log_outcome`
- `cai.py` (LABEL constants, ~line 169-182) — used `LABEL_REFINED` and `LABEL_PR_NEEDS_HUMAN` for re-queue/escalation
- `cai.py` (_fetch_previous_fix_attempts, ~line 1085) — used as-is for prior attempt count; NOT modified

## Key symbols
- `_log_outcome` (cai.py:228) — writes one outcome record per confirm verdict
- `_load_outcome_counts` (cai.py:246) — raw per-category {total, solved} counts (90-day window)
- `_load_outcome_stats` (cai.py:286) — success rates; <3 obs → 0.60 prior
- `_score` (cai.py:1090) — nested function in `_select_fix_target`; scores each candidate
- `_extract_category` (cai.py:4863) — nested function in `cmd_confirm` verdict loop; pulls `category:X` label value
- `requeue_matches` (cai.py:4895) — `re.findall` (not `re.search`) to handle multiple appended re-queue blocks

## Design decisions
- Used `re.findall` + `[-1]` for re-queue count parsing — multiple appended blocks exist after repeated re-queues; `re.search` would return the first (lowest) count
- Used `LABEL_PR_NEEDS_HUMAN = "needs-human-review"` for escalation — already used for human-attention scenarios, consistent with existing conventions
- Kept `_fetch_previous_fix_attempts` unmodified — scope guardrail
- `_load_outcome_counts` is the canonical file reader; `_load_outcome_stats` delegates to it (DRY)
- Rejected: separate `outcomes.py` module — issue says prefer inline matching `log_cost()` pattern

## Out of scope / known gaps
- `_fetch_previous_fix_attempts` is called once per candidate in `_select_fix_target()` — adds latency for large candidate sets but acceptable per scope guardrails
- `cai-confirm.md` agent definition not modified — confidence field addition deferred
- `publish.py` not modified — category labels already set correctly

## Invariants this change relies on
- `category:{value}` labels are already applied to issues by `publish.py`
- `_fetch_previous_fix_attempts` returns empty list on API failure (safe default for scoring)
- `OUTCOME_LOG_PATH.parent` (`/var/log/cai/`) is writable at runtime (same assumption as `COST_LOG_PATH`)
- The `%Y-%m-%dT%H:%M:%SZ` strptime format matches GitHub API `createdAt` field format
