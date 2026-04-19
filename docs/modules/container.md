# container

Docker image definition, Compose orchestration, installer script, entry
point, and example configuration files used to deploy robotsix-cai as a
long-lived service.

## Entry points
- `Dockerfile` — Container image definition (Python 3.12 + Node + claude-code CLI).
- `docker-compose.yml` — Multi-service orchestration with named volumes.
- `entrypoint.sh` — Templates crontab, runs initial cycle, execs supercronic.
- `install.sh` — Interactive installer for end-users.
- `.env.example` — Required environment-variable template.
- `workspaces.json.example` — Multi-workspace configuration template.
- `.gitignore` — Git ignore rules.
- `pyproject.toml` — Python project configuration (ruff lint settings).
