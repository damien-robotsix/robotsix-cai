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

ENV_FILE="${INSTALL_DIR}/.env"
DC="docker compose -f ${INSTALL_DIR}/docker-compose.yml"

# Upsert KEY=VALUE in .env so re-runs preserve already-set values.
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

echo "Langfuse server details:"
echo "  (host one yourself first — see docs/langfuse-server.md)"
prompt LF_BASE_URL "Base URL (e.g. https://langfuse.your-domain.com)"
prompt LF_PK "Project public key (pk-lf-...)"
prompt LF_SK "Project secret key (sk-lf-...)"
upsert_env LANGFUSE_BASE_URL    "$LF_BASE_URL"
upsert_env LANGFUSE_PUBLIC_KEY  "$LF_PK"
upsert_env LANGFUSE_SECRET_KEY  "$LF_SK"

cat > docker-compose.yml <<EOF
# cai client. Traces are shipped to the Langfuse server in .env
# (LANGFUSE_BASE_URL / LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY).

services:
  cai:
    image: robotsix/cai:${IMAGE_TAG}
    restart: unless-stopped
    env_file:
      - .env
    volumes:
      - cai_home:/home/cai

volumes:
  cai_home:
    name: cai_home
EOF

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
echo "  Sending traces to: $(grep '^LANGFUSE_BASE_URL=' "$ENV_FILE" | cut -d= -f2-)"
echo
echo "Done. Try: cai"
