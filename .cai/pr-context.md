# PR Context Dossier
Refs: damien-robotsix/robotsix-cai#692

## Files touched
- `Dockerfile:58` — added `RUN pip install --no-cache-dir "anthropic>=0.25"` before user creation
- `cai_lib/structured_client.py` — new file; `call_with_tool()` wraps Anthropic messages API with forced tool-use and logs cost via `log_cost`
- `cai_lib/actions/plan.py:156-264` — added `_strip_frontmatter`, `_extract_frontmatter_field` helpers; replaced `_run_select_agent` body with direct API call; removed `parse_confidence` + `re.sub` from `_run_plan_select_pipeline`
- `.cai-staging/agents/cai-select.md` — added frontmatter comment noting body doubles as system prompt; updated output format section for tool-use path

## Files read (not touched) that matter
- `cai_lib/subprocess_utils.py` — reference for `_run_claude_p` cost logging fields mirrored in `call_with_tool`
- `cai_lib/logging_utils.py` — `log_cost` signature and row dict shape
- `cai_lib/fsm.py` — `Confidence` enum and `parse_confidence` (left intact, still used by `handle_plan_gate` and `cmd_unblock.py`)

## Key symbols
- `call_with_tool` (`cai_lib/structured_client.py:16`) — single public function; wraps Anthropic messages API with `tool_choice` forced
- `_run_select_agent` (`cai_lib/actions/plan.py:180`) — now returns `tuple[str, Confidence] | None` instead of `str`; reads agent md from `/app/.claude/agents/cai-select.md`
- `_run_plan_select_pipeline` (`cai_lib/actions/plan.py:267`) — updated to unpack structured result directly; no longer calls `parse_confidence` or `re.sub`
- `submit_selection` tool schema — defined inline in `_run_select_agent`; fields: `plan` (str), `confidence` (enum HIGH|MEDIUM|LOW), `note?` (str)

## Design decisions
- `cost_usd: None` in cost log row — Anthropic messages API doesn't return `total_cost_usd`; field is present but null to keep schema consistent
- Model extracted from frontmatter — `_extract_frontmatter_field(raw, "model")` reads the agent's declared model; falls back to `"claude-opus-4-6"` if missing
- Note prepended as blockquote — if `note` field is set, prepended as `> **Note:** …` before plan text to match the old cai-select output format hint
- Agent md path hardcoded to `/app/.claude/agents/cai-select.md` — container always runs with `/app` as workdir and the canonical agents live there

## Out of scope / known gaps
- `parse_confidence` in `fsm.py` left intact — still used by `handle_plan_gate` (reads from stored plan block) and `cmd_unblock.py`
- `_run_plan_agent` (for cai-plan) still uses `_run_claude_p` subprocess path — only cai-select migrated in this step
- Other gate-critical agents (cai-triage, cai-merge, cai-unblock) not migrated here — tracked in parent issue #686

## Invariants this change relies on
- `/app/.claude/agents/cai-select.md` always exists in the container image (COPY .claude /app/.claude in Dockerfile)
- `ANTHROPIC_API_KEY` env var is set at container runtime (consumed automatically by `anthropic.Anthropic()`)
- Pre-existing F821 ruff violation (`Confidence` in string annotation at `_run_plan_select_pipeline` return type) was present before this PR — not introduced here
