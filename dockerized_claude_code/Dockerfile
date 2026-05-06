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
    build-essential \
    pkg-config \
    libssl-dev \
    && rm -rf /var/lib/apt/lists/*

# Node.js (current LTS) + npm via NodeSource — gets a recent LTS instead of the
# stale apt version. Includes corepack (yarn / pnpm shims) automatically.
RUN curl -fsSL https://deb.nodesource.com/setup_lts.x | bash - \
 && apt-get install -y --no-install-recommends nodejs \
 && rm -rf /var/lib/apt/lists/*

# Docker CLI + Compose plugin. The CLI talks to whichever Docker daemon is
# reachable; for it to actually do anything from inside this container, mount
# the host's `/var/run/docker.sock` (compose volume) AND make sure the host's
# `docker` group GID is reachable by the `claude` user (e.g. via a
# `DOCKER_GID` build arg + `usermod -aG docker claude`). Without those, the
# CLI is installed but every command will hit `permission denied`.
RUN install -m 0755 -d /etc/apt/keyrings \
 && curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc \
 && chmod a+r /etc/apt/keyrings/docker.asc \
 && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian $(. /etc/os-release && echo $VERSION_CODENAME) stable" \
        > /etc/apt/sources.list.d/docker.list \
 && apt-get update \
 && apt-get install -y --no-install-recommends docker-ce-cli docker-compose-plugin \
 && rm -rf /var/lib/apt/lists/*

ARG HOST_UID=1000
RUN useradd -m -s /bin/bash -u ${HOST_UID} claude

USER claude

ARG TOOLCHAIN_REFRESH=bootstrap

RUN : "${TOOLCHAIN_REFRESH}" \
 && curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y --default-toolchain stable --profile minimal

RUN : "${TOOLCHAIN_REFRESH}" \
 && curl -LsSf https://astral.sh/uv/install.sh | sh

ENV PATH="/home/claude/.cargo/bin:/home/claude/.local/bin:$PATH"

# Pretty-printing CLI used by the `/print` skill (custom_skills/print/SKILL.md).
# Installed via uv's tool system into ~/.local/bin/ so it's on PATH for every shell.
RUN : "${TOOLCHAIN_REFRESH}" \
 && uv tool install rich-cli

RUN : "${TOOLCHAIN_REFRESH}" \
 && curl -fsSL https://claude.ai/install.sh | bash
ENV DISABLE_AUTOUPDATER=1
ENV CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1
ENV USE_BUILTIN_RIPGREP=0

WORKDIR /workspace
ENTRYPOINT ["claude"]

