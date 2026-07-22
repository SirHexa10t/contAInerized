"""One-shot conversions of the user's `~/.claude-agents` state files from
retired on-disk formats into the current `instances.toml` store.

DELIBERATELY ISOLATED: this is the only module that knows retired formats
exist. Everything else in the launcher reads and writes exclusively the
current store via `tags.store`; when a format retires, its knowledge moves
here (and eventually ages out entirely) instead of leaking guards across
the codebase.

Currently handled — the pre-tags two-map format:
  agent_workspace_map.json   {instance_id: workspace_path_or_null}
  agent_modes_map.json       {instance_id: [mode, ...]}   modes ∈ web/auto/DooD

`ensure_migrated` runs once at launcher startup: if `instances.toml`
doesn't exist but either legacy map does, the maps fold into the store and
the originals are renamed `*.pre-rewrite.bak`. Every later launch (store
present) and every fresh install (no maps) is a no-op.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..file_access import path_exists, read_text
from ..paths import AGENTS_DIR, AGENTS_STATE, INSTANCES_FILE
from . import store
from .lego import load_lego

AGENT_WORKSPACE_MAP_FILE = AGENTS_STATE / "agent_workspace_map.json"
AGENT_MODES_MAP_FILE = AGENTS_STATE / "agent_modes_map.json"

# Legacy mode string → [(axis, tag-name), ...]. `web` was a mode but is a
# profession now; `auto`/`DooD` are specialties (lowercased). Legacy `auto`
# bundled the firewall, which is its own specialty post-split — so it
# translates to BOTH {auto} and {firewall}, preserving the launch behavior
# the entry was recorded with.
_MODE_TRANSLATION: dict[str, list[tuple[str, str]]] = {
    "web":  [("professions", "webdev")],
    "auto": [("specialties", "auto"), ("specialties", "firewall")],
    "DooD": [("specialties", "dood")],
    "dood": [("specialties", "dood")],
}


def ensure_migrated() -> None:
    """One-shot legacy migration, called at launcher startup (before anything
    reads the store). See the module docstring for the trigger conditions."""
    if path_exists(INSTANCES_FILE):
        return
    legacy = [p for p in (AGENT_WORKSPACE_MAP_FILE, AGENT_MODES_MAP_FILE) if path_exists(p)]
    if not legacy:
        return

    def read_map(path: Path) -> dict[str, Any]:
        text = read_text(path).strip() if path_exists(path) else ""
        return json.loads(text) if text else {}

    store.save(migrate_from_maps(read_map(AGENT_WORKSPACE_MAP_FILE),
                                 read_map(AGENT_MODES_MAP_FILE), AGENTS_DIR))
    for path in legacy:
        path.rename(path.with_suffix(path.suffix + ".pre-rewrite.bak"))
    print(f"  Migrated legacy instance maps into {INSTANCES_FILE.name} "
          f"({', '.join(p.name for p in legacy)} → *.pre-rewrite.bak)")


def migrate_from_maps(workspace_map: dict[str, Any], modes_map: dict[str, list[str]],
                      agents_dir: Path) -> dict[str, dict[str, Any]]:
    """Build the store mapping from the two legacy maps.

    For each instance across both maps: start from its agent's `.lego`
    defaults (engine + professions + policies), overlay the legacy modes
    translated onto the right axis (`web` → professions, `auto`/`DooD` →
    specialties), and attach the stored workspace. The agent is the part of
    the instance id before `__`. This preserves each instance's effective
    behavior — a `["auto"]` instance becomes `specialties: ["auto",
    "firewall"]` on top of its agent's code/engine defaults."""
    out: dict[str, dict[str, Any]] = {}
    for instance_id in sorted(set(workspace_map) | set(modes_map)):
        agent = instance_id.split("__", 1)[0]
        build = load_lego(agents_dir / f"{agent}.lego")
        professions = list(build.professions)
        specialties: list[str] = []
        policies = list(build.policies)
        for mode in modes_map.get(instance_id, []):
            for axis, name in _MODE_TRANSLATION.get(mode, []):
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
