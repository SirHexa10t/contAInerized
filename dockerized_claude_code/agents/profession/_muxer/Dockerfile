# {muxer}'s image layer — a terminal multiplexer inside the container.
#
# Hidden asset dir, claimed by the specialty of the same name (specialty.scan
# matches `_<name>` layers by name). It sits at the PROFESSION ROOT rather than
# under [code], so it carries no profession requirement: multiplexing is useful
# to any instance, not just a coding one.
#
# tmux was chosen over zellij / WezTerm / herdr on researched grounds (the full
# comparison is in cluster_plan.md's "Multiplexer options"). The two properties
# that decided it are both used by launch/cluster/tmux.py: `new-window -e` sets
# per-window environment natively (each cluster member runs a different model),
# and `status-right` takes `#(shell-command)` interpolation, so the banner is a
# file the launcher owns rather than a format the tool dictates.
#
# Deliberately a thin layer: the multiplexer is the ONLY thing installed here, so
# swapping the backend later (herdr is the watched candidate) is a change to this
# file plus launch/cluster/tmux.py, not a rebuild of the tag tree.

ARG PARENT_IMAGE=claude-agents:base
FROM ${PARENT_IMAGE}

USER root

# tmux + its one runtime dep. ~1 MB installed; no recommends, apt lists dropped,
# matching every other layer's footprint discipline.
RUN apt-get update \
 && apt-get install -y --no-install-recommends tmux \
 && rm -rf /var/lib/apt/lists/*

USER claude
