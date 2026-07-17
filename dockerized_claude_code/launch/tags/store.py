"""`instances.toml` — the per-instance axis store, keyed by instance id:

    ["researcher__proj"]
    workspace = "/home/u/proj"
    engine = "researcher"
    professions = ["code"]
    specialties = ["auto", "firewall"]
    policies = ["web-research"]

TOML like every other launcher-authored config (`.lego`, `tag.info`,
`tag.docker`, `combos.info`). Entries store tag **names** (strings) —
display shortnames and tag objects are resolved against the registry at
read time. Full-replacement semantics: an entry wins over the agent's
`.lego` defaults wholesale; a missing entry means "fresh — open the form
on the `.lego` pre-picks". `workspace` / `engine` are simply omitted when
unset (TOML has no null); readers see the absent key as None.

Reading goes through stdlib `tomllib`; writing through the small emitter
below — the schema is flat and fixed (string + string-list values only),
so a dependency-free serializer stays ~20 lines. Deliberately cache-free:
load reads the (small) file each call, so it's trivially testable
(functions take an explicit `path`) and there's no stale cache across the
picker's several reads. Callers follow load → mutate → save; `write_text`
makes the save atomic.

(One-shot conversions FROM retired on-disk formats live in
`tags/migrations.py` — this module only speaks the current format.)
"""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path
from typing import Any

from ..file_access import path_exists, read_text, write_text
from ..paths import INSTANCES_FILE
from .lego import AgentBuild

_FILE_HEADER = (
    "# Per-instance tag selections — one table per <agent>__<session>.\n"
    "# Launcher-owned: rewritten on every launch/modify; the picker's F2 form\n"
    "# is the supported editor. An entry fully replaces the agent's .lego\n"
    "# defaults; deleting an entry re-opens the form on those defaults.\n"
)

# TOML bare keys: letters/digits/underscore/dash. Anything else (a future
# dotted agent name, say) gets basic-string quoting so the file stays valid.
_BARE_KEY_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def _toml_key(key: str) -> str:
    return key if _BARE_KEY_RE.match(key) else json.dumps(key)


def _toml_str(value: str) -> str:
    # JSON string escaping is a subset of TOML basic-string escaping
    # (\" \\ \n \t \uXXXX …), so json.dumps yields a valid TOML string.
    return json.dumps(value)


def dumps(mapping: dict[str, dict[str, Any]]) -> str:
    """Serialize the store: header comment, then one key-sorted table per
    instance. Only the shapes this store holds are supported — optional
    strings (`workspace`, `engine`; omitted when None) and string lists
    (the three axes)."""
    blocks = [_FILE_HEADER]
    for instance_id in sorted(mapping):
        entry = mapping[instance_id]
        lines = [f"[{_toml_key(instance_id)}]"]
        for key in ("workspace", "engine"):
            if entry.get(key) is not None:
                lines.append(f"{key} = {_toml_str(entry[key])}")
        for axis in ("professions", "specialties", "policies"):
            values = ", ".join(_toml_str(v) for v in entry.get(axis, []))
            lines.append(f"{axis} = [{values}]")
        blocks.append("\n".join(lines) + "\n")
    return "\n".join(blocks)


def load(path: Path | None = None) -> dict[str, dict[str, Any]]:
    """The store as `{instance_id: entry}`; `{}` when the file is missing or
    empty. `path` defaults to INSTANCES_FILE at call time (tests patch the
    module attribute). A malformed file raises tomllib.TOMLDecodeError —
    `python -m launch.audit` reports the same corruption non-fatally."""
    path = path or INSTANCES_FILE
    if not path_exists(path):
        return {}
    text = read_text(path).strip()
    return tomllib.loads(text) if text else {}


def save(mapping: dict[str, dict[str, Any]], path: Path | None = None) -> None:
    """Persist the store as key-sorted TOML (atomic via write_text)."""
    write_text(path or INSTANCES_FILE, dumps(mapping))


def entry_to_build(entry: dict[str, Any]) -> AgentBuild:
    """A store entry's axis names as an `AgentBuild` (the same shape a `.lego`
    parses to, so the form and resolve paths are shared between fresh creates
    and stored instances)."""
    return AgentBuild(
        engine=entry.get("engine"),
        professions=tuple(entry.get("professions", [])),
        specialties=tuple(entry.get("specialties", [])),
        policies=tuple(entry.get("policies", [])),
    )


def build_entry(build: AgentBuild, workspace: str | None) -> dict[str, Any]:
    """The store-entry dict for an instance's build + workspace (inverse of
    `entry_to_build`). None values stay in the dict — `dumps` omits them at
    the file boundary."""
    return {
        "workspace":   workspace,
        "engine":      build.engine,
        "professions": list(build.professions),
        "specialties": list(build.specialties),
        "policies":    list(build.policies),
    }
