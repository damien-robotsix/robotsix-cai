#!/usr/bin/env bash
#
# robotsix-cai installer
# ----------------------
#
# Generates a minimal docker-compose.yml configured for your chosen
# authentication mode. After this script finishes, you can `cd` into the
# install directory and `docker compose up` to run the container.
#
# Usage (download then run, recommended):
#
#   wget https://raw.githubusercontent.com/damien-robotsix/robotsix-cai/main/install.sh
#   less install.sh    # review before running
#   bash install.sh
#
# Or piped (skips review):
#
#   wget -qO- https://raw.githubusercontent.com/damien-robotsix/robotsix-cai/main/install.sh | bash
#
# Environment variables (optional):
#
#   INSTALL_DIR  Directory to install into. Default: ./robotsix-cai
#   IMAGE_TAG    Docker image tag to pin.   Default: latest

set -euo pipefail

INSTALL_DIR="${INSTALL_DIR:-$(pwd)/robotsix-cai}"
IMAGE_TAG="${IMAGE_TAG:-latest}"

# Resolve a TTY for interactive prompts. This makes the piped form
# (wget -qO- ... | bash) work — stdin is consumed by the pipe, so we
# read user input directly from the controlling terminal when one
# exists. If there's no controlling terminal (e.g. CI, headless),
# we fall back to whatever stdin is.
if (exec < /dev/tty) 2>/dev/null; then
  TTY=/dev/tty
else
  TTY=/dev/stdin
fi

# Prompts always print to stderr so they're visible even when stdout is
# redirected and even when /dev/tty isn't openable.
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
echo
echo "Install directory: $INSTALL_DIR"
echo "Image:             robotsix/cai:$IMAGE_TAG"
echo

mkdir -p "$INSTALL_DIR"
cd "$INSTALL_DIR"

if [[ -e docker-compose.yml ]]; then
  echo "[!] $INSTALL_DIR/docker-compose.yml exists; it will be overwritten."
  echo
fi

# Capture the host user's UID/GID. The generated compose passes these
# to the container (HOST_UID/HOST_GID env + user: "0:0"), and entrypoint.sh
# remaps the in-container 'cai' user to match before dropping privileges.
# This keeps bind-mounts and named volumes owned by the host user without
# needing a local image build or any host-side permission setup.
HOST_UID="$(id -u)"
HOST_GID="$(id -g)"
echo "[i] Host UID/GID detected: ${HOST_UID}:${HOST_GID} (entrypoint will remap at startup)"
echo

echo "How should the container authenticate to Claude?"
echo
echo "  1) Open the claude REPL inside the container — it auto-prompts"
echo "     for OAuth login on first start (recommended; credentials land"
echo "     in the cai_home volume and persist across restarts; no static"
echo "     secret stored in the container env. The installer opens the"
echo "     REPL for you automatically.)"
echo
echo "  2) Use an Anthropic API key (you'll be prompted to paste it)"
echo

prompt AUTH_CHOICE "Choice" "1"

echo
echo "Enable Watchtower for automatic updates?"
echo
echo "Watchtower is a small sidecar container that polls Docker Hub"
echo "every 12 hours and automatically pulls + restarts cai when a"
echo "new image is published. Recommended for hands-off operation."
echo
echo "WARNING: if cai is mid-fix when watchtower restarts it, the"
echo "in-flight fix will be killed and the issue may be left stuck"
echo "in auto-improve:in-progress. Manual relabel may be needed"
echo "until the audit feature (tracked separately) lands."
echo
prompt ENABLE_WATCHTOWER "Enable Watchtower? [y/N]" "n"

case "$ENABLE_WATCHTOWER" in
  y|Y|yes|Yes|YES)
    WATCHTOWER_SERVICE=$(cat <<'WATCHTOWER'

  watchtower:
    image: nickfedor/watchtower:1.16.1
    restart: unless-stopped
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock
    command:
      - --label-enable
      - --interval=43200
      - --cleanup
WATCHTOWER
)
    CAI_LABEL_BLOCK=$(cat <<'LABEL'
    labels:
      - "com.centurylinklabs.watchtower.enable=true"
LABEL
)
    ;;
  *)
    WATCHTOWER_SERVICE=""
    CAI_LABEL_BLOCK=""
    ;;
esac

# Prompt for GitHub admin logins — required for the human:solved unblock
# workflow.  Without CAI_ADMIN_LOGINS every human:solved label is ignored
# and parked issues/PRs silently stay parked.
echo
echo "Which GitHub logins can use the 'human:solved' label to unblock stuck"
echo "tasks? (see docs/configuration.md for details)"
echo
GH_DEFAULT_LOGIN="$(gh api user --jq .login 2>/dev/null || true)"
if [[ -n "$GH_DEFAULT_LOGIN" ]]; then
  prompt ADMIN_LOGINS "Admin GitHub logins (comma-separated)" "$GH_DEFAULT_LOGIN"
else
  prompt ADMIN_LOGINS "Admin GitHub logins (comma-separated, or press Enter to skip)"
fi
if [[ -n "$ADMIN_LOGINS" ]]; then
  CAI_ADMIN_ENV_LINE="      CAI_ADMIN_LOGINS: \"${ADMIN_LOGINS}\""
else
  CAI_ADMIN_ENV_LINE=""
fi

# Cross-host transcript sync — optional. When enabled, every machine
# running this container pushes its Claude Code session transcripts to
# a central SSH server, then pulls the union back before analyze/confirm.
# Single-host installs should skip this; only turn it on if you run
# cai against the same repo from multiple hosts and want the analyzer
# to see the full picture.
echo
echo "Enable cross-host transcript sync?"
echo
echo "Only relevant if you run cai for this repo on multiple machines"
echo "and want the self-improvement signal to reflect activity across"
echo "all of them. Requires an SSH-accessible server you own (e.g. a"
echo "VPS) to act as the shared transcript store."
echo
echo "Leave this disabled for single-host installs."
echo
prompt ENABLE_SYNC "Enable transcript sync? [y/N]" "n"

TRANSCRIPT_SYNC_ENV=""
TRANSCRIPT_SYNC_VOLUMES=""
case "$ENABLE_SYNC" in
  y|Y|yes|Yes|YES)
    echo
    echo "Pick the transport for the transcript store:"
    echo
    echo "  1) SSH — transcripts live on a remote server you own."
    echo "     This host rsyncs to it via SSH. Use this for laptops"
    echo "     and any host that isn't the one holding the store."
    echo
    echo "  2) Local — transcripts live on this host's filesystem,"
    echo "     bind-mounted into the container. Use this on the"
    echo "     host that IS the central store (e.g. the VPS itself),"
    echo "     so its own pushes/pulls avoid a pointless SSH loopback."
    echo
    prompt SYNC_MODE "Transport [1/2]" "1"

    case "$SYNC_MODE" in
      2)
        echo
        echo "Enter the absolute path on this host where transcripts live."
        echo "Created if missing. Bind-mounted into the container at the"
        echo "same path. The default lives under the install directory so"
        echo "no root / chown is needed."
        echo
        prompt SYNC_PATH "Local path" "${INSTALL_DIR}/transcripts"
        if [[ -z "$SYNC_PATH" ]]; then
          echo "ERROR: local path cannot be empty."
          exit 1
        fi
        if [[ "$SYNC_PATH" != /* ]]; then
          echo "ERROR: local path must be absolute (starts with /)."
          exit 1
        fi
        # The cai container runs as UID 1000, so the bind-mount target
        # must be owned by that UID. A plain host-side mkdir would
        # create it as whichever UID ran the installer, and a host
        # user that isn't 1000 then has to sudo chown before the
        # container can write. Sidestep that by letting docker itself
        # (which runs the helper as root) do mkdir + chown for us —
        # no sudo on the host.
        SYNC_PARENT="$(dirname "$SYNC_PATH")"
        SYNC_BASE="$(basename "$SYNC_PATH")"
        if [[ ! -d "$SYNC_PARENT" ]] && ! mkdir -p "$SYNC_PARENT" 2>/dev/null; then
          echo "ERROR: cannot create parent directory $SYNC_PARENT."
          echo "       Pick a path under a directory you can write to."
          exit 1
        fi
        echo "[i] Ensuring $SYNC_PATH exists and is owned by UID ${HOST_UID}:${HOST_GID} (via docker)..."
        if ! docker run --rm --user 0 \
             -v "${SYNC_PARENT}:/mnt" \
             -e TARGET="/mnt/${SYNC_BASE}" \
             -e OWNER="${HOST_UID}:${HOST_GID}" \
             alpine:3 \
             sh -c 'mkdir -p "$TARGET" && chown "$OWNER" "$TARGET"'; then
          echo "ERROR: failed to create/chown $SYNC_PATH via docker."
          exit 1
        fi

        SYNC_URL="$SYNC_PATH"
        TRANSCRIPT_SYNC_ENV=$(cat <<EOF
      CAI_TRANSCRIPT_SYNC_URL: "${SYNC_URL}"
      CAI_TRANSCRIPT_SYNC_SCHEDULE: "*/15 * * * *"
EOF
)
        # In local mode we bind-mount the host path at the same path
        # inside the container so the user sees consistent paths in
        # logs and docker exec sessions.
        TRANSCRIPT_SYNC_VOLUMES=$(cat <<EOF
      - ${SYNC_PATH}:${SYNC_PATH}
      - /etc/machine-id:/etc/host-machine-id:ro
EOF
)
        echo
        echo "[OK] Local-path transport configured (${SYNC_PATH})."
        echo "    No SSH key needed. Remote hosts should still SSH-push to"
        echo "    this server's $SYNC_PATH — see docs/configuration.md."
        echo
        ;;
      *)
        echo
        echo "Enter the SSH destination for the shared transcript store."
        echo "Format: <user>@<host>:<absolute-path>"
        echo "Example: cai@ovh.example.com:/srv/cai-transcripts"
        echo
        prompt SYNC_URL "Sync URL"
        if [[ -z "$SYNC_URL" ]]; then
          echo "ERROR: sync URL cannot be empty when sync is enabled."
          exit 1
        fi
        if [[ "$SYNC_URL" != *:* ]]; then
          echo "ERROR: SSH URL must contain ':' (user@host:/path). Did you mean local mode?"
          exit 1
        fi

        # Generate a dedicated ed25519 keypair just for transcript sync.
        # Kept separate from any user's personal keys so it can be rotated
        # or revoked without collateral damage.
        SYNC_KEY_PATH="${INSTALL_DIR}/cai_transcript_key"
        if [[ -f "$SYNC_KEY_PATH" ]]; then
          echo
          echo "[i] Reusing existing key at $SYNC_KEY_PATH"
        else
          echo
          echo "Generating a dedicated ed25519 keypair at $SYNC_KEY_PATH ..."
          ssh-keygen -t ed25519 -N '' -f "$SYNC_KEY_PATH" \
            -C "cai-transcript-sync@$(hostname)" >/dev/null
          echo "[OK] Key generated."
        fi

        # Mount permissions matter: the cai user inside the container runs
        # as UID 1000. If the host user isn't UID 1000, the bind-mount will
        # show up owned by a different UID and ssh will refuse to use it.
        # We chmod the file 600 (ssh's required perms) and rely on the
        # common case (host's first user = UID 1000 = matches cai).
        chmod 600 "$SYNC_KEY_PATH"
        chmod 644 "${SYNC_KEY_PATH}.pub"

        echo
        echo "==========================================================="
        echo "ACTION REQUIRED — install the public key on the sync server"
        echo "==========================================================="
        echo
        echo "Copy the following public key into the remote user's"
        echo "~/.ssh/authorized_keys on the sync server:"
        echo
        cat "${SYNC_KEY_PATH}.pub"
        echo
        echo "One-liner from this host (if you have password SSH access):"
        SYNC_USER_HOST="${SYNC_URL%:*}"
        echo "    ssh-copy-id -i ${SYNC_KEY_PATH}.pub ${SYNC_USER_HOST}"
        echo
        echo "Also create the transcript root on the server, e.g.:"
        SYNC_REMOTE_PATH="${SYNC_URL#*:}"
        echo "    ssh ${SYNC_USER_HOST} 'mkdir -p ${SYNC_REMOTE_PATH} && chmod 700 ${SYNC_REMOTE_PATH}'"
        echo
        echo "For automatic age/size cleanup, copy scripts/server-cleanup.sh"
        echo "to the server and wire it into its cron (see the script header"
        echo "for env vars and an example cron line)."
        echo
        prompt _SYNC_CONTINUE "Press Enter once the public key is installed" ""

        TRANSCRIPT_SYNC_ENV=$(cat <<EOF
      CAI_TRANSCRIPT_SYNC_URL: "${SYNC_URL}"
      CAI_TRANSCRIPT_SYNC_SCHEDULE: "*/15 * * * *"
EOF
)
        TRANSCRIPT_SYNC_VOLUMES=$(cat <<'EOF'
      - ./cai_transcript_key:/home/cai/.ssh/cai_transcript_key:ro
      - /etc/machine-id:/etc/host-machine-id:ro
EOF
)
        ;;
    esac
    ;;
  *)
    ;;
esac

case "$AUTH_CHOICE" in
  1)
    cat > docker-compose.yml <<YAML
# Generated by robotsix-cai install.sh
# Auth mode: in-container claude REPL OAuth (credentials in cai_home volume)
services:
  cai:
    image: robotsix/cai:${IMAGE_TAG}
    # Start as root so entrypoint.sh can remap the in-container cai user
    # to match HOST_UID/HOST_GID, then drop privileges. Filled in by
    # install.sh from \$(id -u)/\$(id -g).
    user: "0:0"
    restart: unless-stopped
    environment:
      # Host user UID/GID used by entrypoint.sh to remap the cai user
      # at startup so bind-mounts and named volumes are owned by the
      # host user. Captured by install.sh from the installer's shell.
      HOST_UID: "${HOST_UID}"
      HOST_GID: "${HOST_GID}"
      # Crontab expressions for the scheduled tasks (any valid
      # 5-field cron line — see https://crontab.guru/).
      #
      # CAI_CYCLE_SCHEDULE drives the fix pipeline on auto-improve:plan-approved
      # issues (fix → revise → review-pr → merge → confirm). A flock
      # in cmd_cycle serializes overlapping runs so issues are
      # processed one at a time. CAI_PLAN_ALL_SCHEDULE drives the
      # upstream refine → plan flow that turns :raised/:refined
      # issues into :planned for humans to approve. The remaining
      # schedules are for orthogonal tasks that run independently.
      CAI_CYCLE_SCHEDULE: "0 * * * *"        # hourly — fix pipeline on auto-improve:plan-approved
      CAI_PLAN_ALL_SCHEDULE: "30 * * * *"   # hourly @30 — drain :raised/:refined into :planned
      CAI_ANALYZER_SCHEDULE: "0 0 * * *"   # daily 00:00 UTC (LLM call)
      CAI_AUDIT_SCHEDULE: "0 */6 * * *"     # every 6h (Sonnet: LLM audit + deterministic cleanup; see README)
      CAI_CODE_AUDIT_SCHEDULE: "0 3 * * 0"  # weekly Sunday 03:00 UTC (Sonnet, code consistency)
      CAI_PROPOSE_SCHEDULE: "0 4 * * 0"    # weekly Sunday 04:00 UTC (creative improvement proposals)
      CAI_UPDATE_CHECK_SCHEDULE: "0 4 * * 1" # weekly Monday 04:00 UTC (Claude Code release check)
      CAI_EXTERNAL_SCOUT_SCHEDULE: "0 6 * * 1" # weekly Monday 06:00 UTC (scout for external libraries)
      CAI_HEALTH_REPORT_SCHEDULE: "0 7 * * 1" # weekly Monday 07:00 UTC (pipeline health report)
      CAI_COST_OPTIMIZE_SCHEDULE: "0 5 * * 0" # weekly Sunday 05:00 UTC (cost-reduction analysis)
      CAI_CHECK_WORKFLOWS_SCHEDULE: "0 */6 * * *" # every 6h (check for CI workflow failures)
      CAI_MERGE_CONFIDENCE_THRESHOLD: "high" # high | medium | disabled
      CAI_MERGE_MAX_DIFF_LEN: "200000"      # max chars of PR diff passed to merge agent
      CAI_TRANSCRIPT_WINDOW_DAYS: "7"       # only parse sessions from last N days
      CAI_TRANSCRIPT_MAX_FILES: "50"        # read at most N recent transcript files (0 = no limit)
${CAI_ADMIN_ENV_LINE}
${TRANSCRIPT_SYNC_ENV}
    volumes:
      # Persistent state for the cai user (Claude OAuth credentials,
      # session transcripts, gh config, claude-code's runtime
      # \`.claude.json\` config file, etc.). Mounted at /home/cai
      # rather than /home/cai/.claude so that BOTH the .claude/
      # directory AND its sibling .claude.json file are captured —
      # the latter is where claude-code stores runtime config and
      # would otherwise be lost on every container restart.
      #
      # Authenticate once with:
      #     docker compose run --rm -it --user cai cai claude
      # (the REPL auto-prompts for OAuth login on first start; exit
      # with /exit or Ctrl-D to flush credentials to disk)
      - cai_home:/home/cai
      # Persistent per-agent memory (\`.claude/agent-memory/<name>/\`)
      # so the durable notes each subagent accumulates across runs
      # survive container restarts.
      - cai_agent_memory:/app/.claude/agent-memory
      - cai_logs:/var/log/cai
${TRANSCRIPT_SYNC_VOLUMES}
${CAI_LABEL_BLOCK}${WATCHTOWER_SERVICE}

volumes:
  cai_home:
    name: cai_home
  cai_agent_memory:
    name: cai_agent_memory
  cai_logs:
    name: cai_logs
YAML
    echo
    echo "[OK] Wrote $INSTALL_DIR/docker-compose.yml (in-container OAuth mode)"
    ;;
  2)
    prompt API_KEY "Anthropic API key (sk-ant-...)"
    if [[ -z "$API_KEY" ]]; then
      echo "ERROR: API key cannot be empty."
      exit 1
    fi
    cat > docker-compose.yml <<YAML
# Generated by robotsix-cai install.sh
# Auth mode: API key from .env
services:
  cai:
    image: robotsix/cai:${IMAGE_TAG}
    # Start as root so entrypoint.sh can remap the in-container cai user
    # to match HOST_UID/HOST_GID, then drop privileges. Filled in by
    # install.sh from \$(id -u)/\$(id -g).
    user: "0:0"
    restart: unless-stopped
    env_file:
      - .env
    environment:
      # Host user UID/GID used by entrypoint.sh to remap the cai user
      # at startup so bind-mounts and named volumes are owned by the
      # host user. Captured by install.sh from the installer's shell.
      HOST_UID: "${HOST_UID}"
      HOST_GID: "${HOST_GID}"
      # Crontab expressions for the scheduled tasks (any valid
      # 5-field cron line — see https://crontab.guru/).
      #
      # CAI_CYCLE_SCHEDULE drives the fix pipeline on auto-improve:plan-approved
      # issues (fix → revise → review-pr → merge → confirm). A flock
      # in cmd_cycle serializes overlapping runs so issues are
      # processed one at a time. CAI_PLAN_ALL_SCHEDULE drives the
      # upstream refine → plan flow that turns :raised/:refined
      # issues into :planned for humans to approve. The remaining
      # schedules are for orthogonal tasks that run independently.
      CAI_CYCLE_SCHEDULE: "0 * * * *"        # hourly — fix pipeline on auto-improve:plan-approved
      CAI_PLAN_ALL_SCHEDULE: "30 * * * *"   # hourly @30 — drain :raised/:refined into :planned
      CAI_ANALYZER_SCHEDULE: "0 0 * * *"   # daily 00:00 UTC (LLM call)
      CAI_AUDIT_SCHEDULE: "0 */6 * * *"     # every 6h (Sonnet: LLM audit + deterministic cleanup; see README)
      CAI_CODE_AUDIT_SCHEDULE: "0 3 * * 0"  # weekly Sunday 03:00 UTC (Sonnet, code consistency)
      CAI_PROPOSE_SCHEDULE: "0 4 * * 0"    # weekly Sunday 04:00 UTC (creative improvement proposals)
      CAI_UPDATE_CHECK_SCHEDULE: "0 4 * * 1" # weekly Monday 04:00 UTC (Claude Code release check)
      CAI_EXTERNAL_SCOUT_SCHEDULE: "0 6 * * 1" # weekly Monday 06:00 UTC (scout for external libraries)
      CAI_HEALTH_REPORT_SCHEDULE: "0 7 * * 1" # weekly Monday 07:00 UTC (pipeline health report)
      CAI_COST_OPTIMIZE_SCHEDULE: "0 5 * * 0" # weekly Sunday 05:00 UTC (cost-reduction analysis)
      CAI_CHECK_WORKFLOWS_SCHEDULE: "0 */6 * * *" # every 6h (check for CI workflow failures)
      CAI_MERGE_CONFIDENCE_THRESHOLD: "high" # high | medium | disabled
      CAI_MERGE_MAX_DIFF_LEN: "200000"      # max chars of PR diff passed to merge agent
      CAI_TRANSCRIPT_WINDOW_DAYS: "7"       # only parse sessions from last N days
      CAI_TRANSCRIPT_MAX_FILES: "50"        # read at most N recent transcript files (0 = no limit)
${CAI_ADMIN_ENV_LINE}
${TRANSCRIPT_SYNC_ENV}
    volumes:
      # Persistent state for the cai user (Claude transcripts, gh
      # config, claude-code's runtime \`.claude.json\`, etc.).
      # API-key auth means the volume can stay empty for credentials,
      # but transcripts and other claude-code config still
      # accumulate here.
      - cai_home:/home/cai
      # Persistent per-agent memory (\`.claude/agent-memory/<name>/\`)
      # so the durable notes each subagent accumulates across runs
      # survive container restarts.
      - cai_agent_memory:/app/.claude/agent-memory
      - cai_logs:/var/log/cai
${TRANSCRIPT_SYNC_VOLUMES}
${CAI_LABEL_BLOCK}${WATCHTOWER_SERVICE}

volumes:
  cai_home:
    name: cai_home
  cai_agent_memory:
    name: cai_agent_memory
  cai_logs:
    name: cai_logs
YAML
    cat > .env <<ENV
ANTHROPIC_API_KEY=${API_KEY}
ENV
    [[ -n "${ADMIN_LOGINS:-}" ]] && printf 'CAI_ADMIN_LOGINS=%s\n' "${ADMIN_LOGINS}" >> .env
    chmod 600 .env
    echo
    echo "[OK] Wrote $INSTALL_DIR/docker-compose.yml (API-key mode)"
    echo "[OK] Wrote $INSTALL_DIR/.env with your API key (chmod 600)"
    ;;
  *)
    echo "ERROR: invalid choice '$AUTH_CHOICE'."
    exit 1
    ;;
esac

echo
echo "Pulling the cai image..."
echo
if ! docker compose pull; then
  echo
  echo "[!] 'docker compose pull' failed. If you're developing locally, you"
  echo "    can build from source instead: 'docker compose build'"
  exit 1
fi

# Wipe any existing cai volumes so the install starts from a clean
# state. This is important on re-install / upgrade because earlier
# versions used different volume layouts (cai_claude, cai_gh_config,
# cai_transcripts) that no longer match the current
# (cai_home + cai_agent_memory) layout. Stale volumes can also have
# wrong-ownership files left over from when the container ran as
# root or as a different HOST_UID. Easier to wipe and start fresh
# than to migrate.
#
# `docker compose down --volumes --remove-orphans` first to stop any
# running cai / watchtower containers from a prior install — without
# this, the `docker volume rm` below silently fails with "volume in
# use" and the subsequent `docker compose run --user cai` commands
# hit a stale, already-chowned /home/cai they can't write to.
echo
echo "Stopping any running cai containers and wiping volumes for a clean install..."
docker compose down --volumes --remove-orphans 2>/dev/null || true
for vol in cai_home cai_agent_memory cai_logs cai_claude cai_gh_config cai_transcripts; do
  if docker volume inspect "$vol" >/dev/null 2>&1; then
    if docker volume rm "$vol" >/dev/null 2>&1; then
      echo "  removed: $vol"
    else
      echo "  [!] failed to remove $vol (still in use? stop other containers using it first)"
    fi
  fi
done
echo

echo
echo "Authenticating gh inside the container (interactive)."
echo "Credentials persist in the cai_home volume so subsequent"
echo "'docker compose up' runs don't need to re-authenticate."
echo
echo "When prompted, pick:"
echo "  * 'GitHub.com'"
echo "  * 'HTTPS'"
echo "  * Authenticate via web browser (easiest on a headless server —"
echo "    gh prints a one-time code and a URL to open)"
echo

# 'docker compose run' needs a real TTY for the interactive prompts.
# The piped form of the installer (wget -qO- | bash) consumes stdin, so
# we redirect stdin from /dev/tty when we have one. Without a TTY, we
# fall back to printing the command and letting the user run it.
if [[ "$TTY" == "/dev/tty" ]]; then
  if ! docker compose run --rm --user cai cai gh auth login --git-protocol https < /dev/tty; then
    echo
    echo "[!] gh auth login did not complete. Rerun it yourself:"
    echo "      cd $INSTALL_DIR && docker compose run --rm --user cai cai gh auth login --git-protocol https"
    exit 1
  fi
  echo
  echo "[OK] gh is authenticated. Credentials persisted in cai_home."

  # Configure git identity inside the container so commits are
  # attributed correctly. Default to the GitHub user's name and
  # email (from `gh api user`), let the user override.
  echo
  echo "Configuring git identity inside the container."
  echo
  GH_USER_NAME="$(docker compose run --rm --user cai cai gh api user --jq .name 2>/dev/null || true)"
  GH_USER_EMAIL="$(docker compose run --rm --user cai cai gh api user --jq .email 2>/dev/null || true)"
  prompt GIT_USER_NAME "Git user name" "${GH_USER_NAME:-}"
  prompt GIT_USER_EMAIL "Git user email" "${GH_USER_EMAIL:-}"
  if [[ -n "$GIT_USER_NAME" ]]; then
    docker compose run --rm --user cai cai git config --global user.name "$GIT_USER_NAME"
  fi
  if [[ -n "$GIT_USER_EMAIL" ]]; then
    docker compose run --rm --user cai cai git config --global user.email "$GIT_USER_EMAIL"
  fi
  if [[ -n "$GIT_USER_NAME" || -n "$GIT_USER_EMAIL" ]]; then
    echo "[OK] Git identity configured (persisted in cai_home)."
  else
    echo "[!] No git identity set. Configure it later with:"
    echo "      docker compose exec --user cai cai git config --global user.name 'Your Name'"
    echo "      docker compose exec --user cai cai git config --global user.email 'you@example.com'"
  fi

  # Same pattern for claude — only relevant in OAuth mode (AUTH_CHOICE=1).
  # API-key mode (AUTH_CHOICE=2) uses ANTHROPIC_API_KEY from .env, no
  # interactive login needed.
  #
  # We open the claude REPL with `docker compose run --rm -it --user cai cai claude`.
  # On first start (no credentials in the cai_home volume), the REPL
  # automatically prompts for OAuth login — the user completes the
  # browser flow, claude saves credentials to the volume, and the
  # user exits the REPL.
  #
  # Why the REPL form instead of `claude auth login` (the obvious
  # CLI subcommand): the latter does NOT work reliably under
  # `docker compose run -it < /dev/tty`. The CLI subcommand's input
  # mechanism doesn't play well with the pseudo-TTY layer — the user
  # pastes the authorization code and it silently never reaches claude.
  # The REPL uses the full terminal handling code path that does
  # propagate the paste correctly.
  if [[ "$AUTH_CHOICE" == "1" ]]; then
    echo
    echo "Authenticating claude inside the container (interactive)."
    echo "Credentials persist in the cai_home volume so subsequent"
    echo "'docker compose up' runs don't need to re-authenticate."
    echo
    echo "We're about to open the claude REPL. On first start, it will"
    echo "automatically prompt you to log in:"
    echo "  1. The REPL opens a browser to the OAuth page"
    echo "  2. Complete the sign-in and copy the authorization code"
    echo "  3. Paste the code back into the REPL and press Enter"
    echo "  4. Once login is confirmed, exit the REPL gracefully:"
    echo "       type /exit, or press Ctrl-D"
    echo "     (Ctrl-C also works but may interrupt before claude flushes"
    echo "      the credentials and config to disk — use it only if"
    echo "      nothing else does)"
    echo
    echo "Press Enter when you're ready..."
    read -r _ < /dev/tty || true
    if ! docker compose run --rm -it --user cai cai claude --dangerously-skip-permissions < /dev/tty; then
      # The REPL may exit non-zero (Ctrl-C, Ctrl-D, etc.); don't
      # treat that as an error — it auto-prompts on next start.
      :
    fi
    echo
  fi

  # Offer to create shell aliases so the user can type `cai` from
  # anywhere to open an interactive claude session, and `cai-*` to
  # trigger pipeline commands without docker compose boilerplate.
  DC="docker compose -f ${INSTALL_DIR}/docker-compose.yml"
  echo
  echo "Would you like to add shell aliases for cai?"
  echo
  echo "This adds aliases to your shell rc file so you can run:"
  echo "    cai                       # interactive claude session"
  echo "    cai -p 'fix the bug'      # one-shot prompt"
  echo "    cai-cycle                  # run one fix-pipeline cycle"
  echo "    cai-audit                  # run the queue/PR audit"
  echo "    cai-analyze                # run the transcript analyzer"
  echo "    cai-logs                   # tail container logs"
  echo "    cai-exec <cmd>             # run any command in the container"
  echo
  prompt CREATE_ALIAS "Add aliases? [y/N]" "n"

  case "$CREATE_ALIAS" in
    y|Y|yes|Yes|YES)
      # Detect the user's shell and pick the right rc file.
      USER_SHELL="$(basename "${SHELL:-/bin/bash}")"
      case "$USER_SHELL" in
        zsh)  RC_FILE="${HOME}/.zshrc" ;;
        *)    RC_FILE="${HOME}/.bashrc" ;;
      esac

      prompt RC_CHOICE "Shell rc file" "$RC_FILE"

      ALIAS_BLOCK=$(cat <<ALIASES
# robotsix-cai aliases (generated by install.sh)
alias cai='${DC} exec --user cai cai claude --dangerously-skip-permissions'
alias cai-cycle='${DC} exec --user cai cai python /app/cai.py cycle'
alias cai-audit='${DC} exec --user cai cai python /app/cai.py audit'
alias cai-analyze='${DC} exec --user cai cai python /app/cai.py analyze'
alias cai-dispatch='${DC} exec --user cai cai python /app/cai.py dispatch'
alias cai-verify='${DC} exec --user cai cai python /app/cai.py verify'
alias cai-cost='${DC} exec --user cai cai python /app/cai.py cost-report'
alias cai-health='${DC} exec --user cai cai python /app/cai.py health-report --dry-run'
alias cai-logs='${DC} logs -f cai'
alias cai-exec='${DC} exec --user cai cai'
ALIASES
)

      # Check whether the exact alias block already exists in the rc file.
      if grep -qF '# robotsix-cai aliases' "$RC_CHOICE" 2>/dev/null; then
        EXISTING="$(sed -n '/^# robotsix-cai aliases/,/^alias cai-exec=/p' "$RC_CHOICE")"
        if [[ "$EXISTING" == "$ALIAS_BLOCK" ]]; then
          echo "[OK] cai aliases already up-to-date in $RC_CHOICE — no changes made."
        else
          sed -i '/^# robotsix-cai aliases/,/^alias cai-exec=/d' "$RC_CHOICE"
          printf '\n%s\n' "$ALIAS_BLOCK" >> "$RC_CHOICE"
          echo "[OK] Replaced existing cai aliases in $RC_CHOICE"
        fi
      else
        printf '\n%s\n' "$ALIAS_BLOCK" >> "$RC_CHOICE"
        echo "[OK] Added cai aliases to $RC_CHOICE"
      fi
      echo "     Run 'source $RC_CHOICE' or open a new terminal to use them."
      ;;
    *)
      echo
      echo "Skipped. You can always add them manually later:"
      echo "  alias cai='${DC} exec --user cai cai claude --dangerously-skip-permissions'"
      echo "  alias cai-cycle='${DC} exec --user cai cai python /app/cai.py cycle'"
      echo "  alias cai-audit='${DC} exec --user cai cai python /app/cai.py audit'"
      ;;
  esac

  echo
  echo "Next steps:"
  echo "  cd $INSTALL_DIR"
  echo "  docker compose up -d                    # start the scheduler"
  echo "  docker compose logs -f cai              # watch the first cycle"
  echo
  echo "Override the schedule by editing docker-compose.yml's"
  echo "CAI_ANALYZER_SCHEDULE env var (any valid cron expression)."
  echo
  echo "Trigger an ad-hoc analyzer run without waiting for the tick:"
  echo "  docker compose exec --user cai cai python /app/cai.py analyze"
else
  echo "[!] No controlling TTY — skipping the interactive login."
  echo "    Finish authentication yourself before the first run:"
  echo "      cd $INSTALL_DIR"
  echo "      docker compose run --rm --user cai cai gh auth login --git-protocol https"
  echo "      docker compose run --rm --user cai cai git config --global user.name 'Your Name'"
  echo "      docker compose run --rm --user cai cai git config --global user.email 'you@example.com'"
  if [[ "$AUTH_CHOICE" == "1" ]]; then
    echo "      docker compose run --rm -it --user cai cai claude --dangerously-skip-permissions    # then complete the OAuth prompt"
  fi
  echo "      docker compose up -d"
fi
