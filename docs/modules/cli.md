# cli

Top-level CLI dispatcher and subcommand implementations. `cai.py` is the
main entry point that parses arguments and routes to the per-command
handlers in `cai_lib/cmd_*.py`. Root-level `parse.py` and `publish.py`
are thin shims re-exporting their `cai_lib/` counterparts.
`cai_lib/__init__.py` and `cai_lib/dispatcher.py` wire together
state→handler routing for the lifecycle FSM.

## Entry points
- `cai.py` — Main CLI dispatcher with all `cai` subcommands.
- `parse.py`, `publish.py` — Root-level shims re-exporting `cai_lib/` implementations.
- `cai_lib/__init__.py` — Package init.
- `cai_lib/dispatcher.py` — Issue/PR FSM dispatcher routing to handlers.
- `cai_lib/cmd_*.py` — Subcommand implementations (agents, cycle, helpers, implement, misc, rescue, unblock).
