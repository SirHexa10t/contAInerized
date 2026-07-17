# Programming-stack image — extends its PARENT_IMAGE (default claude-agents:base)
# with build-essential, Rust toolchain, and Node.js LTS. Built when an agent's
# filename includes the [code] tag.
#
# Build chain: PARENT_IMAGE is set by run.py's ensure_image() to whatever
# precedes code in the chain — for code as the first chain step, that's
# claude-agents:base. The default below is the standalone case.
#
# Optional CLI installs at the end are gated by INSTALL_<TOOL>=1 build-args, set
# by run.py based on which directories exist under ~/.claude-agents/user_extras/optional_creds/.
# Each tool gets its own ARG/RUN so adding a new credential only invalidates that
# tool's layer (downstream layers re-run as no-ops). Order matters: stable layers
# (rustup, node) come BEFORE optional installs so opting into a new tool doesn't
# trigger a Rust reinstall.

ARG PARENT_IMAGE=claude-agents:base
FROM ${PARENT_IMAGE}

USER root
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    pkg-config \
    libssl-dev \
    python3-dotenv \
    && rm -rf /var/lib/apt/lists/*

# Node.js (current LTS) + npm via NodeSource — gets a recent LTS instead of the
# stale apt version. Includes corepack (yarn / pnpm shims) automatically.
RUN curl -fsSL https://deb.nodesource.com/setup_lts.x | bash - \
 && apt-get install -y --no-install-recommends nodejs \
 && rm -rf /var/lib/apt/lists/*

USER claude

ARG SOFTWARE_STACK_REFRESH=bootstrap

RUN : "${SOFTWARE_STACK_REFRESH}" \
 && curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y --default-toolchain stable --profile minimal

ENV PATH="/home/claude/.cargo/bin:$PATH"
# Lets `npm install -g <pkg>` write to ~/.local/{lib/node_modules,bin} as the claude
# user — no sudo, no /usr/lib/node_modules permission errors. Applies to the optional
# vercel/railway installs below AND any runtime `npm install -g` the agent runs.
ENV NPM_CONFIG_PREFIX="/home/claude/.local"

# === Optional CLI installs — gated by INSTALL_<TOOL>=1 build-args from run.py ===
#
# Each install block follows the same resilient pattern:
#   - Reference both INSTALL_<TOOL> and FORCE_INSTALLS_REFRESH in the leading
#     `:` no-op so docker's layer cache key includes both. Changing either
#     value (e.g. opting in a new cred OR launching with --refresh-installs)
#     busts the layer and the install retries.
#   - Strip this tool's own name from /var/log/claude-agents/install_failures.log at the
#     start, so a previously-failed install that succeeds on retry clears
#     its own warning.
#   - Wrap the install steps in `{ ... } || { ... }`. On failure (curl 403,
#     apt repo unreachable, etc.) we append the tool's name to the failure
#     log and emit a stderr line, then exit 0 — the build never aborts on
#     a single optional install. The launcher reads the log post-build and
#     surfaces failures in the launch banner.
#
# Adding a new optional install means following the same pattern: ARG + RUN
# with the strip-then-try-then-log shape, plus one INSTALL_<TOOL> entry in
# compose.code.yml's `args:` block.

# Root-needing (apt-based) — installed as root, then we drop back to claude.
USER root

ARG FORCE_INSTALLS_REFRESH=stable
# Pre-create the failure log in a claude-owned subdir of /var/log so both root
# and non-root install blocks can manage it. Root writes regardless of perms;
# claude owns the dir (needed because `sed -i` writes a tempfile alongside the
# target and renames over — that requires write access to the *parent dir*,
# not just the file, and claude lacks write on /var/log itself).
RUN install -d -o claude -g claude /var/log/claude-agents \
 && install -o claude -g claude -m 644 /dev/null /var/log/claude-agents/install_failures.log

ARG INSTALL_GH=0
RUN : "${INSTALL_GH}" "${FORCE_INSTALLS_REFRESH}" \
 && sed -i '/^gh$/d' /var/log/claude-agents/install_failures.log \
 && if [ "$INSTALL_GH" = "1" ]; then \
        { install -m 0755 -d /etc/apt/keyrings \
        && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg -o /etc/apt/keyrings/githubcli-archive-keyring.gpg \
        && chmod go+r /etc/apt/keyrings/githubcli-archive-keyring.gpg \
        && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" > /etc/apt/sources.list.d/github-cli.list \
        && apt-get update \
        && apt-get install -y --no-install-recommends gh \
        && rm -rf /var/lib/apt/lists/*; } \
        || { echo "gh" >> /var/log/claude-agents/install_failures.log; echo "  ⚠ gh install failed; continuing" >&2; }; \
    fi

ARG INSTALL_GLAB=0
RUN : "${INSTALL_GLAB}" "${FORCE_INSTALLS_REFRESH}" \
 && sed -i '/^glab$/d' /var/log/claude-agents/install_failures.log \
 && if [ "$INSTALL_GLAB" = "1" ]; then \
        { curl -1sLf 'https://packages.gitlab.com/install/repositories/gitlab/cli/setup.deb.sh' | bash \
        && apt-get install -y --no-install-recommends glab \
        && rm -rf /var/lib/apt/lists/*; } \
        || { echo "glab" >> /var/log/claude-agents/install_failures.log; echo "  ⚠ glab install failed; continuing" >&2; }; \
    fi

ARG INSTALL_SSH=0
RUN : "${INSTALL_SSH}" "${FORCE_INSTALLS_REFRESH}" \
 && sed -i '/^ssh$/d' /var/log/claude-agents/install_failures.log \
 && if [ "$INSTALL_SSH" = "1" ]; then \
        { apt-get update \
        && apt-get install -y --no-install-recommends openssh-client \
        && rm -rf /var/lib/apt/lists/*; } \
        || { echo "ssh" >> /var/log/claude-agents/install_failures.log; echo "  ⚠ ssh install failed; continuing" >&2; }; \
    fi

# Heavy install (~400-500MB); only paid by users who actually opt in via user_extras/optional_creds/gcloud/.
ARG INSTALL_GCLOUD=0
RUN : "${INSTALL_GCLOUD}" "${FORCE_INSTALLS_REFRESH}" \
 && sed -i '/^gcloud$/d' /var/log/claude-agents/install_failures.log \
 && if [ "$INSTALL_GCLOUD" = "1" ]; then \
        { install -m 0755 -d /etc/apt/keyrings \
        && curl -fsSL https://packages.cloud.google.com/apt/doc/apt-key.gpg | gpg --dearmor -o /etc/apt/keyrings/cloud.google.gpg \
        && echo "deb [signed-by=/etc/apt/keyrings/cloud.google.gpg] https://packages.cloud.google.com/apt cloud-sdk main" > /etc/apt/sources.list.d/google-cloud-sdk.list \
        && apt-get update \
        && apt-get install -y --no-install-recommends google-cloud-cli \
        && rm -rf /var/lib/apt/lists/*; } \
        || { echo "gcloud" >> /var/log/claude-agents/install_failures.log; echo "  ⚠ gcloud install failed; continuing" >&2; }; \
    fi

# User-runnable (uv-tool / static binary / npm-global into ~/.local).
USER claude

ARG INSTALL_AWS=0
RUN : "${INSTALL_AWS}" "${FORCE_INSTALLS_REFRESH}" \
 && sed -i '/^aws$/d' /var/log/claude-agents/install_failures.log \
 && if [ "$INSTALL_AWS" = "1" ]; then \
        uv tool install awscli \
        || { echo "aws" >> /var/log/claude-agents/install_failures.log; echo "  ⚠ aws install failed; continuing" >&2; }; \
    fi

ARG INSTALL_KUBE=0
RUN : "${INSTALL_KUBE}" "${FORCE_INSTALLS_REFRESH}" \
 && sed -i '/^kubectl$/d' /var/log/claude-agents/install_failures.log \
 && if [ "$INSTALL_KUBE" = "1" ]; then \
        { curl -fsSL "https://dl.k8s.io/release/$(curl -L -s https://dl.k8s.io/release/stable.txt)/bin/linux/$(dpkg --print-architecture)/kubectl" -o /home/claude/.local/bin/kubectl \
        && chmod +x /home/claude/.local/bin/kubectl; } \
        || { echo "kubectl" >> /var/log/claude-agents/install_failures.log; echo "  ⚠ kubectl install failed; continuing" >&2; }; \
    fi

# Jira CLI (ankitpokhrel/jira-cli) — Go-based static binary from GitHub releases.
# Latest version pulled from the GitHub API at build time; arch maps amd64 → x86_64,
# arm64 → arm64 (the tarball's own naming convention).
ARG INSTALL_JIRA=0
RUN : "${INSTALL_JIRA}" "${FORCE_INSTALLS_REFRESH}" \
 && sed -i '/^jira$/d' /var/log/claude-agents/install_failures.log \
 && if [ "$INSTALL_JIRA" = "1" ]; then \
        { VERSION=$(curl -fsSL https://api.github.com/repos/ankitpokhrel/jira-cli/releases/latest | grep -oP '"tag_name": "v\K[^"]+') \
        && case "$(dpkg --print-architecture)" in \
              amd64) ARCH_SUFFIX=x86_64 ;; \
              arm64) ARCH_SUFFIX=arm64 ;; \
              *) echo "unsupported arch for jira-cli: $(dpkg --print-architecture)" >&2; exit 1 ;; \
           esac \
        && curl -fsSL "https://github.com/ankitpokhrel/jira-cli/releases/download/v${VERSION}/jira_${VERSION}_linux_${ARCH_SUFFIX}.tar.gz" \
           | tar -xz -C /tmp \
        && mv "/tmp/jira_${VERSION}_linux_${ARCH_SUFFIX}/bin/jira" /home/claude/.local/bin/jira \
        && rm -rf "/tmp/jira_${VERSION}_linux_${ARCH_SUFFIX}"; } \
        || { echo "jira" >> /var/log/claude-agents/install_failures.log; echo "  ⚠ jira install failed; continuing" >&2; }; \
    fi

ARG INSTALL_VERCEL=0
RUN : "${INSTALL_VERCEL}" "${FORCE_INSTALLS_REFRESH}" \
 && sed -i '/^vercel$/d' /var/log/claude-agents/install_failures.log \
 && if [ "$INSTALL_VERCEL" = "1" ]; then \
        npm install -g vercel \
        || { echo "vercel" >> /var/log/claude-agents/install_failures.log; echo "  ⚠ vercel install failed; continuing" >&2; }; \
    fi

ARG INSTALL_RAILWAY=0
RUN : "${INSTALL_RAILWAY}" "${FORCE_INSTALLS_REFRESH}" \
 && sed -i '/^railway$/d' /var/log/claude-agents/install_failures.log \
 && if [ "$INSTALL_RAILWAY" = "1" ]; then \
        npm install -g @railway/cli \
        || { echo "railway" >> /var/log/claude-agents/install_failures.log; echo "  ⚠ railway install failed; continuing" >&2; }; \
    fi
