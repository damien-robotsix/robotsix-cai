# config

Shared infrastructure utilities — constants / path definitions,
structured logging, subprocess helpers, and the stale-lock watchdog.
These are cross-cutting dependencies imported by nearly every handler.

## Entry points
- `cai_lib/config.py` — Shared constants and path definitions.
- `cai_lib/logging_utils.py` — Logging utilities.
- `cai_lib/subprocess_utils.py` — Subprocess helpers with timeouts.
- `cai_lib/watchdog.py` — Stale-lock watchdog.
