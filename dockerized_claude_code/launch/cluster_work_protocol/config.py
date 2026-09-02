"""Every tunable, in one module — loaded from settings/cluster_protocol.toml
(host-side: the repo file, for tests; in-container: that file's read-only
mount at CONFIG_IN_CONTAINER). The protocol's trial-and-error loop lives in
that FILE: a tweak is an edit plus relaunch, never a code change or an image
rebuild.

Missing keys are a LOUD stop (ProtocolError naming the key), never a silent
default — a protocol quietly running on built-ins would hide exactly the
tweak the operator thought they had made. Same stance the launcher's
ui_profile launch-read takes, for the same reason.

STANDALONE CONSTRAINT (see __init__): stdlib only, no imports beyond this
package.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

from .schema import STANCE_MAX, STANCE_MIN, ProtocolError

# Where the launcher mounts settings/cluster_protocol.toml, read-only. Owned
# HERE (this package is what runs in-container); the mount wiring points at
# it and a drift-pin test keeps the two agreeing. /opt-rooted beside the
# package mount, NOT under /cluster: a file mount's auto-created parent is
# root-owned (the recorded herdr lesson), and /cluster/* must stay writable
# by the members' user.
CONFIG_IN_CONTAINER = Path("/opt/cluster_work_protocol.toml")


@dataclass(frozen=True)
class ProtocolConfig:
    """The tunables, typed. `scale` maps every stance value (0-10, complete
    by validation) to the sentiment wording agents quote."""
    nudge_after_seconds: int
    close_after_seconds: int
    reply_cap: int
    lock_wait_seconds: int
    loop_cap: int
    scale: dict[int, str]


def _require(table: dict[str, object], section: str, key: str) -> object:
    if key not in table:
        raise ProtocolError(
            f"cluster_protocol.toml: [{section}] is missing '{key}' — every "
            f"tunable is required (a silent default would hide a mistyped "
            f"tweak); restore the key or re-seed the file from the repo's "
            f"settings/cluster_protocol.toml")
    return table[key]


def load_config(path: Path = CONFIG_IN_CONTAINER) -> ProtocolConfig:
    """Parse and validate the protocol config. The scale must be COMPLETE —
    one meaning per stance value, 0 through 10 — because agents are told to
    quote these meanings; a hole would leave a stance nobody can explain."""
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except OSError as error:
        raise ProtocolError(
            f"cluster_protocol.toml not readable at {path} — the launcher "
            f"mounts settings/cluster_protocol.toml there for cluster "
            f"launches") from error
    except tomllib.TOMLDecodeError as error:
        raise ProtocolError(f"{path} is not valid TOML: {error}") from error

    gate = data.get("gate", {})
    queue = data.get("queue", {})
    raw_scale = data.get("scale", {})
    scale: dict[int, str] = {}
    for value in range(STANCE_MIN, STANCE_MAX + 1):
        meaning = raw_scale.get(str(value))
        if not isinstance(meaning, str) or not meaning.strip():
            raise ProtocolError(
                f"cluster_protocol.toml: [scale] needs a non-empty meaning "
                f"for every value {STANCE_MIN}-{STANCE_MAX}; '{value}' is "
                f"missing or empty")
        scale[value] = meaning

    def _int(table: dict[str, object], section: str, key: str) -> int:
        raw = _require(table, section, key)
        if not isinstance(raw, int) or isinstance(raw, bool) or raw <= 0:
            raise ProtocolError(
                f"cluster_protocol.toml: [{section}] {key} must be a "
                f"positive integer, got {raw!r}")
        return raw

    return ProtocolConfig(
        nudge_after_seconds=_int(gate, "gate", "nudge_after_seconds"),
        close_after_seconds=_int(gate, "gate", "close_after_seconds"),
        reply_cap=_int(gate, "gate", "reply_cap"),
        lock_wait_seconds=_int(queue, "queue", "lock_wait_seconds"),
        loop_cap=_int(queue, "queue", "loop_cap"),
        scale=scale,
    )
