#!/usr/bin/env python3
"""Entry script for cluster mode — cohabiting agents (PoC).

    cluster create devteam-poc ~/code/myproject
    cluster list
    cluster plan devteam-poc
    cluster script devteam-poc --out /tmp/cluster-entrypoint.sh
    cluster destroy devteam-poc

A cluster is N agent members sharing ONE container and ONE project, switched
between in a single window via tmux. The second collaboration mode beside
`{cowork}` (which routes between isolated containers instead). Design record:
`cluster_plan.md`.

PoC status: this models, validates, and assembles — it does not start a container.
`plan` prints the full command sequence and `script` writes the tmux entrypoint,
so the remaining work is wiring those into the image build, which `docker_config`
owns. See `launch/cluster/cli.py` for why that split is deliberate.

Thin on purpose, exactly like `cowork.py`: argument parsing and dispatch live in
`launch.cluster.cli` so they are importable and testable without a subprocess.
"""

from __future__ import annotations

import sys

from launch.cluster.cli import main

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
