"""Agent state CRUD, tags edition: every operation that mutates the launcher's
persistent per-instance state, plus the factories that turn on-disk state into
the identity shapes the picker and run.py consume.

Sections:
  - list_all_instances — scan AGENTS_STATE for `<agent>__<session>` dirs
  - persist_instance / delete_instance / modify_instance — instances.toml
    writers (load → mutate → save over tags.store) + state-dir lifecycle
  - install_latest_md — source `.md` + chain-keyed addendum section →
    state-dir CLAUDE.md in one overwrite (tags.addendums supplies the text)
  - resolve_pick — name string → Agent (create) | Instance (cont) factory
    used by run.py's CLI parsing
  - creatable_agents / instance_from_store — picker-entry factories
  - _agent_sort_key — Create-row ordering (profession group, then the
    engine's model family via tags.engine.engine_sort_key, then name)

Identity types (Agent / Instance) and the store primitives live in the tags
package; this module wires them to the filesystem lifecycle. menu_picker and
run.py import from here; nothing here imports them back.
"""

import json

from .file_access import force_remove, is_dir, iter_subdirs, move_path, path_exists, read_text, write_text
from .paths import (
    AGENTS_DIR, AGENTS_STATE, BASE_SETTINGS_FILE, instance_state_dir_path,
    state_settings_path,
)
from .tags import (
    Agent, Instance, Registry, addendums, load_agent, resolve_build, store,
)
from .tags.engine import engine_sort_key
from .tags.identity import SESSION_SEP
from .tags.policy import merge_fragments
from .utils import ordering_index_or_end, prompt_keypress


def list_all_instances() -> list[str]:
    """Every `{agent}__{session}` dir under AGENTS_STATE (filesystem order;
    callers that need a specific order sort themselves). Empty list on a fresh
    install — iter_subdirs is None-safe so the missing-AGENTS_STATE case
    folds through naturally."""
    return [d.name for d in iter_subdirs(AGENTS_STATE) if SESSION_SEP in d.name]


# ============================================================
# instances.toml writers (load → mutate → save over tags.store)
# ============================================================

def persist_instance(inst: Instance) -> None:
    """Write/replace this instance's store entry (workspace + all four axes).
    Full-replacement semantics: the entry IS the instance's configuration;
    `.lego` defaults only matter when no entry exists yet."""
    mapping = store.load()
    mapping[inst.instance] = store.build_entry(inst.build, inst.workspace)
    store.save(mapping)


def delete_instance(inst: Instance) -> None:
    """Remove the instance's state dir and its store entry. Path removal goes
    through `force_remove(name=...)` (logs; sudo fallback for root-owned
    docker leftovers). On failure the store entry is left in place and we
    gate on a keypress so the user reads the failure before the picker
    redraws. Already-gone state dirs count as success so the entry still
    gets cleaned up."""
    if not force_remove(inst.state_dir, name=inst.instance):
        prompt_keypress(
            header=f"Could not remove '{inst.instance}' — see the messages above.",
            body=["Its instances.toml entry was left in place;",
                  "remove the directory manually, then delete the instance again."],
        )
        return
    mapping = store.load()
    mapping.pop(inst.instance, None)
    store.save(mapping)


def modify_instance(old: Instance, new: Instance) -> None:
    """Move an instance's state dir to its new identity (renaming when the id
    differs) and replace its store entry. The entry is always rewritten so
    callers can change axes/workspace without renaming."""
    if new.instance != old.instance:
        if path_exists(new.state_dir):
            raise ValueError(f"Instance '{new.instance}' already exists.")
        move_path(old.state_dir, new.state_dir)
    mapping = store.load()
    mapping.pop(old.instance, None)
    mapping[new.instance] = store.build_entry(new.build, new.workspace)
    store.save(mapping)


# ============================================================
# Per-instance state-dir writers
# ============================================================

def install_settings(inst: Instance) -> None:
    """Merge the shared base settings (settings/settings.json) with the
    instance's policy fragments into `<state>/settings.json`, refreshed each
    launch. docker_config.set_container_mounts RO-mounts the result over
    `~/.claude/settings.json` in-container, so the agent reads its policies
    but can't relax them (the mount shadows the state-dir's rw view of the
    same path). Policy-vs-policy or policy-vs-base scalar conflicts abort
    the launch via merge_fragments' TagError, naming both culprits."""
    fragments = [(BASE_SETTINGS_FILE.name + " (base)", json.loads(read_text(BASE_SETTINGS_FILE)))]
    fragments += [(p.name, p.load_fragment()) for p in inst.policies]
    merged = merge_fragments(fragments)
    write_text(state_settings_path(inst.state_dir), json.dumps(merged, indent=2, sort_keys=True) + "\n")


def install_latest_md(inst: Instance) -> None:
    """Write the agent's source `.md` plus the active-tag addendum section
    into the state dir as CLAUDE.md, in a single overwrite. Refreshed each
    launch so a source-side edit AND any tag toggle both propagate. The
    result is launcher-owned: whatever a previous launch wrote is replaced
    wholesale, no marker-based reconciliation."""
    body = read_text(inst.md_path)
    addendum = addendums.compose(inst.active_tags)
    write_text(inst.state_md, f"{body}\n\n{addendum}" if addendum else body)


def _agent_sort_key(agent: Agent, registry: Registry) -> tuple[tuple[int, ...], tuple[int, tuple[int, int]], str]:
    """Create-row ordering: profession-less agents first (then by each
    profession's registry position), engine model family/version within a
    group, name as the tiebreak."""
    prof_order = list(registry.professions)
    prof_key = tuple(sorted(ordering_index_or_end(p, prof_order) for p in agent.build.professions))
    engine = registry.engines.get(agent.build.engine or agent.name) or registry.engines.get("default")
    model = engine.conf_map.get("ANTHROPIC_MODEL", "") if engine else ""
    return (prof_key, engine_sort_key(model), agent.name)


# ============================================================
# Identity factories — name string / disk state → Agent | Instance
# ============================================================

def instance_from_store(instance_id: str, registry: Registry) -> Instance | None:
    """Rehydrate a stored/continuing instance: its store entry (or, for a
    pre-store instance dir, its agent's `.lego` defaults) resolved into tag
    objects. None when the agent's `.md` is gone (orphan state dir). A store
    entry referencing an unknown tag fails loud via validate_build — same
    contract as `.lego` references."""
    agent_name, _, session = instance_id.partition(SESSION_SEP)
    agent = load_agent(agent_name, AGENTS_DIR)
    if agent is None:
        return None
    entry = store.load().get(instance_id)
    build = store.entry_to_build(entry) if entry else agent.build
    registry.validate_build(build, f"instances.toml[{instance_id}]")
    return Instance(
        agent=agent_name,
        md_path=agent.md_path,
        session=session,
        workspace=entry.get("workspace") if entry else None,
        is_brand_new=False,
        **resolve_build(build, agent_name, registry),
    )


def resolve_pick(name: str | None, registry: Registry) -> Agent | Instance | None:
    """Resolve a CLI name string into what the picker would have returned:
        '<agent>__<session>' with a state dir on disk → Instance (cont)
        '<agent>'           with a matching `.md`     → Agent (create)
    None if `name` is None/empty or neither matches (typo, orphan dir). The
    None-safe input lets parse_cli pass `args.target` through unguarded."""
    if not name:
        return None
    if SESSION_SEP in name and is_dir(instance_state_dir_path(name)):
        inst = instance_from_store(name, registry)
        if inst is not None:
            return inst
    return load_agent(name, AGENTS_DIR)


def creatable_agents(registry: Registry) -> list[Agent]:
    """Agents for the picker's Create rows — every `.md` in AGENTS_DIR with
    its `.lego` defaults attached, sorted by profession group then engine
    capability then name."""
    from .file_access import agent_md_index
    out = [a for name in agent_md_index() if (a := load_agent(name, AGENTS_DIR))]
    out.sort(key=lambda a: _agent_sort_key(a, registry))
    return out
