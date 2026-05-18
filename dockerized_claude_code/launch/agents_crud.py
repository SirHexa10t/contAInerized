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
constants), structs, utils, and stdlib; nothing from agent_composition,
run.py, or menu_picker — all of those import from here.
"""

import re
from pathlib import Path

from .agent_composition import warn_if_dangerous_modes
from .file_access import (
    force_remove, is_dir, iter_subdirs, load_conf, load_modes_map,
    load_workspace_map, move_path, path_exists, read_text, save_modes_map,
    save_workspace_map, write_text,
)
from .memory_addendums import composed_addendum
from .paths import AGENT_MD_FILES, AGENTS_STATE, instance_state_dir_path
from .structs import AgentIdentity, InstanceModifiers, SESSION_SEP, SessionIdentity
from .utils import ordering_index_or_end, parse_agent_name


# Name → md-path index over AGENT_MD_FILES (the launcher's view of agents/ at
# import time). The picker's resolve_pick + audit's per-instance check + every
# AgentIdentity.md_path property access goes through this dict.
AGENT_MD_BY_NAME: dict[str, Path] = {parse_agent_name(p.stem): p for p in AGENT_MD_FILES}


def list_all_instances() -> list[str]:
    """Return every `{agent}__{session}` dir under AGENTS_STATE (filesystem order;
    callers that need a specific order sort themselves)."""
    if not path_exists(AGENTS_STATE):
        return []
    return [d.name for d in iter_subdirs(AGENTS_STATE) if SESSION_SEP in d.name]


# ============================================================
# Single-entry writers
# ============================================================

def update_workspace_map(inst_id) -> None:
    """Persist (inst_id.instance → inst_id.workspace) in agent_workspace_map.json.
    No-op if the entry is already set to this value — avoids rewriting the file on
    every cont relaunch when the workspace hasn't changed."""
    m = load_workspace_map()
    if m.get(inst_id.instance) != inst_id.workspace:
        m[inst_id.instance] = inst_id.workspace
        save_workspace_map(m)


def _write_modes_entry(m: dict, sess_id) -> None:
    """Mutate `m` (a modes-map dict) to reflect sess_id's modes for sess_id.instance:
    set the list when modes are non-empty, pop the entry otherwise. Pure dict
    mutation — callers bracket with their own load/save so a multi-edit pass
    can batch into a single disk write (see modify_instance)."""
    if sess_id.modes:
        m[sess_id.instance] = list(sess_id.modes)
    else:
        m.pop(sess_id.instance, None)


def set_instance_modes(sess_id) -> None:
    """Persist the modes list for an instance, taken off the passed SessionIdentity
    (which carries both the instance key and its resolved modes). An empty modes
    tuple removes the entry from the map (we don't store empty entries — keeps the
    file small and the 'no modes' case explicit by absence). Routes through
    agent_composition.warn_if_dangerous_modes for the {auto}+{DooD} warning —
    the dangerous-combination judgement lives with modifier semantics, this
    writer just persists state and triggers the post-write check."""
    m = load_modes_map()
    _write_modes_entry(m, sess_id)
    save_modes_map(m)
    warn_if_dangerous_modes(sess_id.modes)


# ============================================================
# Per-instance state-dir writers
# ============================================================

def install_latest_md(sess_id) -> None:
    """Write the agent's source `.md` plus the active-chain addendum section
    into the state dir as CLAUDE.md, in a single overwrite. Refreshed each
    launch so a source-side edit AND any modifier toggle both propagate
    without a separate splice step. write_text auto-creates the destination's
    parent directory tree. The result is launcher-owned: a stale wrapper or
    legacy block from a previous launch is replaced wholesale, no marker-
    based reconciliation needed."""
    body = read_text(sess_id.md_path)
    addendum = composed_addendum(sess_id.chain)
    write_text(sess_id.state_md, f"{body}\n\n{addendum}" if addendum else body)


def delete_instance(inst_id) -> None:
    """Remove this instance's state dir and its workspace + modes mapping entries.
    Path removal goes through `force_remove(name=...)` which logs the removal,
    handles root-owned Docker bind-mount leftovers via sudo, and pauses for
    keypress on failure. Already-gone state dirs are treated as success so the
    map entries are still cleaned up."""
    if not force_remove(inst_id.state_dir, name=inst_id.instance):
        return   # force_remove printed errors and waited for keypress
    m = load_workspace_map()
    if inst_id.instance in m:
        del m[inst_id.instance]
        save_workspace_map(m)
    m = load_modes_map()
    if inst_id.instance in m:
        del m[inst_id.instance]
        save_modes_map(m)


def modify_instance(old_inst_id, new_sess_id) -> None:
    """Move an instance's state dir to its new SessionIdentity (renaming if the
    instance id differs) and update both the workspace and modes mappings to
    match. No-op for the rename if old and new ids match; the maps are always
    rewritten so callers can change modes/workspace without renaming."""
    renaming = new_sess_id.instance != old_inst_id.instance
    if renaming:
        if path_exists(new_sess_id.state_dir):
            raise ValueError(f"Instance '{new_sess_id.instance}' already exists.")
        move_path(old_inst_id.state_dir, new_sess_id.state_dir)
    # workspace map
    m = load_workspace_map()
    if renaming:
        m.pop(old_inst_id.instance, None)
    m[new_sess_id.instance] = new_sess_id.workspace
    save_workspace_map(m)
    # modes map — single load/save (mirrors set_instance_modes' shape via the
    # shared helpers so a rename costs one file write instead of two), plus the
    # same {auto}+{DooD} warning at the end so both persistence paths surface it.
    m = load_modes_map()
    if renaming:
        m.pop(old_inst_id.instance, None)
    _write_modes_entry(m, new_sess_id)
    save_modes_map(m)
    warn_if_dangerous_modes(new_sess_id.modes)


# ============================================================
# Picker-entry sort keys
# ============================================================
# Used by creatable_agents (tag + family/version sort, here) and menu_picker's continuable_instances
# (mode + family/version/session sort). Kept here rather than in agent_composition
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


def agent_sort_key(item) -> tuple:
    """Sort by family (ORDERED_MODEL_FAMILIES order — opus first, haiku last),
    then version desc, then name asc. Agents whose .conf has no recognisable
    model sink past all known families via the sentinel index."""
    name, path = item
    _, conf = load_conf(path)
    family, major, minor = parse_model_id(conf.get("ANTHROPIC_MODEL", "")) or (None, 0, 0)
    return (ordering_index_or_end(family, ORDERED_MODEL_FAMILIES), (-major, -minor), name)


def tag_sort_key(tags) -> tuple[int, ...]:
    """Sort key for agents grouped by tag set, following InstanceModifiers.tags()
    declaration order. Untagged ([]) → empty tuple, which sorts before any non-
    empty key. Unknown tags sink past the end via a sentinel index so typo'd tags
    don't mix into the untagged group."""
    return tuple(sorted(ordering_index_or_end(t, InstanceModifiers.tag_values()) for t in tags))


def mode_sort_key(modes) -> tuple[int, ...]:
    """Sort key for instances grouped by mode set, following InstanceModifiers.modes()
    declaration order. Mode-less ([]) → empty tuple, which sorts before any non-empty
    key. Unknown modes sink past the end via a sentinel index."""
    return tuple(sorted(ordering_index_or_end(m, InstanceModifiers.mode_values()) for m in modes))


# ============================================================
# Identity factories — name-string / disk-scan → identity
# ============================================================

def resolve_pick(name: str) -> AgentIdentity | SessionIdentity | None:
    """Resolve a name string into an identity matching select_agent's return shape.
    Two cases:
        '<agent>__<session>' with a state dir on disk → SessionIdentity (is_brand_new=False)
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
            return SessionIdentity(
                agent=agent,
                session=session,
                workspace=load_workspace_map().get(name),
                is_brand_new=False,
                modes=tuple(load_modes_map().get(name, [])),
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
    out = [AgentIdentity(agent=parse_agent_name(p.stem)) for p in AGENT_MD_FILES]
    out.sort(key=lambda a: (
        tag_sort_key(a.tags),                       # untagged sinks to top; rest follow InstanceModifiers.tags() positions
        agent_sort_key((a.agent, a.md_path)),       # within each tag group: family/version/name
    ))
    return out


