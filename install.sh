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

# Read VALUE for KEY from .env (empty string if not present).
read_env() {
  local key="$1"
  [[ -f "$ENV_FILE" ]] || return 0
  grep -m1 "^${key}=" "$ENV_FILE" 2>/dev/null | cut -d= -f2- || true
}

# Set KEY=VALUE in .env, replacing any existing value. Used for credentials
# that rotate (e.g. Claude OAuth access tokens) so re-running install.sh
# refreshes the value rather than silently keeping a stale one.
set_env() {
  local key="$1" val="$2"
  if [[ -f "$ENV_FILE" ]] && grep -q "^${key}=" "$ENV_FILE"; then
    # Use a delimiter unlikely to appear in tokens (|), and escape any
    # forward slashes / pipes in val for sed safety.
    local escaped
    escaped=$(printf '%s' "$val" | sed -e 's/[\/&|]/\\&/g')
    sed -i "s|^${key}=.*|${key}=${escaped}|" "$ENV_FILE"
  else
    printf '%s=%s\n' "$key" "$val" >> "$ENV_FILE"
  fi
}

touch "$ENV_FILE"
chmod 600 "$ENV_FILE"

echo "Langfuse server details:"
echo "  (host one yourself first — see docs/langfuse-server.md)"

_lf_url=$(read_env LANGFUSE_BASE_URL)
_lf_pk=$(read_env LANGFUSE_PUBLIC_KEY)
_lf_sk=$(read_env LANGFUSE_SECRET_KEY)

if [[ -n "$_lf_url" && -n "$_lf_pk" && -n "$_lf_sk" ]]; then
  echo "  Existing Langfuse credentials found in .env ($( printf '%s' "$_lf_url"))."
  prompt _LF_RECONFIG "Reconfigure? [y/N]" "n"
  case "$_LF_RECONFIG" in
    y|Y|yes|YES)
      prompt LF_BASE_URL "Base URL" "$_lf_url"
      prompt LF_PK      "Project public key (pk-lf-...)" "$_lf_pk"
      prompt LF_SK      "Project secret key (sk-lf-...)" "$_lf_sk"
      set_env LANGFUSE_BASE_URL   "$LF_BASE_URL"
      set_env LANGFUSE_PUBLIC_KEY "$LF_PK"
      set_env LANGFUSE_SECRET_KEY "$LF_SK"
      ;;
    *)
      LF_BASE_URL="$_lf_url"
      ;;
  esac
else
  prompt LF_BASE_URL "Base URL (e.g. https://langfuse.your-domain.com)"
  prompt LF_PK "Project public key (pk-lf-...)"
  prompt LF_SK "Project secret key (sk-lf-...)"
  set_env LANGFUSE_BASE_URL   "$LF_BASE_URL"
  set_env LANGFUSE_PUBLIC_KEY "$LF_PK"
  set_env LANGFUSE_SECRET_KEY "$LF_SK"
fi

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

_existing_token=$(read_env CLAUDE_CODE_OAUTH_TOKEN)
_existing_api_key=$(read_env ANTHROPIC_API_KEY)
_skip_claude_auth=0

if [[ -n "$_existing_token" && "$_existing_token" == sk-ant-oat01-* ]]; then
  echo "  Existing Claude OAuth token found in .env."
  prompt _REAUTH "Re-authenticate? [y/N]" "n"
  case "$_REAUTH" in
    y|Y|yes|YES) ;;
    *) _skip_claude_auth=1; AUTH_CHOICE="1" ;;
  esac
elif [[ -n "$_existing_api_key" ]]; then
  echo "  Existing Anthropic API key found in .env."
  prompt _REAUTH "Reconfigure API key? [y/N]" "n"
  case "$_REAUTH" in
    y|Y|yes|YES) ;;
    *) _skip_claude_auth=1; AUTH_CHOICE="2" ;;
  esac
fi

if [[ "$_skip_claude_auth" -eq 0 ]]; then
  echo "  1) Claude subscription via OAuth (recommended; mints a 1-year token,"
  echo "     bills against your Pro/Max plan quota — no per-token charges)"
  echo "  2) Anthropic API key (pay-per-token)"
  prompt AUTH_CHOICE "Choice" "1"

  if [[ "$AUTH_CHOICE" == "2" ]]; then
    prompt API_KEY "Anthropic API key"
    upsert_env ANTHROPIC_API_KEY "$API_KEY"
  fi
fi

$DC pull
$DC up -d

if [[ "$_skip_claude_auth" -eq 0 && "$AUTH_CHOICE" != "2" ]]; then
  echo
  echo "Minting a long-lived (1-year) Claude OAuth token..."
  echo "Follow the OAuth prompts inside the container — the token will be captured"
  echo "automatically and written to .env as CLAUDE_CODE_OAUTH_TOKEN."

  # Write a capture wrapper to the container that runs 'claude setup-token'
  # under a PTY, forwards I/O to the user's terminal so the interactive auth
  # works, and writes the extracted sk-ant-oat01-... token to a temp file.
  $DC exec -T --user cai cai bash -c 'cat > /tmp/_cai_setup_token.py' <<'PY'
import os
import pty
import re
import sys

captured = bytearray()


def master_read(fd):
    data = os.read(fd, 4096)
    captured.extend(data)
    return data


exit_status = pty.spawn(["claude", "setup-token"], master_read)

text = bytes(captured).decode("utf-8", "replace")
# Strip ANSI escape sequences so colour codes don't break the regex.
clean = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", text)
match = re.search(r"sk-ant-oat01-[A-Za-z0-9_-]+", clean)

with open("/tmp/_cai_oauth_token", "w") as f:
    if match:
        f.write(match.group())

sys.exit(exit_status if isinstance(exit_status, int) else 0)
PY

  # Redirect stdin from the resolved TTY so 'curl | bash' invocations don't
  # leave docker compose exec without a real TTY for the OAuth prompts.
  $DC exec --user cai cai python3 /tmp/_cai_setup_token.py < "$TTY"

  TOKEN=$($DC exec -T --user cai cai cat /tmp/_cai_oauth_token 2>/dev/null || true)
  $DC exec -T --user cai cai rm -f /tmp/_cai_setup_token.py /tmp/_cai_oauth_token

  if [[ -z "$TOKEN" ]]; then
    echo "  Failed to extract OAuth token from 'claude setup-token' output."
    echo "  Re-run install.sh once you've completed the auth flow successfully."
    exit 1
  fi

  set_env CLAUDE_CODE_OAUTH_TOKEN "$TOKEN"
  echo "  CLAUDE_CODE_OAUTH_TOKEN written to .env. Restarting cai..."
  $DC up -d --force-recreate
fi

echo
echo "Authenticate the gh CLI as your GitHub user?"
echo "  Needed for 'gh pr/issue/api ...' calls and for git push in repos"
echo "  not bootstrapped with the cai GitHub App below."

if $DC exec -T --user cai cai gh auth status >/dev/null 2>&1; then
  echo "  Already authenticated with gh CLI."
  prompt GH_LOGIN "Re-authenticate? [y/N]" "n"
else
  prompt GH_LOGIN "Run 'gh auth login' now? [Y/n]" "y"
fi

case "$GH_LOGIN" in
  n|N|no|NO) ;;
  *) $DC exec --user cai cai gh auth login ;;
esac

echo
echo "Configure cai as a GitHub App? (Optional)"
echo "  Lets cai push commits, open PRs and issues as 'cai[bot]' instead"
echo "  of your personal account."

if $DC exec -T --user cai cai test -f /home/cai/.config/cai/github-app.pem 2>/dev/null; then
  echo "  Existing GitHub App configuration found in the container."
  prompt SETUP_BOT "Reconfigure? [y/N]" "n"
else
  prompt SETUP_BOT "Configure now? [y/N]" "n"
fi

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
