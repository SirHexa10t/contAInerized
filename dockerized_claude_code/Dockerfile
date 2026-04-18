FROM debian:bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    bash \
    git \
    curl \
    ca-certificates \
    ripgrep \
    && rm -rf /var/lib/apt/lists/*

RUN mkdir -p /workspace

ARG HOST_UID
RUN useradd -m -s /bin/bash -u ${HOST_UID} claude \
    && chown -R claude:claude /workspace \
    && mkdir -p /home/claude/.claude/projects/-workspace/memory \
    && chown -R claude:claude /home/claude/.claude

USER claude

RUN curl -fsSL https://claude.ai/install.sh | bash

ENV PATH="/home/claude/.local/bin:$PATH"
ENV DISABLE_AUTOUPDATER=1
ENV CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1
ENV USE_BUILTIN_RIPGREP=0

WORKDIR /workspace
ENTRYPOINT ["claude"]

