# scripts

Maintenance shell scripts and the coverage verifier invoked manually or
from CI. `scripts/generate-index.sh` regenerates `CODEBASE_INDEX.md`;
`scripts/server-cleanup.sh` does age/size cleanup on the transcript-sync
store; `scripts/check-modules-coverage.py` verifies every tracked file
is matched by exactly one module in `docs/modules.yaml`.

## Entry points
- `scripts/generate-index.sh` — Generator for `CODEBASE_INDEX.md`.
- `scripts/server-cleanup.sh` — Server-side transcript-sync cleanup.
- `scripts/check-modules-coverage.py` — Module-coverage verifier.
