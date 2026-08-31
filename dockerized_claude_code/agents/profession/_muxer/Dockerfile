# {muxer}'s image layer — a terminal multiplexer inside the container.
#
# Hidden asset dir, claimed by the specialty of the same name (specialty.scan
# matches `_<name>` layers by name). It sits at the PROFESSION ROOT rather than
# under [code], so it carries no profession requirement: multiplexing is useful
# to any instance, not just a coding one.
#
# TWO backends ship, deliberately:
#
# - **herdr** (the DEFAULT since 2026-08-29, an operator decision; probed live
#   on v0.8.2): a background server holding the PTYs — runs headless, panes
#   survive with no client attached — whose `tab create --env/--cwd/--label`
#   matches tmux's per-window env, and whose `agent start <name> --kind claude`
#   gets members DETECTED and listed by name with live idle/working state (the
#   roster tmux's banner only approximated). launch/cluster/herdr.py assembles
#   its scripts. Same prefix (ctrl+b), so muscle memory carries over; detach is
#   prefix+q there, not d.
# - **tmux** (the original pick, chosen for PoC-0 on researched grounds —
#   cluster_plan.md's "Multiplexer options"): native per-window `-e` env and a
#   shell-scriptable status line, both used by launch/cluster/tmux.py.
#   The operator picks it in ~/.claude-agents/ui_profile.toml
#   (herdr_instead_of_tmux = false; the picker's "(Edit Preferences)" form
#   edits it) — launch/cluster/__init__.backend() reads the preference, solo
#   and cluster alike.
#
# Still a thin layer: multiplexers only, so backend policy stays a launch-time
# switch rather than an image decision.

ARG PARENT_IMAGE=claude-agents:base
FROM ${PARENT_IMAGE}

USER root

# tmux + its one runtime dep. ~1 MB installed; no recommends, apt lists dropped,
# matching every other layer's footprint discipline.
RUN apt-get update \
 && apt-get install -y --no-install-recommends tmux \
 && rm -rf /var/lib/apt/lists/*

# herdr: a single static Rust binary from the project's GitHub release, version-
# and checksum-PINNED (pre-1.0, weekly prereleases, bus factor ≈1 — the recorded
# risk profile demands exact pins; the checksum was computed from the binary the
# integration was probed against). x86_64 only for now — an aarch64 host needs
# the TARGETARCH switch this comment is the placeholder for.
ARG HERDR_VERSION=0.8.2
ARG HERDR_SHA256=976150a14d490c94b243ea2e1a7eb2dfb67f12e36b182db90936f6728e6aecf4
# The trailing mkdir/chown is load-bearing: herdr writes its runtime state
# (herdr.sock, session.json, logs) NEXT TO its config, and the launcher
# RO-file-mounts settings/herdr.toml at ~/.config/herdr/config.toml. Docker
# creates a missing mountpoint parent ROOT-owned at run time — a server that
# cannot bind its socket then fails every launch at the readiness gate
# (verified in a live container: the dir arrived root:root, touch refused).
# A file mount into an EXISTING dir leaves the dir's ownership alone, so the
# image pre-owning it is the whole fix.
RUN curl -fsSL -o /usr/local/bin/herdr \
      "https://github.com/herdrdev/herdr/releases/download/v${HERDR_VERSION}/herdr-linux-x86_64" \
 && echo "${HERDR_SHA256}  /usr/local/bin/herdr" | sha256sum -c - \
 && chmod 0755 /usr/local/bin/herdr \
 && mkdir -p /home/claude/.config/herdr \
 && chown claude:claude /home/claude/.config /home/claude/.config/herdr

USER claude
