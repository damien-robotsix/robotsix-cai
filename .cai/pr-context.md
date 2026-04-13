# PR Context Dossier
Refs: robotsix-cai/cai#502

## Files touched
- `install.sh:107` — `--interval=1800` → `--interval=43200` in Watchtower service heredoc
- `docker-compose.yml:105` — `#     - --interval=1800` → `#     - --interval=43200` in commented template
- `README.md:394` — "every 30 minutes" → "every 12 hours (43200 s)"

## Files read (not touched) that matter
- `cai.py` — confirmed unrelated `timeout=1800` at ~line 7699 was not touched

## Key symbols
- `WATCHTOWER_SERVICE` (`install.sh:100-109`) — heredoc that generates the watchtower compose service block

## Design decisions
- 43200 s (12h) chosen per maintainer comment; matches issue proposal
- Added `(43200 s)` to README prose so users can cross-reference the `--interval` flag without mental arithmetic

## Out of scope / known gaps
- Existing installs require manual update or re-running `install.sh`; README already documents this at lines 418-422
- No change to Watchtower image pin, other flags, or Docker Compose structure

## Invariants this change relies on
- Watchtower remains enabled; only the poll frequency changes
- `cai.py` `timeout=1800` is a subagent timeout, unrelated to this interval
