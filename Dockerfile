FROM python:3.12-slim

# `gh auth login --web` complains about the missing browser opener even
# though its device flow works fine. Pointing BROWSER at echo silences
# the warning by "opening" URLs as plain text.
ENV BROWSER=echo

# Install Node (for claude-code), gh CLI from GitHub's apt repo, and
# the runtime utilities the CLIs shell out to.
# Acquire::Retries lets apt recover from transient mirror sync glitches
# (e.g. truncated .deb downloads) instead of failing the whole build.
RUN echo 'Acquire::Retries "5";' > /etc/apt/apt.conf.d/80-retries \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        nodejs \
        npm \
        ca-certificates \
        wget \
        gnupg \
        git \
        jq \
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

# Non-root user. claude-code refuses --dangerously-skip-permissions as
# root. UID 1000 matches the typical first-host-user UID so named
# volumes line up without extra chown.
RUN groupadd --system --gid 1000 cai \
    && useradd --system --gid cai --uid 1000 --create-home --shell /bin/bash cai \
    && mkdir -p /home/cai/.config/gh /home/cai/.claude \
    && chown -R cai:cai /home/cai

RUN pip install --no-cache-dir --root-user-action=ignore pydantic pydantic-settings

# /app is populated by cloning the repo at build time so the image ships
# with a real working tree + .git directory — interactive `docker exec`
# sessions can use git, inspect diffs, and commit/push from inside.
# CAI_GIT_REF defaults to main; CI passes the exact commit SHA for
# deterministic published images.
ARG CAI_GIT_URL=https://github.com/damien-robotsix/robotsix-cai.git
ARG CAI_GIT_REF=main

RUN mkdir -p /app && chown cai:cai /app

USER cai
WORKDIR /app

# Cache-bust the clone layer when the upstream ref moves: ADD on a URL
# uses the response ETag as cache key, so when main advances the clone
# layer is rebuilt. For pinned SHAs the JSON is stable and cache hits.
ADD "https://api.github.com/repos/damien-robotsix/robotsix-cai/commits/${CAI_GIT_REF}" /tmp/cai-git-ref.json

RUN git clone "${CAI_GIT_URL}" /app \
    && git -C /app checkout "${CAI_GIT_REF}" \
    && chmod +x /app/entrypoint.sh

CMD ["/app/entrypoint.sh"]
