# PR Context Dossier
Refs: robotsix-cai/robotsix-cai#693

## Files touched
- `cai_lib/actions/triage.py`:1-13 — replaced `import re` with `import json`
- `cai_lib/actions/triage.py`:34-77 — removed 4 parser functions + 3 regex constants; added `_TRIAGE_JSON_SCHEMA` dict
- `cai_lib/actions/triage.py`:167-209 — replaced `_run_claude_p` call (added `--json-schema`) and verdict parsing (JSON instead of regex)
- `cai_lib/actions/triage.py`:224-225 — replaced `_parse_triage_skip_confidence(result.stdout)` with direct dict access
- `cai_lib/actions/triage.py`:241 — replaced `_parse_triage_plan(result.stdout)` with `tool_input.get("plan")`
- `cai_lib/actions/triage.py`:267 — replaced `_parse_triage_ops(result.stdout)` with `tool_input.get("ops")`
- `.cai-staging/agents/cai-triage.md` — added `<!-- Forced tool-use: submit_triage_verdict. See #686 Step 2. -->` at top

## Files read (not touched) that matter
- `cai_lib/actions/plan.py` — established `--json-schema` pattern used by `cai-select`; followed same approach
- `cai_lib/subprocess_utils.py` — confirmed `_run_claude_p` accepts extra flags in the cmd list

## Key symbols
- `_TRIAGE_JSON_SCHEMA` (`cai_lib/actions/triage.py`:38) — JSON schema dict passed as `--json-schema` to force structured output
- `tool_input` (`cai_lib/actions/triage.py`:190) — parsed JSON dict replacing all regex extractions
- `skip_conf_str` / `skip_conf` (`cai_lib/actions/triage.py`:224-225) — converts optional string field to `Confidence` enum

## Design decisions
- Used `--json-schema` with existing `_run_claude_p` (not a new Anthropic SDK client) — matches established pattern from `cai-select` in `plan.py`
- `kind` required in schema (enum code/maintenance) even though agent prompt says omit for HUMAN — JSON schema forces the field; fallback default "code" handles it
- Removed `print(result.stdout)` that previously dumped raw agent output — with JSON output that would print an unreadable blob; verdict summary is printed separately
- On JSON parse failure, `tool_input = {}` makes all fields fall to defaults; `decision=""` routes to REFINE via the else branch

## Out of scope / known gaps
- `cai_lib/dup_check.py` still uses regex parsing — separate Step 3/4 of #686
- `cai_lib/actions/merge.py` still uses `_parse_merge_verdict` regex — separate step of #686

## Invariants this change relies on
- `claude -p --json-schema <schema>` returns valid JSON matching the schema when the agent succeeds (returncode 0)
- `Confidence` enum members are named LOW, MEDIUM, HIGH — used for `Confidence[skip_conf_str]` lookup
