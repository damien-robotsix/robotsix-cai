# installer

Container image, Compose orchestration, installer script, entry point,
and example configuration templates used to deploy robotsix-cai as a
long-lived service.

## Entry points
- `Dockerfile` — Container image definition.
- `docker-compose.yml` — Multi-service orchestration.
- `entrypoint.sh` — Templates crontab, runs initial cycle, execs supercronic.
- `install.sh` — Interactive installer for end-users.
- `.env.example` — Required environment-variable template.
- `workspaces.json.example` — Multi-workspace configuration template.
- `.gitignore` — Git ignore rules.
