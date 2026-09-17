"""One-shot conversions of the user's `~/.ai-agents` state files from
retired on-disk formats into the current `instances.toml` store.

DELIBERATELY ISOLATED: this is the only module that knows retired formats
exist. Everything else in the launcher reads and writes exclusively the
current store via `tags.store`; when a format retires, its knowledge moves
here (and eventually ages out entirely) instead of leaking guards across
the codebase.

Currently handled — the state dir's old NAME (`~/.claude-agents`, until
2026-09-14: one AI, one harness): when the new dir is absent and the old one
present, the old one is renamed into place — one move, nothing copied, so a
running container's bind mounts (which follow the directory, not its path)
survive. Both present → neither is touched, and the launch says so.

The Claude Code login pair's old HOME at the state root (until 2026-09-15):
moved under `credentials/claude-code/`, file by file, when the new place is
empty.

And the pre-tags two-map format:
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

from .. import paths
from ..ai import CLAUDE_CODE
from ..file_access import ensure_dir, is_dir, is_file, login_state, make_private, move_path, path_exists, read_text
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


RETIRED_STATE_DIR_NAME = ".claude-agents"   # the state dir's name until 2026-09-14
SUPERSEDED_SUFFIX = ".superseded.bak"       # an old-location login file outranked by a newer one at the new place
REPLACED_SUFFIX = ".replaced.bak"           # a new-place file that recorded no login, set aside when the real one moved in


def relocate_state_dir() -> None:
    """Rename a `~/.claude-agents` left by an older launcher to the current
    state dir when the current one does not exist yet. Reads `paths.AGENTS_STATE`
    at CALL time (a test redirects it), so the old dir is its sibling under
    the same parent. Both present: hands off — the user decides which holds
    the truth — but say so, every launch, until one is gone."""
    new = paths.AGENTS_STATE
    old = new.parent / RETIRED_STATE_DIR_NAME
    if not is_dir(old):
        return
    if path_exists(new):
        print(f"  Note: both {old} and {new} exist — the launcher uses {new.name}; "
              f"merge or remove the old {old.name} when convenient")
        return
    move_path(old, new)
    print(f"  Renamed {old} → {new} (the launcher's state dir since 2026-09-14)")


def relocate_credentials() -> None:
    """Move the Claude Code login files an older launcher kept at the state
    root (`.claude.json`, `.credentials.json` — until 2026-09-15 the only
    harness, so the only pair) under `credentials/claude-code/`, where every
    harness's auth files live now (plans/credentials.md). Per file — a crash
    between the two completes on the next run — over nothing, a blank, or a
    file recording no login (kept as `.replaced.bak`); a file recording a
    login is never overwritten (the old one is set aside as
    `.superseded.bak`), and an unreadable one — a CLI mid-refresh — is never
    touched. Runs first, before any blank is touched, on every entry path
    (`startup.open_launcher`)."""
    for f in CLAUDE_CODE.auth_files:
        old, new = paths.AGENTS_STATE / f.name, paths.credentials_dir(CLAUDE_CODE.key) / f.name
        if not is_file(old):
            continue
        state = login_state(new, f)
        if state == "recorded":
            # Both record a login: the newer one is at the new place — never
            # overwrite a token with an older one. The old file is superseded:
            # renamed aside (a standing old file is what a later race would
            # move over a live one), kept as a .bak for the operator.
            move_path(old, old.with_name(f"{old.name}{SUPERSEDED_SUFFIX}"))
            print(f"  Note: {old.name} at the state root recorded an older login than "
                  f"{new.relative_to(paths.AGENTS_STATE)} — kept aside as {old.name}{SUPERSEDED_SUFFIX}")
            continue
        if state == "unreadable":
            # Not JSON — a CLI may be refreshing it in place this instant.
            # Never move over it; say so, and leave both for the next launch.
            print(f"  Note: {new.relative_to(paths.AGENTS_STATE)} cannot be read (a CLI may be writing it) — "
                  f"{old.name} at the state root left in place for now; if it stays unreadable, remove it")
            continue
        # Nothing, a blank, or a file a never-logged-in CLI wrote into the
        # blank (2026-09-15: a cluster launched without this migration handed
        # its members blanks; their Claude Code filled `.claude.json` with
        # startup state and no account). A `none` file is replaced, but kept:
        # `login_key` is the CLI's private shape, so a misjudged file costs a
        # copy, never a login.
        ensure_dir(new.parent)
        if state == "none":
            move_path(new, new.with_name(f"{new.name}{REPLACED_SUFFIX}"))
        try:
            move_path(old, new)   # a rename: over a blank it replaces it atomically; a running container keeps the inode
        except FileNotFoundError:
            continue              # a concurrent launcher moved it first — done
        make_private(new)
        print(f"  Moved {old.name} → {new.relative_to(paths.AGENTS_STATE)} (credentials live per harness since 2026-09-15)")


def ensure_migrated() -> None:
    """One-shot legacy migrations, called at launcher startup (before anything
    reads state): first the state dir's name, then the retired map format.
    reads the store). See the module docstring for the trigger conditions."""
    relocate_state_dir()
    relocate_credentials()
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
