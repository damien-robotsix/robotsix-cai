#!/usr/bin/env bash
# robotsix-cai installer (minimal).
#
# Generates a docker-compose.yml in INSTALL_DIR, pulls the image,
# walks you through Claude + GitHub auth, and optionally adds a
# `cai` shell alias. Re-run safely.
#
# Usage:
#   wget https://raw.githubusercontent.com/damien-robotsix/robotsix-cai/main/install.sh
#   bash install.sh
#
# Env vars:
#   INSTALL_DIR  Install location (default: ./robotsix-cai)
#   IMAGE_TAG    Image tag to pin  (default: latest)

set -euo pipefail

INSTALL_DIR="${INSTALL_DIR:-$(pwd)/robotsix-cai}"
IMAGE_TAG="${IMAGE_TAG:-latest}"

# Resolve a TTY for interactive prompts so the piped form
# (wget -qO- ... | bash) still reads input from the controlling
# terminal instead of the consumed stdin.
if (exec < /dev/tty) 2>/dev/null; then
  TTY=/dev/tty
else
  TTY=/dev/stdin
fi

prompt() {
  local var="$1" message="$2" default="${3:-}"
  local input
  if [[ -n "$default" ]]; then
    printf "%s [%s]: " "$message" "$default" >&2
  else
    printf "%s: " "$message" >&2
  fi
  IFS= read -r input < "$TTY"
  printf -v "$var" '%s' "${input:-$default}"
}

echo "robotsix-cai installer"
echo "======================"
echo "Install dir: $INSTALL_DIR"
echo "Image:       robotsix/cai:$IMAGE_TAG"
echo

mkdir -p "$INSTALL_DIR"
cd "$INSTALL_DIR"

cat > docker-compose.yml <<EOF
# Self-hosted Langfuse + cai stack. Secrets live in .env (gitignore-worthy).
# Langfuse UI: http://localhost:3000

services:
  cai:
    image: robotsix/cai:${IMAGE_TAG}
    restart: unless-stopped
    depends_on:
      langfuse-web:
        condition: service_started
    env_file:
      - .env
    environment:
      LANGFUSE_BASE_URL: http://langfuse-web:3000
      LANGFUSE_PUBLIC_KEY: \${LANGFUSE_INIT_PROJECT_PUBLIC_KEY}
      LANGFUSE_SECRET_KEY: \${LANGFUSE_INIT_PROJECT_SECRET_KEY}
    volumes:
      - cai_home:/home/cai

  langfuse-worker:
    image: docker.io/langfuse/langfuse-worker:3
    restart: unless-stopped
    depends_on: &langfuse-depends-on
      postgres:
        condition: service_healthy
      minio:
        condition: service_healthy
      redis:
        condition: service_healthy
      clickhouse:
        condition: service_healthy
    ports:
      - 127.0.0.1:3030:3030
    environment: &langfuse-worker-env
      DATABASE_URL: postgresql://postgres:\${POSTGRES_PASSWORD}@postgres:5432/postgres
      SALT: \${SALT}
      ENCRYPTION_KEY: \${ENCRYPTION_KEY}
      TELEMETRY_ENABLED: "true"
      CLICKHOUSE_MIGRATION_URL: clickhouse://clickhouse:9000
      CLICKHOUSE_URL: http://clickhouse:8123
      CLICKHOUSE_USER: clickhouse
      CLICKHOUSE_PASSWORD: \${CLICKHOUSE_PASSWORD}
      CLICKHOUSE_CLUSTER_ENABLED: "false"
      LANGFUSE_S3_EVENT_UPLOAD_BUCKET: langfuse
      LANGFUSE_S3_EVENT_UPLOAD_REGION: auto
      LANGFUSE_S3_EVENT_UPLOAD_ACCESS_KEY_ID: minio
      LANGFUSE_S3_EVENT_UPLOAD_SECRET_ACCESS_KEY: \${MINIO_ROOT_PASSWORD}
      LANGFUSE_S3_EVENT_UPLOAD_ENDPOINT: http://minio:9000
      LANGFUSE_S3_EVENT_UPLOAD_FORCE_PATH_STYLE: "true"
      LANGFUSE_S3_EVENT_UPLOAD_PREFIX: events/
      LANGFUSE_S3_MEDIA_UPLOAD_BUCKET: langfuse
      LANGFUSE_S3_MEDIA_UPLOAD_REGION: auto
      LANGFUSE_S3_MEDIA_UPLOAD_ACCESS_KEY_ID: minio
      LANGFUSE_S3_MEDIA_UPLOAD_SECRET_ACCESS_KEY: \${MINIO_ROOT_PASSWORD}
      LANGFUSE_S3_MEDIA_UPLOAD_ENDPOINT: http://localhost:9090
      LANGFUSE_S3_MEDIA_UPLOAD_FORCE_PATH_STYLE: "true"
      LANGFUSE_S3_MEDIA_UPLOAD_PREFIX: media/
      REDIS_HOST: redis
      REDIS_PORT: 6379
      REDIS_AUTH: \${REDIS_AUTH}
      REDIS_TLS_ENABLED: "false"
      NEXTAUTH_URL: http://localhost:3000

  langfuse-web:
    image: docker.io/langfuse/langfuse:3
    restart: unless-stopped
    depends_on: *langfuse-depends-on
    ports:
      - 3000:3000
    environment:
      <<: *langfuse-worker-env
      NEXTAUTH_SECRET: \${NEXTAUTH_SECRET}
      LANGFUSE_INIT_ORG_ID: cai
      LANGFUSE_INIT_ORG_NAME: cai
      LANGFUSE_INIT_PROJECT_ID: cai
      LANGFUSE_INIT_PROJECT_NAME: cai
      LANGFUSE_INIT_PROJECT_PUBLIC_KEY: \${LANGFUSE_INIT_PROJECT_PUBLIC_KEY}
      LANGFUSE_INIT_PROJECT_SECRET_KEY: \${LANGFUSE_INIT_PROJECT_SECRET_KEY}
      LANGFUSE_INIT_USER_EMAIL: \${LANGFUSE_INIT_USER_EMAIL}
      LANGFUSE_INIT_USER_NAME: admin
      LANGFUSE_INIT_USER_PASSWORD: \${LANGFUSE_INIT_USER_PASSWORD}

  clickhouse:
    image: docker.io/clickhouse/clickhouse-server
    restart: unless-stopped
    user: "101:101"
    environment:
      CLICKHOUSE_DB: default
      CLICKHOUSE_USER: clickhouse
      CLICKHOUSE_PASSWORD: \${CLICKHOUSE_PASSWORD}
    volumes:
      - langfuse_clickhouse_data:/var/lib/clickhouse
      - langfuse_clickhouse_logs:/var/log/clickhouse-server
    healthcheck:
      test: wget --no-verbose --tries=1 --spider http://localhost:8123/ping || exit 1
      interval: 5s
      timeout: 5s
      retries: 10
      start_period: 1s

  minio:
    image: cgr.dev/chainguard/minio
    restart: unless-stopped
    entrypoint: sh
    command: -c 'mkdir -p /data/langfuse && minio server --address ":9000" --console-address ":9001" /data'
    environment:
      MINIO_ROOT_USER: minio
      MINIO_ROOT_PASSWORD: \${MINIO_ROOT_PASSWORD}
    volumes:
      - langfuse_minio_data:/data
    healthcheck:
      test: ["CMD", "mc", "ready", "local"]
      interval: 1s
      timeout: 5s
      retries: 5
      start_period: 1s

  redis:
    image: docker.io/redis:7
    restart: unless-stopped
    command: >
      --requirepass \${REDIS_AUTH}
      --maxmemory-policy noeviction
    volumes:
      - langfuse_redis_data:/data
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 3s
      timeout: 10s
      retries: 10

  postgres:
    image: docker.io/postgres:17
    restart: unless-stopped
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U postgres"]
      interval: 3s
      timeout: 3s
      retries: 10
    environment:
      POSTGRES_USER: postgres
      POSTGRES_PASSWORD: \${POSTGRES_PASSWORD}
      POSTGRES_DB: postgres
      TZ: UTC
      PGTZ: UTC
    volumes:
      - langfuse_postgres_data:/var/lib/postgresql/data

volumes:
  cai_home:
    name: cai_home
  langfuse_postgres_data:
  langfuse_clickhouse_data:
  langfuse_clickhouse_logs:
  langfuse_minio_data:
  langfuse_redis_data:
EOF

DC="docker compose -f ${INSTALL_DIR}/docker-compose.yml"
ENV_FILE="${INSTALL_DIR}/.env"

# Upsert KEY=VALUE in .env so re-runs preserve already-generated secrets.
# Existing values win; only missing keys get filled in.
upsert_env() {
  local key="$1" val="$2"
  if [[ -f "$ENV_FILE" ]] && grep -q "^${key}=" "$ENV_FILE"; then
    return
  fi
  printf '%s=%s\n' "$key" "$val" >> "$ENV_FILE"
}

touch "$ENV_FILE"
chmod 600 "$ENV_FILE"

# Strong randoms for Langfuse + the data services it depends on. Once
# written they are sticky (re-running install.sh won't rotate them and
# break existing volumes).
upsert_env NEXTAUTH_SECRET                 "$(openssl rand -base64 32)"
upsert_env SALT                            "$(openssl rand -base64 32)"
upsert_env ENCRYPTION_KEY                  "$(openssl rand -hex 32)"
upsert_env POSTGRES_PASSWORD               "$(openssl rand -hex 32)"
upsert_env CLICKHOUSE_PASSWORD             "$(openssl rand -hex 32)"
upsert_env REDIS_AUTH                      "$(openssl rand -hex 32)"
upsert_env MINIO_ROOT_PASSWORD             "$(openssl rand -hex 32)"
upsert_env LANGFUSE_INIT_PROJECT_PUBLIC_KEY "pk-lf-$(openssl rand -hex 16)"
upsert_env LANGFUSE_INIT_PROJECT_SECRET_KEY "sk-lf-$(openssl rand -hex 16)"
upsert_env LANGFUSE_INIT_USER_EMAIL        "admin@cai.local"
upsert_env LANGFUSE_INIT_USER_PASSWORD     "$(openssl rand -base64 24)"

echo "Claude authentication:"
echo "  1) OAuth login (recommended; persisted in cai_home volume)"
echo "  2) Anthropic API key"
prompt AUTH_CHOICE "Choice" "1"

if [[ "$AUTH_CHOICE" == "2" ]]; then
  prompt API_KEY "Anthropic API key"
  upsert_env ANTHROPIC_API_KEY "$API_KEY"
fi

$DC pull
$DC up -d

if [[ "$AUTH_CHOICE" != "2" ]]; then
  echo
  echo "Launching claude REPL for OAuth login. Type /exit when done."
  $DC exec --user cai cai claude
fi

echo
echo "Authenticate the gh CLI as your GitHub user?"
echo "  Needed for 'gh pr/issue/api ...' calls and for git push in repos"
echo "  not bootstrapped with the cai GitHub App below."
prompt GH_LOGIN "Run 'gh auth login' now? [Y/n]" "y"

case "$GH_LOGIN" in
  n|N|no|NO) ;;
  *) $DC exec --user cai cai gh auth login ;;
esac

echo
echo "Configure cai as a GitHub App? (Optional)"
echo "  Lets cai push commits, open PRs and issues as 'cai[bot]' instead"
echo "  of your personal account."
prompt SETUP_BOT "Configure now? [y/N]" "n"

case "$SETUP_BOT" in
  y|Y|yes|YES)
    echo
    echo "[1/5] Register the App"
    echo "  Open: https://github.com/settings/apps/new"
    echo "  Required settings:"
    echo "    Name:                       cai (or cai-<yourhandle> if taken)"
    echo "    Homepage URL:               https://github.com/damien-robotsix/robotsix-cai"
    echo "    Webhook -> Active:          UNCHECK"
    echo "    Repository permissions:"
    echo "      Contents:                 Read & write"
    echo "      Pull requests:            Read & write"
    echo "      Issues:                   Read & write"
    echo "    Organization permissions:"
    echo "      Members:                  Read"
    echo "      Projects:                 Read & write"
    echo "    Where can it be installed:  Only on this account"
    echo
    prompt _CONFIRM "Press Enter once the App is created"

    while :; do
      prompt APP_ID "[2/5] App ID (numeric, top of the App page)"
      [[ "$APP_ID" =~ ^[0-9]+$ ]] && break
      echo "  App ID must be numeric."
    done

    echo
    echo "[3/5] Generate the App's private key"
    echo "  On the App page, scroll to 'Private keys' -> 'Generate a"
    echo "  private key'. A .pem file downloads to your machine."
    echo "  Provide its full path below (e.g. ~/Downloads/cai.<date>.private-key.pem)."
    while :; do
      prompt PEM_PATH "      Path to the downloaded .pem"
      # Expand a leading ~ since read does not.
      PEM_PATH="${PEM_PATH/#\~/$HOME}"
      if [[ ! -f "$PEM_PATH" ]]; then
        echo "  No file at: $PEM_PATH"
        continue
      fi
      if openssl rsa  -in "$PEM_PATH" -noout 2>/dev/null \
      || openssl pkey -in "$PEM_PATH" -noout 2>/dev/null; then
        break
      fi
      echo "  $PEM_PATH does not look like a PEM private key."
    done

    echo
    echo "[4/5] Saving credentials to /home/cai/.config/cai/ (cai_home volume)"
    $DC exec --user cai cai mkdir -p /home/cai/.config/cai
    $DC exec -T --user cai cai sh -c \
      'umask 077 && cat > /home/cai/.config/cai/github-app.pem' < "$PEM_PATH"
    $DC exec -T --user cai cai sh -c \
      "umask 077 && printf 'APP_ID=%s\n' '$APP_ID' > /home/cai/.config/cai/app.env"

    echo "  Validating with GitHub..."
    if ! $DC exec --user cai cai python -c "
from cai import CaiBot
info = CaiBot().verify()
print(f'  OK: authenticated as App {info[\"name\"]!r} (slug={info[\"slug\"]}, id={info[\"id\"]})')
"; then
      echo "  FAILED: GitHub rejected the App ID / private key combination."
      echo "  Re-run install.sh to retry."
      exit 1
    fi

    echo
    echo "[5/5] Install the App on the cai repo, then bootstrap"
    echo "  Open: https://github.com/apps/<your-app-slug> -> Install"
    echo "    (or: https://github.com/settings/installations)"
    echo "  Add: damien-robotsix/robotsix-cai (the repo cloned at /app)"
    echo
    prompt _CONFIRM "Press Enter once installed (Ctrl-C to skip)"
    if $DC exec --workdir /app --user cai cai cai-app-init; then
      echo "  OK: /app inside the container will now push as cai[bot]."
      echo "  For other repos, clone them and run 'cai-app-init' inside."
    else
      echo "  FAILED. Confirm the App is installed on damien-robotsix/robotsix-cai."
    fi
    ;;
esac

echo
echo "Add a 'cai' alias to your shell rc?"
echo "    cai     opens an interactive claude session in the container"
prompt ADD_ALIAS "Add alias? [y/N]" "n"

case "$ADD_ALIAS" in
  y|Y|yes|YES)
    USER_SHELL="$(basename "${SHELL:-/bin/bash}")"
    case "$USER_SHELL" in
      zsh)  RC_DEFAULT="${HOME}/.zshrc" ;;
      *)    RC_DEFAULT="${HOME}/.bashrc" ;;
    esac
    prompt RC_FILE "Shell rc file" "$RC_DEFAULT"

    ALIAS_LINE="alias cai='${DC} exec --user cai cai cai'"
    if grep -qF '# robotsix-cai alias' "$RC_FILE" 2>/dev/null; then
      sed -i '/^# robotsix-cai alias/,/^alias cai=/d' "$RC_FILE"
    fi
    {
      echo
      echo "# robotsix-cai alias (generated by install.sh)"
      echo "$ALIAS_LINE"
    } >> "$RC_FILE"
    echo "Wrote alias to $RC_FILE. Run 'source $RC_FILE' or open a new terminal."
    ;;
esac

echo
echo "Langfuse observability:"
echo "  UI:        http://localhost:3000"
echo "  Login as:  $(grep '^LANGFUSE_INIT_USER_EMAIL=' "$ENV_FILE" | cut -d= -f2-)"
echo "  Password:  $(grep '^LANGFUSE_INIT_USER_PASSWORD=' "$ENV_FILE" | cut -d= -f2-)"
echo "  (credentials are also in $ENV_FILE)"
echo
echo "Done. Try: cai"
