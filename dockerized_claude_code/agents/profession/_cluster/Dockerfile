# {clstr}'s image layer — what the cluster work-protocol needs in-container.
#
# Hidden asset dir, claimed by the `cluster` specialty BY NAME (the same
# mechanism as {muxer}/_muxer; specialty.scan matches `_<name>` layers). At
# the profession ROOT deliberately: a cluster member needn't be [code].
#
# Two installs, and why each:
#
# - **python3** (the full package, ~30MB with its stdlib — python3-minimal
#   ships a PARED stdlib and the protocol needs tomllib/argparse/fcntl):
#   the work-protocol package (launch/cluster_work_protocol, RO-mounted at
#   /opt/cluster_work_protocol by cluster launches) is python, and the BASE
#   image carries no python at all.
# - **the `cluster-chat` shim**: the one executable entry to that package —
#   members speak the queue through it per the {clstr} addendum. It appends
#   /opt to PYTHONPATH and module-runs the CLI, which is exactly the
#   standalone-import contract the package's own tests pin.
ARG PARENT_IMAGE=claude-agents:base
FROM ${PARENT_IMAGE}

USER root

RUN apt-get update \
 && apt-get install -y --no-install-recommends python3 \
 && rm -rf /var/lib/apt/lists/*

RUN printf '%s\n' \
      '#!/bin/sh' \
      '# cluster-chat — the cluster work-protocol CLI. The package is a read-only' \
      '# mount at /opt/cluster_work_protocol; its config sits beside it at' \
      '# /opt/cluster_work_protocol.toml (both mounted by the launcher per launch).' \
      'exec env PYTHONPATH="/opt${PYTHONPATH:+:$PYTHONPATH}" python3 -m cluster_work_protocol.cli "$@"' \
      > /usr/local/bin/cluster-chat \
 && chmod 0755 /usr/local/bin/cluster-chat

USER claude
