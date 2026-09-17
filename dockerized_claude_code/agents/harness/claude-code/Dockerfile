# Claude Code's image layer — the harness's CLI, its env defaults and the
# ENTRYPOINT, plus the image's WEEKLY OS security upgrade. The LAST layer of
# every chain that runs Claude Code (Instance.build_steps appends the harness's
# Dockerfile after the professions' and the specialties'), so the weekly
# SOFTWARE_STACK_REFRESH rebuild — the CLI's only refresh path inside a
# container, since the autoupdater is off — rebuilds this one layer and
# nothing beneath it. Until 2026-09-16 the install sat in the base image and
# its weekly rebuild invalidated every layer above.
#
# Why the OS upgrade is HERE and weekly: that cascade was, by accident, the
# only thing that ever patched the apt packages of every layer (openssl, curl,
# git, NodeSource's node, the browser libs…). Upgrading in the tail keeps the
# cadence at zero cascade cost — apt downloads only the changed .debs — and
# covers every package in the image whatever layer installed it. Resilient
# like the optional installs: a repo outage must not block the CLI's install
# or the launch, so a failed upgrade warns and leaves the cached packages.
#
# A harness's layer supplies exactly this (plans/harness_coupling.md §1): the
# install, the binary's ENTRYPOINT (docker_config.run_container passes the
# agent's flags after the image name when no wrapper entrypoint is in play,
# counting on it), and the image-level env switches the launcher wants for
# that CLI. Being last, this ENTRYPOINT is the image's — a Dockerfile
# ENTRYPOINT in a profession or specialty layer would be overridden here, so
# none may have one; a specialty's entrypoint is a RUN-TIME `--entrypoint`
# contribution in its tag.docker ({firewall}, {muxer}), which
# cluster/launching.refusal exempts precisely because it is not an image fact.
#
# Init order (bug-investigator, gate build-cache): the ARG is re-declared
# after FROM (an ARG before FROM is out of scope for RUN), the install runs
# as an explicit `USER claude` — never the parent's last USER — so the binary
# lands in /home/claude/.local/bin, which the base put on PATH.

ARG PARENT_IMAGE=claude-agents:base
FROM ${PARENT_IMAGE}

ARG SOFTWARE_STACK_REFRESH=bootstrap

USER root
RUN : "${SOFTWARE_STACK_REFRESH}" \
 && { apt-get update \
   && apt-get upgrade -y --no-install-recommends \
   && rm -rf /var/lib/apt/lists/*; } \
   || { rm -rf /var/lib/apt/lists/*; echo "  ⚠ OS package upgrade failed; continuing with the cached packages" >&2; }

USER claude
RUN : "${SOFTWARE_STACK_REFRESH}" \
 && curl -fsSL https://claude.ai/install.sh | bash

# Updates come from the weekly rebuild above, never from inside a container;
# non-essential traffic is the launch shape's decision (a cluster's entrypoint
# UNSETS the kill-switch to enable sibling messaging — launching.py); the
# image's ripgrep is the one every other shell in it uses.
ENV DISABLE_AUTOUPDATER=1
ENV CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1
ENV USE_BUILTIN_RIPGREP=0

ENTRYPOINT ["claude"]
