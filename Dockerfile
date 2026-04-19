# Phase A — first runnable container.
#
# Base image is python:3.12-slim. Node.js is installed via apt so we can
# install the @anthropic-ai/claude-code CLI globally. The backend is plain
# Python; runtime dependencies are declared in pyproject.toml's
# [project].dependencies and installed via `pip install` after the clone
# step below. It shells out to `claude -p` in autonomous mode.

FROM python:3.12-slim

# Slim images don't ship xdg-open / x-www-browser, so `gh auth login
# --web` emits a scary "Failed opening a web browser" error even
# though the device flow is working fine. Point BROWSER at `echo` so
# gh "opens" the URL by just printing it — same information, no noise.
ENV BROWSER=echo

# Pin supercronic (the in-container cron supervisor; Phase D onward).
# SHA256 is computed once against v0.2.44's linux-amd64 binary; bumping
# the version requires computing a new hash.
ARG SUPERCRONIC_VERSION=0.2.44
ARG SUPERCRONIC_SHA256=6feff7d5eba16a89cf229b7eb644cfae2f03a32c62ca320f17654659315275b6

# Install Node.js (Bookworm slim ships Node 18, which satisfies claude-code's
# >=18 requirement) plus npm, then install claude-code globally. Also
# installs the `gh` CLI from GitHub's official apt repository — the analyzer
# uses it to create issues from its findings (Phase C.2 onward). `git` is
# required because `/app` is a live clone (see the git clone step below),
# not a COPY of the build context.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        nodejs \
        npm \
        ca-certificates \
        wget \
        gnupg \
        git \
        rsync \
        openssh-client \
    && mkdir -p -m 755 /etc/apt/keyrings \
    && wget -nv -O /etc/apt/keyrings/githubcli-archive-keyring.gpg \
        https://cli.github.com/packages/githubcli-archive-keyring.gpg \
    && chmod go+r /etc/apt/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
        > /etc/apt/sources.list.d/github-cli.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends gh \
    && rm -rf /var/lib/apt/lists/* \
    && npm install -g "@anthropic-ai/claude-code@latest" \
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
# which we need so the implement and revise subagents can edit
# `.claude/agents/*.md` files (auto-improve self-modifies its own
# prompts). UID 1000 matches the typical first-host-user UID so the
# named `cai_logs` volume mounted at `/var/log/cai` works without
# extra host-side chowning.
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
#                                    read/write it directly, as do
#                                    the cloned-worktree agents that
#                                    have memory tracking via the
#                                    mounted volume; cai-rebase is
#                                    excluded — it has no memory
#                                    tracking by design)
#   - /var/log/cai/               → cai_logs          (run log — one
#                                    key=value line per cai invocation;
#                                    named volume avoids host permission
#                                    issues that a bind-mount causes)
#
# We pre-create a few subdirs under /home/cai (.config/gh and
# .claude/projects) so they exist with cai ownership in the image,
# and Docker's volume copy preserves them. claude-code's runtime
# config files (.claude.json, .claude/.credentials.json, etc.) are
# created on first run inside the volume.
RUN groupadd --system --gid 1000 cai \
    && useradd --system --gid cai --uid 1000 --create-home --shell /bin/bash cai \
    && mkdir -p /var/log/cai /home/cai/.config/gh /home/cai/.claude/projects /home/cai/.ssh \
    && chmod 700 /home/cai/.ssh \
    && chown -R cai:cai /var/log/cai /home/cai

# `/app` is populated by cloning the repo at build time instead of
# copying the build context. This gives the image a real `.git` directory
# so interactive `docker exec <container> claude` sessions can use git,
# inspect diffs, and commit/push feature branches — matching the
# "develop in the container from a clean source" workflow.
#
# CAI_GIT_REF defaults to `main` so local `docker compose build` picks
# up the current tip of main. CI (docker-publish.yml) passes the exact
# commit SHA that triggered the workflow, so published images pin
# deterministically.
ARG CAI_GIT_URL=https://github.com/damien-robotsix/robotsix-cai.git
ARG CAI_GIT_REF=main

RUN mkdir -p /app && chown cai:cai /app

USER cai
WORKDIR /app

# Cache-bust the clone layer when the upstream ref moves. Docker's ADD
# with a URL uses the response ETag as cache key, so when `main` advances
# (or CAI_GIT_REF points at a fresh commit) the subsequent `git clone`
# layer is rebuilt. For pinned SHAs the JSON is stable and the cache
# behaves normally.
ADD "https://api.github.com/repos/damien-robotsix/robotsix-cai/commits/${CAI_GIT_REF}" /tmp/cai-git-ref.json

RUN git clone "${CAI_GIT_URL}" /app \
    && git -C /app checkout "${CAI_GIT_REF}" \
    && chmod +x /app/entrypoint.sh \
    && mkdir -p /app/.claude/agent-memory/shared

# Install Python runtime dependencies declared in pyproject.toml.
# We install system-wide (as root) rather than per-user so the cai user
# and any future root-invoked scripts both pick them up from site-packages.
# The project itself is not installed — cai.py runs directly from /app —
# so we extract the dependencies list and feed it to pip via a requirements
# file, keeping pyproject.toml as the single source of truth.
USER root
RUN python -c "import tomllib; print('\n'.join(tomllib.load(open('/app/pyproject.toml','rb'))['project']['dependencies']))" \
        > /tmp/requirements.txt \
    && pip install --no-cache-dir --root-user-action=ignore -r /tmp/requirements.txt \
    && rm /tmp/requirements.txt
USER cai

CMD ["/app/entrypoint.sh"]
