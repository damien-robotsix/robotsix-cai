# PR Context Dossier
Refs: damien-robotsix/robotsix-cai#706

## Files touched
- `install.sh:123-140` — added admin login prompt block (before auth case statement); builds `CAI_ADMIN_ENV_LINE`
- `install.sh:176` — injects `${CAI_ADMIN_ENV_LINE}` into case 1 (OAuth) docker-compose environment section
- `install.sh:249` — injects `${CAI_ADMIN_ENV_LINE}` into case 2 (API key) docker-compose environment section
- `install.sh:275` — appends `CAI_ADMIN_LOGINS` to `.env` for API key mode when admin logins were provided
- `README.md:414-418` — added callout paragraph explaining `CAI_ADMIN_LOGINS` requirement and link to docs
- `cai_lib/cmd_unblock.py:27` — added `ADMIN_LOGINS` to import from `cai_lib.config`
- `cai_lib/cmd_unblock.py:419-427` — added WARNING to stderr at start of `cmd_unblock` when `ADMIN_LOGINS` is empty

## Files read (not touched) that matter
- `cai_lib/config.py:129-138` — shows `ADMIN_LOGINS` is a module-level frozenset built from `CAI_ADMIN_LOGINS` env var; empty default means no one is admin
- `tests/test_unblock.py` — tests only cover pure-logic helpers, not `cmd_unblock` itself; no test changes needed

## Key symbols
- `CAI_ADMIN_ENV_LINE` (`install.sh:137`) — shell variable holding the YAML line to inject into docker-compose environment section, empty if user skipped
- `ADMIN_LOGINS` (`cai_lib/config.py:129`) — frozenset of authorized GitHub logins; empty if `CAI_ADMIN_LOGINS` not set
- `cmd_unblock` (`cai_lib/cmd_unblock.py:417`) — entry point that now warns early when `ADMIN_LOGINS` is empty

## Design decisions
- Prompt BEFORE the auth-mode case statement so both OAuth and API-key modes get the same prompt
- OAuth mode: write `CAI_ADMIN_LOGINS` to docker-compose environment section (no .env for that mode)
- API key mode: write to both docker-compose environment section AND `.env` (plan said .env; environment section is belt-and-braces for re-runs)
- Used `${CAI_ADMIN_ENV_LINE}` with a trailing blank line in the YAML heredoc — blank lines are valid YAML and avoid complex conditional logic
- WARNING prints to stderr (not stdout) so it doesn't contaminate log parsing, but is visible in journal
- Rejected: emitting WARNING only when `human:solved` targets exist — the warning is more useful upfront before the gh API calls rather than buried after

## Out of scope / known gaps
- Existing installs (already have `docker-compose.yml` / `.env`) are not retroactively updated — users must re-run `install.sh` or manually set `CAI_ADMIN_LOGINS`
- `docs/configuration.md` already documents `CAI_ADMIN_LOGINS`; no changes needed there

## Invariants this change relies on
- `CAI_ADMIN_LOGINS` must be accessible to the container at runtime (env_file or environment: block) for `is_admin_login` to work
- The heredoc delimiters for docker-compose.yml are unquoted (`<<YAML`) so shell variables expand inside them
