# Programming-stack image — extends its PARENT_IMAGE (default claude-agents:base)
# with the coding toolchain. Built when an agent's filename includes the
# [code] tag.
#
# Build chain: PARENT_IMAGE is set by run.py's ensure_image() to whatever
# precedes code in the chain — for code as the first chain step, that's
# claude-agents:base. The default below is the standalone case.
#
# Mandatory (below, unconditional): build-essential, pkg-config, libssl-dev,
# python3-dotenv — the compiler/linker/headers every other install in this
# file (and every [code] agent) can assume are present. Never gated.
#
# Everything else is gated by an INSTALL_<TOOL>=1 build-arg, computed by the
# launcher from one of two DISJOINT mechanisms (test_essential_files.py
# cross-checks the split and the manifest↔ARG lockstep):
#   - Language toolchains (rust / node / cmake / go / java / kotlin /
#     ruby) — the picker's "Edit Toolkits" form, persisted at
#     ~/.claude-agents/code_profile.toml; each `ARG` name below is declared
#     by its entry's `build_arg` in the sibling template.form, so the
#     manifest→ARG pairing is defined in THIS directory
#     (container_env.toolkit_install_flags emits whatever the form names).
#   - Service CLIs (gh ... railway) — creds-presence: a matching
#     ~/.claude-agents/user_extras/optional_creds/<name>/ dir is the opt-in
#     (container_env.install_creds_flags). Deliberately NOT form-toggleable.
# Each tool gets its own ARG/RUN so toggling one only invalidates that tool's
# layer (downstream layers re-run as no-ops). Order matters for cache
# locality: rust/node/cmake (on by default for every fresh profile) come
# first, so a user who never opens the toolkit form gets the same cache
# behavior this file had before it grew toggles; the off-by-default
# languages (go/java/kotlin/ruby) follow — toggling one never rebuilds the
# default-on layers above it — and the creds-gated CLIs come last.
#
# Each install block follows the same resilient pattern:
#   - Reference this tool's own INSTALL_<TOOL> and FORCE_INSTALLS_REFRESH
#     (rust also references the weekly SOFTWARE_STACK_REFRESH rotation) in
#     the leading `:` no-op, so docker's layer cache key includes all of
#     them. Changing any value (toggling the toolkit profile, opting in a
#     new cred, or launching with --refresh-installs) busts the layer and
#     the install retries.
#   - Strip this tool's own name from /var/log/claude-agents/install_failures.log at the
#     start, so a previously-failed install that succeeds on retry clears
#     its own warning.
#   - Wrap the install steps in `{ ... } || { ... }`. On failure (curl 403,
#     apt repo unreachable, etc.) we append the tool's name to the failure
#     log and emit a stderr line, then exit 0 — the build never aborts on
#     a single optional install. The launcher reads the log post-build and
#     surfaces failures in the launch banner.
#
# Adding a new install means following the same pattern: ARG + RUN with the
# strip-then-try-then-log shape, plus EITHER a `[<tool>]` table in
# template.form (toolchain, form-toggleable) OR an OPTIONAL_CREDS_MOUNTS
# entry in launch/paths.py (creds-driven CLI).

ARG PARENT_IMAGE=claude-agents:base
FROM ${PARENT_IMAGE}

USER root
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    pkg-config \
    libssl-dev \
    python3-dotenv \
    && rm -rf /var/lib/apt/lists/*

# Pre-create the failure log in a claude-owned subdir of /var/log so both root
# and non-root install blocks below (root: cmake/node/gh/…; claude: rust/aws/…)
# can manage it. Root writes regardless of perms; claude owns the dir (needed
# because `sed -i` writes a tempfile alongside the target and renames over —
# that requires write access to the *parent dir*, not just the file, and
# claude lacks write on /var/log itself).
RUN install -d -o claude -g claude /var/log/claude-agents \
 && install -o claude -g claude -m 644 /dev/null /var/log/claude-agents/install_failures.log

ARG SOFTWARE_STACK_REFRESH=bootstrap
ARG FORCE_INSTALLS_REFRESH=stable

ARG INSTALL_CMAKE=1
RUN : "${INSTALL_CMAKE}" "${FORCE_INSTALLS_REFRESH}" \
 && sed -i '/^cmake$/d' /var/log/claude-agents/install_failures.log \
 && if [ "$INSTALL_CMAKE" = "1" ]; then \
        { apt-get update \
        && apt-get install -y --no-install-recommends cmake \
        && rm -rf /var/lib/apt/lists/*; } \
        || { echo "cmake" >> /var/log/claude-agents/install_failures.log; echo "  ⚠ cmake install failed; continuing" >&2; }; \
    fi

# Node.js (current LTS) + npm via NodeSource — gets a recent LTS instead of
# the stale apt version. Includes corepack (yarn / pnpm shims) automatically.
ARG INSTALL_NODE=1
RUN : "${INSTALL_NODE}" "${FORCE_INSTALLS_REFRESH}" \
 && sed -i '/^node$/d' /var/log/claude-agents/install_failures.log \
 && if [ "$INSTALL_NODE" = "1" ]; then \
        { curl -fsSL https://deb.nodesource.com/setup_lts.x | bash - \
        && apt-get install -y --no-install-recommends nodejs \
        && rm -rf /var/lib/apt/lists/*; } \
        || { echo "node" >> /var/log/claude-agents/install_failures.log; echo "  ⚠ node install failed; continuing" >&2; }; \
    fi

USER claude

ARG INSTALL_RUST=1
RUN : "${INSTALL_RUST}" "${SOFTWARE_STACK_REFRESH}" "${FORCE_INSTALLS_REFRESH}" \
 && sed -i '/^rust$/d' /var/log/claude-agents/install_failures.log \
 && if [ "$INSTALL_RUST" = "1" ]; then \
        curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y --default-toolchain stable --profile minimal \
        || { echo "rust" >> /var/log/claude-agents/install_failures.log; echo "  ⚠ rust install failed; continuing" >&2; }; \
    fi

ENV PATH="/home/claude/.cargo/bin:$PATH"
# Lets `npm install -g <pkg>` write to ~/.local/{lib/node_modules,bin} as the claude
# user — no sudo, no /usr/lib/node_modules permission errors. Applies to the optional
# vercel/railway installs below AND any runtime `npm install -g` the agent runs.
ENV NPM_CONFIG_PREFIX="/home/claude/.local"
# Go's install prefix — on PATH unconditionally (inert while [go] is off;
# ENV can't be conditional on a build-arg).
ENV PATH="/usr/local/go/bin:$PATH"

# === Additional language toolchains — off by default in template.form (they
# were never part of the pre-toggle image); flip them on in the "Edit
# Toolkits" form. Placed after the default-on layers so toggling one never
# rebuilds rust/node/cmake. ===

USER root

ARG INSTALL_GO=0
RUN : "${INSTALL_GO}" "${FORCE_INSTALLS_REFRESH}" \
 && sed -i '/^go$/d' /var/log/claude-agents/install_failures.log \
 && if [ "$INSTALL_GO" = "1" ]; then \
        { GO_VER=$(curl -fsSL 'https://go.dev/VERSION?m=text' | head -1) \
        && curl -fsSL "https://go.dev/dl/${GO_VER}.linux-$(dpkg --print-architecture).tar.gz" | tar -C /usr/local -xz; } \
        || { echo "go" >> /var/log/claude-agents/install_failures.log; echo "  ⚠ go install failed; continuing" >&2; }; \
    fi

ARG INSTALL_JAVA=0
RUN : "${INSTALL_JAVA}" "${FORCE_INSTALLS_REFRESH}" \
 && sed -i '/^java$/d' /var/log/claude-agents/install_failures.log \
 && if [ "$INSTALL_JAVA" = "1" ]; then \
        { apt-get update \
        && apt-get install -y --no-install-recommends default-jdk-headless \
        && rm -rf /var/lib/apt/lists/*; } \
        || { echo "java" >> /var/log/claude-agents/install_failures.log; echo "  ⚠ java install failed; continuing" >&2; }; \
    fi

# Kotlin compiler (JetBrains GitHub release zip). Needs a JRE — installs the
# headless default alongside; an apt no-op when [java] is also selected.
ARG INSTALL_KOTLIN=0
RUN : "${INSTALL_KOTLIN}" "${FORCE_INSTALLS_REFRESH}" \
 && sed -i '/^kotlin$/d' /var/log/claude-agents/install_failures.log \
 && if [ "$INSTALL_KOTLIN" = "1" ]; then \
        { apt-get update \
        && apt-get install -y --no-install-recommends default-jre-headless unzip \
        && KOTLIN_VER=$(curl -fsSL https://api.github.com/repos/JetBrains/kotlin/releases/latest | grep -oP '"tag_name": "v\K[^"]+') \
        && curl -fsSL -o /tmp/kotlin.zip "https://github.com/JetBrains/kotlin/releases/download/v${KOTLIN_VER}/kotlin-compiler-${KOTLIN_VER}.zip" \
        && unzip -q /tmp/kotlin.zip -d /opt \
        && rm /tmp/kotlin.zip \
        && ln -sf /opt/kotlinc/bin/kotlin /opt/kotlinc/bin/kotlinc /usr/local/bin/ \
        && rm -rf /var/lib/apt/lists/*; } \
        || { echo "kotlin" >> /var/log/claude-agents/install_failures.log; echo "  ⚠ kotlin install failed; continuing" >&2; }; \
    fi

ARG INSTALL_RUBY=0
RUN : "${INSTALL_RUBY}" "${FORCE_INSTALLS_REFRESH}" \
 && sed -i '/^ruby$/d' /var/log/claude-agents/install_failures.log \
 && if [ "$INSTALL_RUBY" = "1" ]; then \
        { apt-get update \
        && apt-get install -y --no-install-recommends ruby-full \
        && rm -rf /var/lib/apt/lists/*; } \
        || { echo "ruby" >> /var/log/claude-agents/install_failures.log; echo "  ⚠ ruby install failed; continuing" >&2; }; \
    fi

# === Creds-gated service CLI installs — same resilient pattern as above;
# installed only for users who drop matching creds into
# user_extras/optional_creds/ (never offered in the toolkit form). ===

# Root-needing (apt-based) — installed as root, then we drop back to claude.
USER root

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

# Heavy install (~400-500MB); only paid by users who actually opt in.
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

# ruff — UNCONDITIONAL, unlike every install below it: no ARG, no
# template.form toggle. Python is a given for [code] (the base image ships
# python3 + uv), and a linter is not an optional toolchain but the check a
# code agent runs on its own output — a coworker probe found `ruff: command
# not found` to be the one remaining blocker after permissions were sorted,
# and every instance re-installing it per container is waste. Still wrapped in
# the failure-log shape so a transient network fault degrades to a banner
# warning rather than aborting the build.
RUN sed -i '/^ruff$/d' /var/log/claude-agents/install_failures.log \
 && { uv tool install ruff; } \
    || { echo "ruff" >> /var/log/claude-agents/install_failures.log; echo "  ⚠ ruff install failed; continuing" >&2; }

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
