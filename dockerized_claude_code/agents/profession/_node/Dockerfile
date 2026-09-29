# The shared Node.js layer — built directly beneath the image layer of every
# harness whose CLI installs from npm (a harness claims it with `layer =
# "node"` in its tag.info; Instance.build_steps puts it right before the
# harness's own layer). Six of the seven harness members install that way; the
# exception is Claude Code, whose standalone installer ships its own Node,
# which is why the base image has none (gate gemini-adapter, 2026-09-25).
#
# Deliberately NOT a copy of [code]'s Node block (profession/code/Dockerfile),
# which is optional (INSTALL_NODE) and failure-tolerant (it logs to
# install_failures.log and carries on). A harness cannot carry on: without
# Node its npm install fails and the image ships no binary for its own
# ENTRYPOINT. So here the install is unconditional and FAILS THE BUILD, and
# it touches no failure log, which only [code] creates.
#
# Keyed on FORCE_INSTALLS_REFRESH ("stable" normally), never on the weekly
# SOFTWARE_STACK_REFRESH: NodeSource's setup_lts.x is a floating pointer, and
# a weekly rebuild here would pull a new Node major beneath whatever else the
# chain installed (researcher__primary). The harness layer above it rebuilds
# weekly without touching this one — docker invalidates from the first
# changed layer down.
#
# When Node is already on the image ([code] is in the chain), this layer
# installs nothing, so it never upgrades the Node [code]'s toolchain was
# built against — it only checks the version meets the floor.

ARG PARENT_IMAGE=claude-agents:base
FROM ${PARENT_IMAGE}

ARG FORCE_INSTALLS_REFRESH=stable

# The floor is the highest `engines.node` among the harnesses that claim this
# layer: @google/gemini-cli 0.61.0 declares >=20. Raise it when a claimant's
# package needs more.
USER root
RUN : "${FORCE_INSTALLS_REFRESH}" \
 && if ! command -v node >/dev/null 2>&1; then \
        curl -fsSL https://deb.nodesource.com/setup_lts.x -o /tmp/nodesource_setup.sh \
        && bash /tmp/nodesource_setup.sh \
        && rm -f /tmp/nodesource_setup.sh \
        && apt-get install -y --no-install-recommends nodejs \
        && rm -rf /var/lib/apt/lists/*; \
    fi \
 && node -e 'process.exit(Number(process.versions.node.split(".")[0]) >= 20 ? 0 : 1)' \
    || { echo "  ✗ Node.js >= 20 is required by this harness's CLI (found: $(node --version 2>/dev/null || echo none))" >&2; exit 1; }

# `npm install -g` as the claude user writes here — the same prefix [code]
# sets, whose bin/ the base image already puts on PATH. Set HERE as well
# because a bare chain (base, this, the harness) has no [code] to inherit it
# from, and the default global prefix is root-owned (bug-investigator). A
# test pins the two spellings together and the prefix's bin/ to base's PATH.
ENV NPM_CONFIG_PREFIX="/home/claude/.local"

USER claude
