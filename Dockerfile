FROM python:3.12-slim

# `gh auth login --web` complains about the missing browser opener even
# though its device flow works fine. Pointing BROWSER at echo silences
# the warning by "opening" URLs as plain text.
ENV BROWSER=echo

# Install Node (for claude-code), gh CLI from GitHub's apt repo, and
# the runtime utilities the CLIs shell out to.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        nodejs \
        npm \
        ca-certificates \
        wget \
        gnupg \
        git \
        jq \
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

# Non-root user. claude-code refuses --dangerously-skip-permissions as
# root. UID 1000 matches the typical first-host-user UID so named
# volumes line up without extra chown.
RUN groupadd --system --gid 1000 cai \
    && useradd --system --gid cai --uid 1000 --create-home --shell /bin/bash cai \
    && mkdir -p /home/cai/.config/gh /home/cai/.claude /home/cai/.ssh \
    && chmod 700 /home/cai/.ssh \
    && chown -R cai:cai /home/cai

RUN pip install --no-cache-dir --root-user-action=ignore pydantic pydantic-settings

USER cai
WORKDIR /app

COPY --chown=cai:cai --chmod=755 entrypoint.sh /app/
COPY --chown=cai:cai cai.py /app/

CMD ["/app/entrypoint.sh"]
