"""`instances.json` — the per-instance store (tags edition).

Folds the two legacy maps (`agent_workspace_map.json` + `agent_modes_map.json`)
into one file keyed by instance id:

    {"researcher__proj": {"workspace": "/home/u/proj", "engine": "researcher",
                          "professions": ["code"], "specialties": ["auto"],
                          "policies": ["web-research"]}}

Entries store tag **names** (strings) — display shortnames and tag objects are
resolved against the registry at read time. Full-replacement semantics: an
entry wins over the agent's `.lego` defaults wholesale; a missing entry means
"fresh — open the form on the `.lego` pre-picks".

Deliberately cache-free: load reads the (small) file each call, so it's
trivially testable (functions take an explicit `path`) and there's no stale
cache across the picker's several reads. Callers follow load → mutate → save,
same as the launcher's other JSON writers; `write_text` makes the save atomic.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..file_access import path_exists, read_text, write_text
from ..paths import (
    AGENT_MODES_MAP_FILE, AGENT_WORKSPACE_MAP_FILE, AGENTS_DIR, INSTANCES_FILE,
)
from .lego import AgentBuild, load_lego

# Legacy mode string → (axis, tag-name) for the one-time migration. `web` was a
# mode but is a profession now; `auto`/`DooD` are specialties (lowercased).
_MODE_TRANSLATION: dict[str, tuple[str, str]] = {
    "web":  ("professions", "web"),
    "auto": ("specialties", "auto"),
    "DooD": ("specialties", "dood"),
    "dood": ("specialties", "dood"),
}


def load(path: Path | None = None) -> dict[str, dict[str, Any]]:
    """The store as `{instance_id: entry}`; `{}` when the file is missing or
    empty. `path` defaults to INSTANCES_FILE at call time (tests patch the
    module attribute)."""
    path = path or INSTANCES_FILE
    if not path_exists(path):
        return {}
    text = read_text(path).strip()
    return json.loads(text) if text else {}


def save(mapping: dict[str, dict[str, Any]], path: Path | None = None) -> None:
    """Persist the store as pretty, key-sorted JSON (atomic via write_text)."""
    write_text(path or INSTANCES_FILE, json.dumps(mapping, indent=2, sort_keys=True) + "\n")


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
    `entry_to_build`)."""
    return {
        "workspace":   workspace,
        "engine":      build.engine,
        "professions": list(build.professions),
        "specialties": list(build.specialties),
        "policies":    list(build.policies),
    }


def ensure_migrated() -> None:
    """One-shot legacy migration, called at launcher startup: if
    `instances.json` doesn't exist but either legacy map does, fold the two
    maps into the new store (via `migrate_from_maps`) and rename the legacy
    files to `*.pre-rewrite.bak`. No-op on every later launch (the store
    exists) and on fresh installs (nothing to migrate)."""
    if path_exists(INSTANCES_FILE):
        return
    legacy = [p for p in (AGENT_WORKSPACE_MAP_FILE, AGENT_MODES_MAP_FILE) if path_exists(p)]
    if not legacy:
        return

    def read_map(path: Path) -> dict[str, Any]:
        text = read_text(path).strip() if path_exists(path) else ""
        return json.loads(text) if text else {}

    save(migrate_from_maps(read_map(AGENT_WORKSPACE_MAP_FILE),
                           read_map(AGENT_MODES_MAP_FILE), AGENTS_DIR))
    for path in legacy:
        path.rename(path.with_suffix(path.suffix + ".pre-rewrite.bak"))
    print(f"  Migrated legacy instance maps into {INSTANCES_FILE.name} "
          f"({', '.join(p.name for p in legacy)} → *.pre-rewrite.bak)")


def migrate_from_maps(workspace_map: dict[str, Any], modes_map: dict[str, list[str]],
                      agents_dir: Path) -> dict[str, dict[str, Any]]:
    """Build the `instances.json` mapping from the two legacy maps (run once,
    at first launch of the tags-era launcher).

    For each instance across both maps: start from its agent's `.lego`
    defaults (engine + professions + policies), overlay the legacy modes
    translated onto the right axis (`web` → professions, `auto`/`DooD` →
    specialties), and attach the stored workspace. The agent is the part of
    the instance id before `__`. This preserves each instance's effective
    behavior — a `["auto"]` instance becomes `specialties: ["auto"]` on top
    of its agent's code/engine defaults."""
    out: dict[str, dict[str, Any]] = {}
    for instance_id in sorted(set(workspace_map) | set(modes_map)):
        agent = instance_id.split("__", 1)[0]
        build = load_lego(agents_dir / f"{agent}.lego")
        professions = list(build.professions)
        specialties: list[str] = []
        policies = list(build.policies)
        for mode in modes_map.get(instance_id, []):
            axis, name = _MODE_TRANSLATION.get(mode, ("", ""))
            if axis == "professions" and name not in professions:
                professions.append(name)
            elif axis == "specialties" and name not in specialties:
                specialties.append(name)
        out[instance_id] = {
            "workspace":   workspace_map.get(instance_id),
            "engine":      build.engine or agent,
            "professions": professions,
            "specialties": specialties,
            "policies":    policies,
        }
    return out
