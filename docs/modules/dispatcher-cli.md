# dispatcher-cli

Top-level Python entry point and root-level shims. `cai.py` is the main CLI
dispatcher providing 16+ subcommands for the self-improvement loop; `parse.py`
and `publish.py` are thin wrappers that re-export the real implementations
from `cai_lib/`.

## Entry points
- `cai.py` — Main CLI dispatcher with all `cai` subcommands.
- `parse.py` — Wrapper shim; real implementation in `cai_lib/parse.py`.
- `publish.py` — Wrapper shim; real implementation in `cai_lib/publish.py`.

## Dependencies
- `cai-lib` — all business logic lives in `cai_lib/`.
