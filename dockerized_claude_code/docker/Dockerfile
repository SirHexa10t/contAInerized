# Base image — what every agent needs: bash/git/curl/ripgrep/python3 + uv (Python) +
# rich-cli (used by /print) + Claude Code itself. Tagged `claude-agents:base` by compose.
#
# Programming-oriented agents (filename includes [prog]) layer the heavier build
# environment on top via docker/Dockerfile.prog (which does `FROM claude-agents:base`).

FROM debian:bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    bash \
    git \
    curl \
    ca-certificates \
    gnupg \
    ripgrep \
    python3 \
    python3-pip \
    python3-venv \
    && rm -rf /var/lib/apt/lists/*

ARG HOST_UID=1000
RUN useradd -m -s /bin/bash -u ${HOST_UID} claude

USER claude

ARG SOFTWARE_STACK_REFRESH=bootstrap

RUN : "${SOFTWARE_STACK_REFRESH}" \
 && curl -LsSf https://astral.sh/uv/install.sh | sh

ENV PATH="/home/claude/.local/bin:$PATH"

# Pretty-printing CLI used by the `/print` skill (custom_skills/print/SKILL.md).
# Installed via uv's tool system into ~/.local/bin/ so it's on PATH for every shell.
RUN : "${SOFTWARE_STACK_REFRESH}" \
 && uv tool install rich-cli

RUN : "${SOFTWARE_STACK_REFRESH}" \
 && curl -fsSL https://claude.ai/install.sh | bash
ENV DISABLE_AUTOUPDATER=1
ENV CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1
ENV USE_BUILTIN_RIPGREP=0

WORKDIR /workspace
ENTRYPOINT ["claude"]
