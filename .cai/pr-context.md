# PR Context: #314 — New subagent cai-spike

## Files touched
- `cai.py` — added `cmd_spike`, `LABEL_NEEDS_SPIKE`, `_STATE_PRIORITY` entry, spike marker check, argparse registration
- `entrypoint.sh` — added `CAI_SPIKE_SCHEDULE`, crontab line, initial-pass call
- `.claude/agents/cai-spike.md` — new agent file (via staging)
- `README.md` — updated lifecycle diagram and cron schedule docs

## Key symbols
- `LABEL_NEEDS_SPIKE` — cai.py label constant
- `cmd_spike` — main spike command function in cai.py
- `_STATE_PRIORITY` — dict in cai.py that needed `:needs-spike` entry
- `CAI_SPIKE_SCHEDULE` — entrypoint.sh env var

## Design decisions
- Spike agent uses `claude-opus-4-5` model (expensive research)
- Cron schedule every 2 hours (lower cadence than fix)
- Three output shapes: `## Spike Findings`, `## Refined Issue`, `## Spike Blocked`
- No `--issue` flag in MVP (always picks oldest `:needs-spike` issue)

## Out of scope / known gaps
- Memory-review gating for spike agent memory updates (follow-on)
- `--issue` flag for `cmd_spike`
- Edit/Write tools for spike agent (Bash + Read/Grep/Glob/Agent is sufficient)

## Invariants this change relies on
- `LABEL_NEEDS_SPIKE` defined at cai.py and used consistently
- Spike agent runs in a throwaway clone (no git commits)
- Output parsing keyed on `## Spike Findings` / `## Refined Issue` / `## Spike Blocked` markers

## Revision 1 (2026-04-12)

### Rebase
- clean

### Files touched this revision
- `.claude/agents/cai-audit.md` (via staging) — added `:needs-spike` to Active states list on line 64-65

### Decisions this revision
- Added `:needs-spike` to cai-audit.md active states list so the audit agent checks for stale `:needs-spike` issues

### New gaps / deferred
- None
