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

# The site toolbox — a SEPARATE layer from the browser libs above, so neither
# list's cache turns on the other's, and the provenance comment there keeps
# covering exactly playwright's set. Three tools, each answering a question a
# web agent otherwise cannot ask from inside the container (operator request,
# 2026-09-17: "notably dig"):
#
# - **bind9-dnsutils** → `dig` (plus `nslookup`): what a name actually
#   resolves to, and which records exist — the A/AAAA/CNAME/MX/TXT check
#   behind "the site is up for me but not for them", a DNS cutover, a domain
#   verification TXT record. `bind9-dnsutils` is the package's real name since
#   bullseye; `dnsutils` is only a transitional stub and is gone in trixie.
# - **whois**: who holds the domain, when it expires, which nameservers it
#   delegates to — registration facts no HTTP request reveals.
# - **openssl**: `openssl s_client -connect host:443` for the certificate
#   chain and its expiry, the other half of "why does this site warn". Named
#   explicitly rather than leaned on: ca-certificates has pulled the CLI in on
#   past Debian releases, which is a transitive dependency, not a promise.
#
# Deliberately NOT here: anything needing a browser at BUILD time (lighthouse
# and friends) — browser binaries live in the runtime cache mount, so a
# build-time install of one would be the layer's only heavy, and the only part
# that could not work offline.
RUN apt-get update && apt-get install -y --no-install-recommends \
    bind9-dnsutils \
    whois \
    openssl \
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
# Keyed on FORCE_INSTALLS_REFRESH (--refresh-installs) like every tool
# install — the playwright CLI refreshes on demand; the apt libs above are
# patched weekly by the harness layer's `apt-get upgrade`, and the browser
# binaries live in the host cache mount, refreshed by `playwright install`
# at run time. Never the weekly SOFTWARE_STACK_REFRESH — that is the harness
# layer's word, and a weekly buster here cascaded into every specialty layer
# above until 2026-09-16.
ARG FORCE_INSTALLS_REFRESH=stable
RUN : "${FORCE_INSTALLS_REFRESH}" \
 && uv tool install playwright
