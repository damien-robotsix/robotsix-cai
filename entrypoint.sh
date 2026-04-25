#!/usr/bin/env bash
# Container stays alive so `docker compose exec` can launch interactive
# claude sessions on demand. No scheduled work yet.
set -euo pipefail

exec tail -f /dev/null
