# PR Context Dossier
Refs: robotsix-cai/robotsix-cai#449

## Files touched
- `.cai-staging/agents/cai-cost-optimize.md`:1 — new agent definition (Read/Grep/Glob, sonnet-4-6, memory:project); proposal and evaluation output formats
- `cai.py`:173 — added `COST_OPTIMIZE_MEMORY` constant
- `cai.py`:4831 — added `_read_cost_optimize_memory`, `_save_cost_optimize_memory`, `cmd_cost_optimize`
- `cai.py`:8273 — added `"cost-optimize"` argparse subcommand after `explore`
- `cai.py`:8335 — added `"cost-optimize": cmd_cost_optimize` to handlers dict
- `entrypoint.sh`:20 — added cost-optimize to task list comment
- `entrypoint.sh`:53 — added `CAI_COST_OPTIMIZE_SCHEDULE` env var (default `0 5 * * 0`)
- `entrypoint.sh`:75 — added cron line for cost-optimize
- `README.md`:67 — added cost-optimize row to subcommand table
- `README.md`:76 — added `CAI_COST_OPTIMIZE_SCHEDULE` to env var list

## Files read (not touched) that matter
- `cai.py` (lines 338–455) — `_load_cost_log`, `_row_ts`, `_build_cost_summary`: reused directly in `cmd_cost_optimize`
- `cai.py` (lines 4833–4858) — `_read_propose_memory`/`_save_propose_memory` pattern followed exactly
- `.claude/agents/cai-propose.md` — used as template for agent definition structure

## Key symbols
- `COST_OPTIMIZE_MEMORY` (`cai.py`:173) — path to `/var/log/cai/cost-optimize-memory.md`
- `_read_cost_optimize_memory` (`cai.py`:~4833) — reads memory file for prior proposals
- `_save_cost_optimize_memory` (`cai.py`:~4845) — persists `## Memory Update` block from agent output
- `cmd_cost_optimize` (`cai.py`:~4860) — main handler; builds cost data, runs agent, creates issue or logs evaluation
- `_by_agent_detailed` (inline in `cmd_cost_optimize`) — aggregates cost/tokens/cache per agent for WoW table

## Design decisions
- No repo clone needed: agent works on cost log data passed in user message, not source code
- `_build_cost_summary(days=14, top_n=20)` reused directly to avoid duplicating health-report logic
- `cache_read_input_tokens` field name used (not `cache_read_tokens`) per actual log schema
- Evaluation conclusions tracked in memory only; no comment posted to original issue (keeps it simple for first iteration)
- Proposal dedup via `cost-optimize-{key}` fingerprint in issue body, same pattern as `cai-propose`

## Out of scope / known gaps
- Evaluation results are not posted as comments on the original proposal issue (memory-only tracking)
- No `--dry-run` flag (can be added later following health-report pattern)
- Agent does not inspect source code/agent definitions; relies on cost data alone for proposals

## Invariants this change relies on
- `/var/log/cai/cai-cost.jsonl` rows contain `cache_read_input_tokens` field (not `cache_read_tokens`)
- `_run_claude_p` signature: `(cmd, *, category, agent, **kwargs)` with `input` and `cwd` as kwargs
- `_build_cost_summary` returns empty string when no rows exist (handled: early return on `not rows_14d`)
- `log_run` accepts `exit` keyword argument (consistent with other callers)
