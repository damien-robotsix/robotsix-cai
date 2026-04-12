# PR Context Dossier
Refs: robotsix-cai/robotsix-cai#438

## Files touched
- `cai.py:519` — replaced `next()` with full result-event collection; added subagent token aggregation and `parent_cost_usd`/`subagents` fields to the cost row

## Files read (not touched) that matter
- `cai.py:510–600` — `_run_claude_p` cost-row construction; only this section was changed

## Key symbols
- `_run_claude_p` (`cai.py:~480`) — wraps `claude -p` subprocess; constructs the cost row written to `cai-cost.jsonl`
- `subagent_results` (`cai.py:520`) — new variable: list of result events before the last one (subagents)
- `combined` (`cai.py:564`) — replaces `flat`; parent tokens + summed subagent tokens
- `subagent_rows` (`cai.py:563`) — per-subagent token dicts with `cost_usd`; appended to `row["subagents"]`
- `log_cost` (`cai.py:596`) — unchanged; writes the row to `cai-cost.jsonl`

## Design decisions
- Parent result = last `"type": "result"` event; subagents = all earlier ones — matches Claude Code emission order
- `row.update(combined)` replaces `row.update(flat)` so flat token fields become combined totals
- `parent_cost_usd = total_cost_usd - sum(subagent cost_usd)` — derived, not from a usage dict
- `cost_usd` stays sourced from the parent envelope's `total_cost_usd` (already correct, no change)
- Rejected: using `models` dict from parent envelope to split costs — too indirect; per-event `total_cost_usd` is more reliable

## Out of scope / known gaps
- `cmd_cost_report` reads `input_tokens`/`output_tokens`; now gets combined totals — correct behavior, no code change needed
- Subagent `type` (e.g. "Explore") not extracted — not available on result events without additional parsing
- If Claude Code changes result-event ordering, parent/subagent split breaks (combined totals still correct)

## Invariants this change relies on
- The last `"type": "result"` in the verbose JSON array is always the parent/top-level result
- Subagent result events have their own `usage` dict with the same flat_keys structure
- The parent envelope's `total_cost_usd` already covers all subagents (unchanged assumption)
- `subagent_results` is `[]` when `parsed` is a dict (legacy format) — no behavioral change in that path
