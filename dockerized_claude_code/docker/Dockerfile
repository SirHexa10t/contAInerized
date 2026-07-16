# Base image — what every agent needs: bash/git/curl + ripgrep/fd/tree (search) +
# jq/yq (data) + python3 + uv (Python) + rich-cli (used by /print) + Claude Code
# itself. Tagged `claude-agents:base` by compose.
#
# Programming-oriented agents (filename includes [code]) layer the heavier build
# environment on top via docker/Dockerfile.code (which does `FROM claude-agents:base`).
#
# trixie (Debian 13) ships python3.13 — the tags-rewrite code requires ≥3.12
# in-container, because the dev loop runs the launcher's test suite inside
# agent containers.

FROM debian:trixie-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    bash \
    git \
    curl \
    ca-certificates \
    gnupg \
    ripgrep \
    fd-find \
    tree \
    jq \
    python3 \
    python3-pip \
    python3-venv \
    && rm -rf /var/lib/apt/lists/*

# Debian ships fd's binary as `fdfind` to avoid a name collision with an old
# package — symlink it to the upstream name so muscle-memory `fd` invocations work.
RUN ln -s "$(command -v fdfind)" /usr/local/bin/fd

# mikefarah/yq — Go binary from GitHub releases. /latest/download/ resolves to
# the current stable; rebuilding this layer pulls a fresh binary.
RUN curl -fsSL "https://github.com/mikefarah/yq/releases/latest/download/yq_linux_$(dpkg --print-architecture)" \
      -o /usr/local/bin/yq \
 && chmod +x /usr/local/bin/yq

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
