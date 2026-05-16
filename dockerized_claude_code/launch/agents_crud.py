"""Agent state CRUD: every operation that mutates the launcher's persistent
agent state, plus the factories that turn raw on-disk state into the identity
/ picker-entry shapes the rest of the launcher consumes.

Roughly grouped by section in this file:
  - list_all_instances — scan AGENTS_STATE for `<agent>__<session>` dirs
  - update_workspace_map / set_instance_modes — single-entry writers
  - warn_dood_with_auto — interactive prompt used by mode writers
  - install_latest_md / delete_instance / modify_instance — per-instance
    state-dir writers (sync_memory_templates is in agent_composition since it
    keys off the modifier taxonomy)
  - resolve_pick — name-string → identity factory used by run.py's CLI parsing
  - creatable_agents / continuable_instances — picker entry dict factories the
    menu_picker UI consumes

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

from .agent_composition import warn_if_dangerous_modes
from .file_access import (
    copy_file, find_md_for_agent, force_remove, is_dir, iter_subdirs,
    load_conf, load_modes_map, load_workspace_map, move_path, parse_stem,
    path_exists, read_text, resolved_cwd, resolved_path,
    save_modes_map, save_workspace_map, write_text,
)
from .paths import (
    ACCOUNT_FILE, AGENTS_DIR, AGENTS_STATE, CREDENTIALS_FILE,
    DEFAULT_WORKSPACE, DEFAULTING_DIRS, MD_EXT, instance_state_dir_path,
)
from .structs import AgentIdentity, InstanceModifiers, SESSION_SEP, SessionIdentity
from .utils import ordering_index_or_end, relative_time

NO_WORKSPACE_DISPLAY = "?"            # subtitle placeholder when a Cont row's workspace map entry is missing or stale


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

def install_latest_md(inst_id) -> None:
    """Copy the agent's `.md` into the state dir as CLAUDE.md (refreshed each
    launch so a source-side edit propagates), and ensure the shared OAuth files
    exist so docker's bind-mount doesn't auto-create them as root. copy_file
    and write_text both auto-create the destination's parent directories as
    needed (via ensure_dir inside the primitives). Returns the state dir."""
    copy_file(inst_id.md_path, inst_id.state_md, overwrite_if_dest=True)
    if not path_exists(ACCOUNT_FILE):
        write_text(ACCOUNT_FILE, "{}")
    if not path_exists(CREDENTIALS_FILE):
        write_text(CREDENTIALS_FILE, "{}")
    return inst_id.state_dir


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
# Used by creatable_agents (tag + family/version sort) and continuable_instances
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
        if find_md_for_agent(agent) is not None:
            return SessionIdentity(
                agent=agent,
                session=session,
                workspace=load_workspace_map().get(name),
                is_brand_new=False,
                modes=tuple(load_modes_map().get(name, [])),
            )
    if find_md_for_agent(name) is not None:
        return AgentIdentity(agent=name)
    return None


def creatable_agents() -> list[dict]:
    """Agent dicts for the picker's Create rows. Sorted first by tag set
    (untagged first, then groups ordered by each tag's position in InstanceModifiers.tags());
    within each tag group, the existing model family/version sort applies. Each
    entry carries an AgentIdentity (`identity`) the picker hands back as the
    selection value, plus picker-private caches (`tags`, `md_path`, `md_text`)
    so sort comparisons and previews don't re-glob AGENTS_DIR per row."""
    out = []
    for path in AGENTS_DIR.glob(f"*{MD_EXT}"):
        name, tags, _ = parse_stem(path.stem)
        if name == "default":
            continue
        out.append({
            "identity": AgentIdentity(agent=name),    # the value the picker hands back on selection
            "tags": tags,                             # filename-grammar tags (e.g. ["prog"]); rendered prefixed in green by menu_picker
            "md_path": path,                          # display only — the picker uses this for the agents/ relative path in previews
            "md_text": read_text(path),               # raw .md content; menu_picker uses it for both description and preview
        })
    def _sort_key(d: dict) -> tuple:
        # Narrow the heterogeneous-dict access by binding the identity once;
        # mypy can't infer AgentIdentity off `d["identity"]` directly.
        ident: AgentIdentity = d["identity"]
        return (
            tag_sort_key(d["tags"]),                            # untagged sinks to top; rest follow InstanceModifiers.tags() positions
            agent_sort_key((ident.agent, d["md_path"])),        # within each tag group: family/version/name
        )
    out.sort(key=_sort_key)
    return out


def continuable_instances() -> list[dict]:
    """Instance dicts for the picker's Cont/DELETE rows. Orphans (missing .md) skipped.
    Sorted first by mode set (mode-less first, then groups ordered by each mode's
    position in InstanceModifiers.modes()); within each mode group, sorted by (agent rank,
    session) as before. Marks instances whose workspace resolves to the current
    working directory (for the picker's CURRENT DIR hint). Each entry carries a
    SessionIdentity (`identity`) — stored workspace + modes are baked in so the
    modify flow's pre-fill can read them straight off the identity. Remaining
    fields are display-only (workspace_display, modes_display, last_used_display,
    is_current_dir, is_default_dir) — the picker reads them and they never leave
    menu_picker."""
    # Symlinks normalized via .resolve() so e.g. /home/<user> matches /var/users/<user>
    # when one symlinks to the other. Subdirs deliberately don't count — being in a
    # project under $HOME doesn't make /ai_workspace your "default" workspace.
    cwd = resolved_cwd()
    defaulting_dir_active = cwd in {resolved_path(d) for d in DEFAULTING_DIRS}
    default_workspace_resolved = resolved_path(DEFAULT_WORKSPACE)
    workspace_map = load_workspace_map()
    modes_map = load_modes_map()

    out = []
    for dir_name in list_all_instances():
        agent, _, session = dir_name.partition(SESSION_SEP)
        if find_md_for_agent(agent) is None:
            continue
        modes = tuple(modes_map.get(dir_name, []))
        ws = workspace_map.get(dir_name)
        ws_resolved = resolved_path(ws) if ws and is_dir(ws) else None
        sess_id = SessionIdentity(agent=agent, session=session, workspace=ws, is_brand_new=False, modes=modes)
        last_mtime = sess_id.last_used_mtime
        out.append({
            "identity": sess_id,
            "modes_display":     ", ".join(modes) or "(none)",
            "workspace_display": ws if ws_resolved else NO_WORKSPACE_DISPLAY,    # "?" sentinel when the map entry is missing or stale
            "is_current_dir":    ws_resolved == cwd,
            "is_default_dir":    defaulting_dir_active and ws_resolved == default_workspace_resolved,    # cwd ∈ DEFAULTING_DIRS and ws matches DEFAULT_WORKSPACE — tagged `(DEFAULT DIR)` by menu_picker
            "last_used_display": relative_time(last_mtime) if last_mtime is not None else "(never)",
        })
    def _sort_key(d: dict) -> tuple:
        # Narrow the heterogeneous-dict access by binding the identity once;
        # mypy can't infer SessionIdentity off `d["identity"]` directly.
        sess: SessionIdentity = d["identity"]
        return (
            mode_sort_key(sess.modes),                          # mode-less sinks to top; rest follow InstanceModifiers.modes() positions
            agent_sort_key((sess.agent, sess.md_path)),         # within each mode group: family/version/name
            sess.session,                                       # then session for tiebreak between instances of the same agent
        )
    out.sort(key=_sort_key)
    return out
