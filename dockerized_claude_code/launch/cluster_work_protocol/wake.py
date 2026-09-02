"""The ping side — how a queue event WAKES a member.

One mechanism for every ping kind, and it is NOT the messaging feature: a
gate's nudge/close timers run as headless shell forks, and a shell process
cannot call an agent tool like SendMessage. What it CAN do is type into a
member's pane through the multiplexer that hosts the cluster — the plan's
recorded wake option (b), easier here than in the cowork hub because there is
no container boundary. An injected line lands in the member's Claude session
as a user message: a real turn starts, prefixed `[cluster-chat]` so the
member knows to read the queue rather than treat the text as operator prose.

Backend handling mirrors the launcher's: herdr first (its `agent list` maps
member names to panes — the detection the backend was chosen for), tmux as
the fallback (`-L muxer`, window named after the member, `$CLUSTER_SESSION`
naming the session). A failed ping WARNS and never raises: the queue already
holds the truth, and a gate must not die because a wake path wobbled —
`check-gate`'s timers double as the retry.

STANDALONE CONSTRAINT (see __init__): stdlib only, no imports beyond this
package.
"""

from __future__ import annotations

import json
import os
import subprocess

INJECT_PREFIX = "[cluster-chat]"
SESSION_ENV = "CLUSTER_SESSION"
_TIMEOUT_SECONDS = 10


def _run(argv: list[str]) -> "subprocess.CompletedProcess[str] | None":
    """One tool call, captured; None when the binary is absent or hangs —
    both mean 'this backend is not the one running here'."""
    try:
        return subprocess.run(argv, capture_output=True, text=True,
                              timeout=_TIMEOUT_SECONDS)
    except (OSError, subprocess.TimeoutExpired):
        return None


def _herdr_panes() -> "dict[str, str] | None":
    """member id → pane id from herdr's live roster, or None when herdr
    isn't serving here (absent binary, no server, unparseable reply)."""
    reply = _run(["herdr", "agent", "list"])
    if reply is None or reply.returncode != 0:
        return None
    try:
        agents = json.loads(reply.stdout)["result"]["agents"]
        return {agent["name"]: agent["pane_id"] for agent in agents}
    except (json.JSONDecodeError, KeyError, TypeError):
        return None


def inject(member: str, text: str) -> bool:
    """Type `[cluster-chat] <text>` + Enter into `member`'s pane; True when a
    backend accepted it. herdr's `pane run` submits text-plus-Enter
    atomically (doc-verified); tmux's `send-keys … Enter` is its equivalent
    on the fallback backend."""
    line = f"{INJECT_PREFIX} {text}"
    panes = _herdr_panes()
    if panes is not None and member in panes:
        sent = _run(["herdr", "pane", "run", panes[member], line])
        if sent is not None and sent.returncode == 0:
            return True
    session = os.environ.get(SESSION_ENV, "")
    if session:
        sent = _run(["tmux", "-L", "muxer", "send-keys", "-t",
                     f"{session}:{member}", line, "Enter"])
        if sent is not None and sent.returncode == 0:
            return True
    return False


def ping_members(members: "tuple[str, ...] | list[str]", text: str) -> list[str]:
    """Inject `text` to each member; returns WARNING lines for the failures
    (empty = all delivered). Callers print them — a lost wake is worth a
    line, never an exception."""
    return [f"warning: could not wake {member} — it will see the queue on "
            f"its next read"
            for member in members if not inject(member, text)]
