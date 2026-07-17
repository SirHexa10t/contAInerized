# Web-stack image — extends its PARENT_IMAGE (default claude-agents:code) with
# the headless-browser dependencies playwright needs: the playwright CLI itself
# plus the ~20 apt libs chromium (and other engines) link against. Built when
# an instance has the {web} mode set.
#
# Browser BINARIES are NOT installed here. They live in playwright's default
# install path (`~/.cache/ms-playwright/`), which already sits inside [code]'s
# `~/.cache` bind-mount — so the first `playwright install <browser>` call in
# any [code][web] instance lands the binaries on the host cache, and every
# subsequent [code][web] instance reuses them. Image stays lean (~30MB add
# for apt libs + CLI, vs ~160MB if we baked chromium in).
#
# {web} is gated behind [code] in the prompt flow, so PARENT_IMAGE always
# resolves to something that includes code — either claude-agents:code or a
# chain layer above it (e.g. code.auto.dood).
#
# No display server: chromium runs headless; the apt libs are the offscreen-
# renderer / font / audio set the browser binary statically links against.

ARG PARENT_IMAGE=claude-agents:code
FROM ${PARENT_IMAGE}

USER root

# Minimal headless-chromium apt set. Sourced from playwright's own registry
# (nativeDeps.ts, `debian13` chromium entry — matches the trixie base; several
# libs carry the t64 suffix from Debian 13's 64-bit time_t transition) —
# pinned here so the build doesn't shell out to playwright to discover its
# own deps. The two fonts-* packages aren't in playwright's list; they're a
# deliberate add so pages render text/emoji instead of tofu boxes.
RUN apt-get update && apt-get install -y --no-install-recommends \
    libnss3 \
    libnspr4 \
    libdbus-1-3 \
    libatk1.0-0t64 \
    libatk-bridge2.0-0t64 \
    libatspi2.0-0t64 \
    libcups2t64 \
    libdrm2 \
    libglib2.0-0t64 \
    libx11-6 \
    libxcb1 \
    libxext6 \
    libxkbcommon0 \
    libxcomposite1 \
    libxdamage1 \
    libxfixes3 \
    libxrandr2 \
    libgbm1 \
    libpango-1.0-0 \
    libcairo2 \
    libasound2t64 \
    fonts-liberation \
    fonts-noto-color-emoji \
 && rm -rf /var/lib/apt/lists/*

USER claude

# Install just the playwright CLI (and Python package), not any browser
# binaries — those download into the cache mount on first use. `uv tool
# install` puts `playwright` in ~/.local/bin (already on PATH from base).
# Agents that want playwright importable from Python in their project do
# `uv pip install playwright` in their own venv — package is ~5MB and finds
# the runtime-downloaded browsers at the default `~/.cache/ms-playwright/`
# location (which is the bind-mounted [code] cache, so cross-instance
# sharing is automatic).
#
# SOFTWARE_STACK_REFRESH busts the layer cache weekly so subsequent rebuilds
# pull the latest playwright — same pattern Rust/uv/rich-cli/Claude Code use.
ARG SOFTWARE_STACK_REFRESH=bootstrap
RUN : "${SOFTWARE_STACK_REFRESH}" \
 && uv tool install playwright
