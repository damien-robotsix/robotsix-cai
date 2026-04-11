# Phase A — first runnable container.
#
# Base image is python:3.12-slim. Node.js is installed via apt so we can
# install the @anthropic-ai/claude-code CLI globally. The backend itself
# is plain Python (stdlib only at this stage) and shells out to `claude -p`
# in autonomous mode.

FROM python:3.12-slim

# Slim images don't ship xdg-open / x-www-browser, so `gh auth login
# --web` emits a scary "Failed opening a web browser" error even
# though the device flow is working fine. Point BROWSER at `echo` so
# gh "opens" the URL by just printing it — same information, no noise.
ENV BROWSER=echo

# Pin the claude-code version so the self-improvement loop is reproducible.
# Bumping this should be a deliberate, reviewed change.
ARG CLAUDE_CODE_VERSION=2.1.96

# Pin supercronic (the in-container cron supervisor; Phase D onward).
# SHA256 is computed once against v0.2.44's linux-amd64 binary; bumping
# the version requires computing a new hash.
ARG SUPERCRONIC_VERSION=0.2.44
ARG SUPERCRONIC_SHA256=6feff7d5eba16a89cf229b7eb644cfae2f03a32c62ca320f17654659315275b6

# Install Node.js (Bookworm slim ships Node 18, which satisfies claude-code's
# >=18 requirement) plus npm, then install claude-code globally. Also
# installs the `gh` CLI from GitHub's official apt repository — the analyzer
# uses it to create issues from its findings (Phase C.2 onward).
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        nodejs \
        npm \
        ca-certificates \
        wget \
        gnupg \
    && mkdir -p -m 755 /etc/apt/keyrings \
    && wget -nv -O /etc/apt/keyrings/githubcli-archive-keyring.gpg \
        https://cli.github.com/packages/githubcli-archive-keyring.gpg \
    && chmod go+r /etc/apt/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
        > /etc/apt/sources.list.d/github-cli.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends gh \
    && rm -rf /var/lib/apt/lists/* \
    && npm install -g "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" \
    && claude --version \
    && gh --version

# Install supercronic — a cron-compatible scheduler built for containers.
# It runs as PID 1 via entrypoint.sh, forwards child stdout/stderr to its
# own stdout (so docker logs sees everything), and handles SIGTERM
# gracefully (lets in-flight tasks finish before exiting).
RUN wget -nv -O /usr/local/bin/supercronic \
        "https://github.com/aptible/supercronic/releases/download/v${SUPERCRONIC_VERSION}/supercronic-linux-amd64" \
    && echo "${SUPERCRONIC_SHA256}  /usr/local/bin/supercronic" | sha256sum -c - \
    && chmod +x /usr/local/bin/supercronic \
    && supercronic -version

# Create a non-root user. claude-code refuses
# `--dangerously-skip-permissions` when running as root
# ("cannot be used with root/sudo privileges for security reasons"),
# which we need so the fix and revise subagents can edit
# `.claude/agents/*.md` files (auto-improve self-modifies its own
# prompts). UID 1000 matches the typical first-host-user UID so the
# bind-mounted `./logs:/var/log/cai` directory works without extra
# host-side chowning.
#
# We pre-create the named-volume mount points with cai:cai ownership
# so Docker's "copy image contents into a new empty named volume on
# first mount" trick inherits the right ownership. Without
# pre-creating, the mount points get created at runtime as
# root:root and the cai user hits "permission denied":
#
#   - /home/cai/                  → cai_home          (the user's
#                                    entire home directory:
#                                    .claude/credentials, .claude.json
#                                    runtime config (sibling to
#                                    .claude/), .config/gh, session
#                                    transcripts under
#                                    .claude/projects/, etc. — one
#                                    volume for ALL claude-code and
#                                    gh user state.)
#   - /app/.claude/agent-memory/  → cai_agent_memory  (per-agent
#                                    durable memory across container
#                                    restarts; the /app agents
#                                    read/write it directly, the
#                                    cloned-worktree agents have it
#                                    copied in/out by the wrapper
#                                    around each invocation)
#
# We pre-create a few subdirs under /home/cai (.config/gh and
# .claude/projects) so they exist with cai ownership in the image,
# and Docker's volume copy preserves them. claude-code's runtime
# config files (.claude.json, .claude/.credentials.json, etc.) are
# created on first run inside the volume.
RUN groupadd --system --gid 1000 cai \
    && useradd --system --gid cai --uid 1000 --create-home --shell /bin/bash cai \
    && mkdir -p /var/log/cai /home/cai/.config/gh /home/cai/.claude/projects \
    && chown -R cai:cai /var/log/cai /home/cai

WORKDIR /app
COPY --chown=cai:cai cai.py /app/cai.py
COPY --chown=cai:cai parse.py /app/parse.py
COPY --chown=cai:cai publish.py /app/publish.py
COPY --chown=cai:cai .claude /app/.claude
COPY --chown=cai:cai entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh \
    && mkdir -p /app/.claude/agent-memory \
    && chown -R cai:cai /app

USER cai

CMD ["/app/entrypoint.sh"]
