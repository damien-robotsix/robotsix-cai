# Phase A — first runnable container.
#
# Base image is python:3.12-slim. Node.js is installed via apt so we can
# install the @anthropic-ai/claude-code CLI globally. The backend itself
# is plain Python (stdlib only at this stage) and shells out to `claude -p`
# in autonomous mode.

FROM python:3.12-slim

# Pin the claude-code version so the self-improvement loop is reproducible.
# Bumping this should be a deliberate, reviewed change.
ARG CLAUDE_CODE_VERSION=2.1.96

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

WORKDIR /app
COPY cai.py /app/cai.py
COPY parse.py /app/parse.py
COPY publish.py /app/publish.py
COPY prompts /app/prompts

CMD ["python", "/app/cai.py"]
