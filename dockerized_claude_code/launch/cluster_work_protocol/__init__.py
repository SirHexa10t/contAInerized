"""`launch.cluster_work_protocol` — the cluster's INTERNAL work protocol: the
message-queue members speak on, its schema and vocabulary, the gate machinery,
and every tunable. Design record: plans/cluster_plan.md ("PROPOSAL v2" and the
iteration-accounting section — nop is the fold, stances ride the configured
0-10 scale, the completing reply pings the opener).

One module per concern, deliberately — trial-and-error is the expected mode
and a tweak must have exactly one home:

    config.py    every tunable (timeouts, caps, the stance scale), loaded
                 from settings/cluster_protocol.toml's mount
    schema.py    the message shape + reply vocabulary + validation
    queue.py     the append-only chat.jsonl: flock'd appends, read-since,
                 per-member cursors — the only module touching the file
    gates.py     open/close/accounting (nop vs stance vs TIMEOUT)   [step 4]
    wake.py      the ping policy (completion-ping, @mentions)       [step 4]
    cli.py       the `cluster-chat` verbs members call              [step 3]

**STANDALONE CONSTRAINT — this package must import WITHOUT the launch/
parent.** Containers receive it as a read-only mount of this directory alone
(plus a bin shim), so: stdlib only, intra-package imports only (`from .config
import …` — never `from ..paths import …`). It works as both
`launch.cluster_work_protocol` (host tests, the quality gate) and
`cluster_work_protocol` (in-container). A test pins the constraint.

Container-side paths are therefore OWNED HERE (the package is what runs
there); the launcher's mount wiring points at them, and a drift-pin test
keeps the two agreeing — the same pattern as solo.CONTAINER_SCRIPT.
"""

from pathlib import Path

from .config import CONFIG_IN_CONTAINER, ProtocolConfig, load_config
from .queue import PROTOCOL_DIR_IN_CONTAINER, Queue
from .schema import KINDS, REPLY_KINDS, Message, ProtocolError

# Where the launcher RO-mounts THIS package inside a cluster container. The
# `_cluster` layer's `cluster-chat` shim puts its parent (/opt) on PYTHONPATH
# and module-runs cli — a test pins the shim, this constant, and the mount
# wiring to one another.
PACKAGE_IN_CONTAINER = Path("/opt/cluster_work_protocol")

__all__ = [
    "CONFIG_IN_CONTAINER", "KINDS", "Message", "PACKAGE_IN_CONTAINER",
    "PROTOCOL_DIR_IN_CONTAINER", "ProtocolConfig", "ProtocolError", "Queue",
    "REPLY_KINDS", "load_config",
]
