"""Agent state CRUD: every operation that mutates the launcher's persistent
agent state, plus the factories that turn raw on-disk state into the identity
/ picker-entry shapes the rest of the launcher consumes.

Roughly grouped by section in this file:
  - list_all_instances — scan AGENTS_STATE for `<agent>__<session>` dirs
  - update_workspace_map / set_instance_modes — single-entry writers
  - warn_dood_with_auto — interactive prompt used by mode writers
  - install_latest_md / delete_instance / modify_instance — per-instance
    state-dir writers (install_latest_md composes the active-chain addendum
    section onto the source `.md` body and writes the result as CLAUDE.md
    in one go; memory_addendums.composed_addendum supplies the addendum)
  - resolve_pick — name-string → identity factory used by run.py's CLI parsing
  - creatable_agents — picker Create-row factory (returns list[AgentIdentity]).
    The Cont-row factory lives in menu_picker (its row type is picker-only).

The JSON map load/save primitives (load_workspace_map, save_modes_map, etc.)
live in file_access with module-level caches that refresh on save — this
module just imports them and uses the load-mutate-save pattern. structs.py
owns the dataclasses themselves (plus the InstanceModifiers taxonomy used
here for the auto+DooD warning and sort-key ordering); this module imports
those and constructs the dataclasses (resolve_pick + the picker builders).
Picker-side sort keys (agent_sort_key / mode_sort_key / tag_sort_key) live
here too — they're picker concerns, not composition concerns, so they sit
next to the picker-entry factories that consume them. Imports from
file_access (parse + map I/O + load_conf for the family sort), paths (path
constants), structs, utils, and stdlib; nothing from agent_modifiers_handler,
run.py, or menu_picker — all of those import from here.
"""

import re
from collections.abc import Iterable
from pathlib import Path

from .agent_modifiers_handler import warn_if_dangerous_modes
from .file_access import (
    force_remove, is_dir, iter_subdirs, load_conf, load_modes_map,
    load_workspace_map, move_path, path_exists, read_text, save_modes_map,
    save_workspace_map, write_text,
)
from .template_code.memory_addendums import composed_addendum
from .paths import AGENT_MD_BY_NAME, AGENTS_STATE, instance_state_dir_path
from .structs import AgentIdentity, InstanceIdentity, InstanceModifiers, SESSION_SEP
from .utils import ordering_index_or_end


def list_all_instances() -> list[str]:
    """Return every `{agent}__{session}` dir under AGENTS_STATE (filesystem order;
    callers that need a specific order sort themselves)."""
    if not path_exists(AGENTS_STATE):
        return []
    return [d.name for d in iter_subdirs(AGENTS_STATE) if SESSION_SEP in d.name]


# ============================================================
# Single-entry writers
# ============================================================

def update_workspace_map(inst_id: InstanceIdentity) -> None:
    """Persist (inst_id.instance → inst_id.workspace) in agent_workspace_map.json.
    No-op if the entry is already set to this value — avoids rewriting the file on
    every cont relaunch when the workspace hasn't changed."""
    m = load_workspace_map()
    if m.get(inst_id.instance) != inst_id.workspace:
        m[inst_id.instance] = inst_id.workspace
        save_workspace_map(m)


def _write_modes_entry(m: dict[str, list[str]], inst_id: InstanceIdentity) -> None:
    """Mutate `m` (a modes-map dict) to reflect inst_id's modes for inst_id.instance:
    set the list when modes are non-empty, pop the entry otherwise. Pure dict
    mutation — callers bracket with their own load/save so a multi-edit pass
    can batch into a single disk write (see modify_instance). Modes are
    serialized as their `.value` strings (JSON-friendly); inst_id.modes is a
    tuple of typed enum members on the in-memory side."""
    if inst_id.modes:
        m[inst_id.instance] = [mode.value for mode in inst_id.modes]
    else:
        m.pop(inst_id.instance, None)


def set_instance_modes(inst_id: InstanceIdentity) -> None:
    """Persist the modes list for an instance, taken off the passed InstanceIdentity
    (which carries both the instance key and its resolved modes). An empty modes
    tuple removes the entry from the map (we don't store empty entries — keeps the
    file small and the 'no modes' case explicit by absence). Routes through
    agent_modifiers_handler.warn_if_dangerous_modes for the {auto}+{DooD} warning —
    the dangerous-combination judgement lives with modifier semantics, this
    writer just persists state and triggers the post-write check."""
    m = load_modes_map()
    _write_modes_entry(m, inst_id)
    save_modes_map(m)
    warn_if_dangerous_modes(inst_id.modes)


# ============================================================
# Per-instance state-dir writers
# ============================================================

def install_latest_md(inst_id: InstanceIdentity) -> None:
    """Write the agent's source `.md` plus the active-chain addendum section
    into the state dir as CLAUDE.md, in a single overwrite. Refreshed each
    launch so a source-side edit AND any modifier toggle both propagate
    without a separate splice step. write_text auto-creates the destination's
    parent directory tree. The result is launcher-owned: a stale wrapper or
    legacy block from a previous launch is replaced wholesale, no marker-
    based reconciliation needed."""
    body = read_text(inst_id.md_path)
    addendum = composed_addendum(inst_id.chain)
    write_text(inst_id.state_md, f"{body}\n\n{addendum}" if addendum else body)


def delete_instance(inst_id: InstanceIdentity) -> None:
    """Remove this instance's state dir and its workspace + modes mapping entries.
    Path removal goes through `force_remove(name=...)` which logs the removal,
    handles root-owned Docker bind-mount leftovers via sudo, and pauses for
    keypress on failure. Already-gone state dirs are treated as success so the
    map entries are still cleaned up."""
    if not force_remove(inst_id.state_dir, name=inst_id.instance):
        return   # force_remove printed errors and waited for keypress
    workspace_map = load_workspace_map()
    if inst_id.instance in workspace_map:
        del workspace_map[inst_id.instance]
        save_workspace_map(workspace_map)
    modes_map = load_modes_map()
    if inst_id.instance in modes_map:
        del modes_map[inst_id.instance]
        save_modes_map(modes_map)


def modify_instance(old_inst_id: InstanceIdentity, new_inst_id: InstanceIdentity) -> None:
    """Move an instance's state dir to its new InstanceIdentity (renaming if the
    instance id differs) and update both the workspace and modes mappings to
    match. No-op for the rename if old and new ids match; the maps are always
    rewritten so callers can change modes/workspace without renaming."""
    renaming = new_inst_id.instance != old_inst_id.instance
    if renaming:
        if path_exists(new_inst_id.state_dir):
            raise ValueError(f"Instance '{new_inst_id.instance}' already exists.")
        move_path(old_inst_id.state_dir, new_inst_id.state_dir)
    # workspace map
    workspace_map = load_workspace_map()
    if renaming:
        workspace_map.pop(old_inst_id.instance, None)
    workspace_map[new_inst_id.instance] = new_inst_id.workspace
    save_workspace_map(workspace_map)
    # modes map — single load/save (mirrors set_instance_modes' shape via the
    # shared helpers so a rename costs one file write instead of two), plus the
    # same {auto}+{DooD} warning at the end so both persistence paths surface it.
    modes_map = load_modes_map()
    if renaming:
        modes_map.pop(old_inst_id.instance, None)
    _write_modes_entry(modes_map, new_inst_id)
    save_modes_map(modes_map)
    warn_if_dangerous_modes(new_inst_id.modes)


# ============================================================
# Picker-entry sort keys
# ============================================================
# Used by creatable_agents (tag + family/version sort, here) and menu_picker's continuable_instances
# (mode + family/version/session sort). Kept here rather than in agent_modifiers_handler
# because they're picker output concerns — composition handlers don't sort
# anything. Tag/mode position comes from InstanceModifiers (in structs);
# ORDERED_MODEL_FAMILIES is picker-only so it lives here as a local constant.

ORDERED_MODEL_FAMILIES = ["opus", "sonnet", "haiku"]
_FAMILY_RE = re.compile(rf"({'|'.join(ORDERED_MODEL_FAMILIES)})-(\d+)(?:-(\d+))?")


def parse_model_id(model: str) -> tuple[str, int, int] | None:
    """Extract (family, major, minor) from a model ID like 'claude-opus-4-7'.
    Returns None when no recognized family is present. The regex's family
    alternation is derived from ORDERED_MODEL_FAMILIES, so a new family means
    one list-entry change — no parallel regex to update."""
    m = _FAMILY_RE.search(model)
    if not m:
        return None
    return m.group(1), int(m.group(2)), int(m.group(3) or 0)


def agent_sort_key(item: tuple[str, Path]) -> tuple[int, tuple[int, int], str]:
    """Sort by family (ORDERED_MODEL_FAMILIES order — opus first, haiku last),
    then version desc, then name asc. Agents whose .conf has no recognisable
    model sink past all known families via the sentinel index."""
    name, path = item
    _, conf = load_conf(path)
    family, major, minor = parse_model_id(conf.get("ANTHROPIC_MODEL", "")) or (None, 0, 0)
    return (ordering_index_or_end(family, ORDERED_MODEL_FAMILIES), (-major, -minor), name)


def tag_sort_key(tags: Iterable[InstanceModifiers]) -> tuple[int, ...]:
    """Sort key for agents grouped by tag set, following InstanceModifiers.tags()
    declaration order. Untagged () → empty tuple, which sorts before any non-
    empty key. Tags are typed members here; we sort by each member's `.value`
    position in tag_values() (preserves the InstanceModifiers declaration order)."""
    return tuple(sorted(ordering_index_or_end(t.value, InstanceModifiers.tag_values()) for t in tags))


def mode_sort_key(modes: Iterable[InstanceModifiers]) -> tuple[int, ...]:
    """Sort key for instances grouped by mode set, following InstanceModifiers.modes()
    declaration order. Mode-less () → empty tuple, which sorts before any non-empty
    key. Modes are typed members here; we sort by each member's `.value` position
    in mode_values() (preserves the InstanceModifiers declaration order)."""
    return tuple(sorted(ordering_index_or_end(m.value, InstanceModifiers.mode_values()) for m in modes))


# ============================================================
# Identity factories — name-string / disk-scan → identity
# ============================================================

def resolve_pick(name: str) -> AgentIdentity | InstanceIdentity | None:
    """Resolve a name string into an identity matching select_agent's return shape.
    Two cases:
        '<agent>__<session>' with a state dir on disk → InstanceIdentity (is_brand_new=False)
        '<agent>'           with a matching .md       → AgentIdentity
    Returns None if neither matches (orphan state dir without .md, typo, etc.).

    The cont path packages stored workspace + modes into the identity so the
    downstream flow doesn't need a second pass over the maps; the workspace may
    still be None (missing-map entry) — resolve_target validates / re-prompts.
    Whether this is a brand-new vs continuing launch is encoded by the returned
    type (and by is_brand_new for InstanceIdentity-shaped picks), so callers
    don't carry a parallel `kind` string alongside.

    Used by run.py's parse_cli to convert sys.argv[1] into the same shape the
    picker would have returned, so launch's downstream flow is uniform."""
    if SESSION_SEP in name and is_dir(instance_state_dir_path(name)):
        agent, _, session = name.partition(SESSION_SEP)
        if agent in AGENT_MD_BY_NAME:
            # JSON-load boundary for modes: convert each string → enum member
            # via InstanceModifiers(s), which raises ValueError on unknowns
            # (fail-fast for defective modes-map entries).
            return InstanceIdentity(
                agent=agent,
                session=session,
                workspace=load_workspace_map().get(name),
                is_brand_new=False,
                modes=tuple(InstanceModifiers.from_value(s) for s in load_modes_map().get(name, [])),
            )
    if name in AGENT_MD_BY_NAME:
        return AgentIdentity(agent=name)
    return None


def creatable_agents() -> list[AgentIdentity]:
    """AgentIdentities for the picker's Create rows, sorted first by tag set
    (untagged first, then groups ordered by each tag's position in InstanceModifiers.tags());
    within each tag group, the existing model family/version sort applies.
    `AgentIdentity.tags` / `.md_path` are properties (the md_path lookup
    hits AGENT_MD_BY_NAME, parse_stem is cheap), so the sort doesn't need
    a side cache."""
    out = [AgentIdentity(agent=name) for name in AGENT_MD_BY_NAME]
    out.sort(key=lambda a: (
        tag_sort_key(a.tags),                       # untagged sinks to top; rest follow InstanceModifiers.tags() positions
        agent_sort_key((a.agent, a.md_path)),       # within each tag group: family/version/name
    ))
    return out


