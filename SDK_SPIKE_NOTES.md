# SDK Spike Notes — Issue #1226

This document records the outcome of the spike that ports
`cai_lib/actions/confirm.py` off the `_run_claude_p` argv facade onto a
direct `claude_agent_sdk.ClaudeAgentOptions` + `run_subagent(...)` call.

## Net LOC delta

- Added: `cai_lib/subprocess_utils.py::run_subagent` (~190 lines, copy-port of
  `_run_claude_p` with the argv-parse step dropped and the field-set construction
  preserved byte-for-byte).
- Added: `tests/test_sdk_spike_parity.py` (~110 lines).
- Modified: `cai_lib/actions/confirm.py` — two call sites; small (~10 lines net,
  trading argv-list construction for typed `ClaudeAgentOptions` construction).

The spike strictly *increases* total LOC because both code paths coexist by
design — `_run_claude_p` is unchanged so the parity test can compare them
side-by-side. Net savings are deferred until the remaining 12 handlers are
ported and the facade plus `_argv_to_options` can be deleted.

## ClaudeAgentOptions fields the facade implicitly sets

`_argv_to_options` (`cai_lib/subprocess_utils.py:116-189`) and the
post-parse block in `_run_claude_p` (lines 542-547) implicitly set
several fields callers tend to forget when constructing
`ClaudeAgentOptions` by hand:

- `cli_path` — pinned to `shutil.which("claude")` so the SDK reuses
  the npm-installed CLI audited in the Dockerfile rather than the wheel-
  bundled copy.
- `stderr` — a bounded sink (200 lines / 4 000 chars max) so SDK
  errors surface in logs instead of vanishing behind the SDK's
  `"Check stderr output for details"` placeholder.
- `plugins` — auto-appends `.claude/plugins/cai-skills` (when the
  directory exists at the caller's cwd) without the caller asking.
- `extra_args` — every unknown `--flag value` argv pair forwards
  here; `--agent <name>` rides this channel today.
- `add_dirs`, `allowed_tools`, `plugins` are pre-initialised to
  empty lists by the argv parser so callers can `.append(...)` safely.

`run_subagent(...)` re-applies the first three (`cli_path`, `stderr`
sink, `cai-skills` auto-inject) inside its own body so callers
constructing `ClaudeAgentOptions` directly do not need to remember
them. It does NOT auto-populate `extra_args` / `add_dirs` /
`allowed_tools` — those are typed fields the caller passes in the
options object directly.

## Sketch: porting the remaining 12 handlers

The remaining argv call sites fall into three buckets:

1. **Plain `--agent <name>` calls** (no JSON-schema, no extra flags) —
   straight ports: drop the argv list, construct
   `ClaudeAgentOptions(extra_args={"agent": "<name>"})`, call
   `run_subagent(...)`. Estimated effort: ~10 lines per call site.
2. **`--json-schema` calls** (triage, refine, split, plan/select,
   unblock, rescue, merge, revise/filter): need to lift the schema
   construction into Python (parse the JSON file, build the
   `output_format = {"type": "json_schema", "schema": ...}` dict) and
   set it on the options object. `run_subagent` already preserves the
   `subtype == "error_max_structured_output_retries"` short-circuit
   that those callers depend on. Estimated effort: ~15 lines per call
   site.
3. **`actions/explore.py`** uses the legacy `timeout=` kwarg (30-min
   cap). Already supported by `run_subagent(timeout=...)`.

After all 13 are ported, `_run_claude_p`, `_argv_to_options`, and the
parity test in this spike all become deletable. The argv facade is the
only remaining caller of `_argv_to_options`.

## Parity check result

`python -m unittest tests.test_sdk_spike_parity` (and the equivalent
discovery via `python -m unittest discover tests`) passes. The cost
rows emitted via `_run_claude_p` and `run_subagent` for the same
mocked `ResultMessage` fixture are equal modulo the volatile
`ts` / `session_id` / `host` keys, and the returned
`subprocess.CompletedProcess.{returncode, stdout, stderr}` triples
match byte-for-byte on success, structured-output, and
`is_error=True` paths.

## Return type evolution to `RunResult` (PR #1277)

Following the spike, the `subprocess.CompletedProcess` return type was
replaced with a typed `RunResult` Pydantic model (issue #1277). The new
return type preserves the same observable behavior (`.ok` property mirrors
`.returncode == 0`, `.stdout` carries the same content, `.error_summary`
replaces `.stderr`), but provides structured access to the
`ResultMessage`, error subtypes, and captured stderr lines without
re-parsing opaque strings. The parity test continues to verify that both
call paths emit identical cost-row payloads.
