# transcripts

Transcript parsing and cross-host sync. `cai_lib/parse.py` is the
deterministic signal extractor consumed by on-demand
workflow-enhancement audits; `cai_lib/transcript_sync.py` fans
JSONL session transcripts between long-lived workers and the
audit host.

## Key entry points
- [`cai_lib/parse.py`](../../cai_lib/parse.py) —
  `extract_tool_calls(lines)` walks parsed JSONL events and
  returns per-call summaries; `_categorize_error(error_text)` and
  `_extract_error_text(block)` classify tool-call failures;
  `_get_cutoff_time` / `_get_max_files` read env-var limits;
  `collect_jsonl_lines(source)` loads and filters JSONL files;
  `main()` is the CLI entry (also exposed via the root-level
  `parse.py` shim).
- [`cai_lib/transcript_sync.py`](../../cai_lib/transcript_sync.py)
  — `push()`, `pull()`, `sync()` move JSONL files over rsync+SSH;
  `parse_source()` resolves the local bucket; `cmd_transcript_sync(args)` is the
  CLI subcommand. `_ssh_command`, `_server_bucket`,
  `_server_slug`, `_transport_args` compose the transport.

## Inter-module dependencies
- Imports from **config** — `transcript_sync.py` depends on
  `cai_lib.config` for bucket paths and machine/instance IDs.
- Imported by **cli** — `cmd_transcript_sync` is a direct
  subcommand; on-demand workflow-enhancement audits read the
  parsed output.
- Imported by **audit** (indirect) — `cai-audit-workflow-enhancement`
  reads the same JSONL store this module writes.
- Imported by **tests** — `tests/test_parse.py` (signal
  extraction) and `tests/test_transcript_sync.py` (no-op /
  fallback paths / repo-slug).
- No upstream imports from actions/handlers.

## Operational notes
- **Cost sensitivity.** `parse.py` itself is free (pure Python);
  its output size directly controls the cost of any on-demand
  audit that consumes the signals. The `CAI_PARSE_MAX_FILES` and
  `CAI_PARSE_CUTOFF_*` env vars bound the per-run load.
- **SSH transport.** `transcript_sync` assumes rsync over SSH to
  the configured server bucket. Missing `rsync` or a missing SSH
  key degrades to a no-op — verify via `config.transcript_sync_enabled()`
  before diagnosing phantom failures.
- **Cross-host concerns.** Multiple workers push to the same
  server bucket; the server-side cleanup script
  (`scripts/server-cleanup.sh`) enforces age/size limits.
- **CI implications.** Both files have dedicated pytest modules;
  the sync test stubs subprocess calls and does not exercise the
  real network path.
- **Root shim.** `parse.py` at repo root is a thin wrapper that
  re-exports `cai_lib/parse.py` — preserve it for backwards
  compatibility.
