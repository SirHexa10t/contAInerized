"""Agent state CRUD, tags edition: every operation that mutates the launcher's
persistent per-instance state, plus the factories that turn on-disk state into
the identity shapes the picker and run.py consume.

Sections:
  - list_all_instances — scan ~/.claude-agents/instances/ for `<agent>__<session>` dirs
  - persist_instance / delete_instance / modify_instance — instances.toml
    writers (load → mutate → save over tags.store) + state-dir lifecycle
  - install_latest_md — source `.md` + chain-keyed addendum section →
    state-dir CLAUDE.md in one overwrite (tags.addendums supplies the text)
  - compute_resume_flag — Instance → claude resume args (["--continue"] | [])
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

from .file_access import (
    copy_file, ensure_dir, force_remove, home_relative, is_dir, iter_subdirs,
    move_path, path_exists, read_text, write_text,
)
from .paths import (
    AGENTS_COMMANDS_DIR, AGENTS_DIR, BASE_SETTINGS_FILE, INSTANCES_FILE,
    SHARED_COMMANDS_DIR, instance_state_dir_path, instances_dir,
    state_commands_dir, state_settings_path,
)
from .tags import (
    Agent, Instance, Registry, TagError, addendums, load_agent, resolve_build,
    store,
)
from .tags.engine import engine_sort_key
from .tags.identity import SESSION_SEP
from .tags.policy import merge_fragments
from .utils import ordering_index_or_end, plural, prompt_keypress


def list_all_instances() -> list[str]:
    """Every `{agent}__{session}` dir under ~/.claude-agents/instances/
    (filesystem order; callers that need a specific order sort themselves).
    Empty list on a fresh install — or before the user has moved pre-existing
    instances into instances/ (audit's `stray` check flags those); iter_subdirs
    is None-safe, so a missing instances/ dir folds through as empty."""
    return [d.name for d in iter_subdirs(instances_dir()) if SESSION_SEP in d.name]


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

def install_commands(inst: Instance) -> None:
    """Assemble this instance's slash-command dir: the shared commands, plus every
    command its active tags DECLARE (`commands = [...]` in tag.info → the file
    `agents/_commands/<name>.md`).

    **Why assembled rather than mounted per tag.** The obvious approach — each tag
    mounting its own command file over `~/.claude/commands/<name>.md` — cannot
    work: that directory is itself a READ-ONLY mount, and docker cannot create a
    mountpoint inside one. The container dies at start with `mount: read-only file
    system`, naming a path but not the reason. So the launcher builds one directory
    and mounts that.

    **Why declared rather than shipped inside the tag dir.** One central dir means
    a command can be granted by several tags without duplicating the file, and
    every specialized command is findable in one place; the registry has already
    validated that each declared name resolves to a real file, so nothing here can
    miss. Two tags declaring the SAME command converge on one file — only a NAME
    collision between a tag command and a shared one is a fault, and a loud one:
    silently letting either shadow the other would ship a different command than
    one of its authors wrote.

    Rebuilt from scratch each launch (like `install_latest_md`) so a command
    removed from a tag, or a tag removed from the instance, actually disappears
    instead of lingering from a previous run."""
    destination = state_commands_dir(inst.state_dir)
    force_remove(destination)
    ensure_dir(destination)
    sources = {path.name: path for path in sorted(SHARED_COMMANDS_DIR.glob("*.md"))}
    for tag in inst.active_tags:
        for name in tag.commands:
            source = AGENTS_COMMANDS_DIR / f"{name}.md"
            claimed = sources.setdefault(source.name, source)
            if claimed != source:
                raise TagError(
                    f"command name collision: {tag.label} grants {source}, but "
                    f"{claimed} already installs as '{source.name}' — rename one")
    for source in sources.values():
        copy_file(source, destination / source.name)


def install_settings(inst: Instance, registry: Registry) -> None:
    """Merge the shared base settings (settings/settings.json) with the
    instance's policy fragments into `<state>/settings.json`, refreshed each
    launch. docker_config.set_container_mounts RO-mounts the result over
    `~/.claude/settings.json` in-container, so the agent reads its policies
    but can't relax them (the mount shadows the state-dir's rw view of the
    same path). Policy-vs-policy or policy-vs-base scalar conflicts abort
    the launch via merge_fragments' TagError, naming both culprits.

    ALWAYS-ON (static) policies — `always_on = true` in their tag.info, e.g.
    `<-su>` — merge into EVERY instance, straight from the registry: they're
    never listed on the instance itself. Then the instance's selected
    policies, then specialties that claim a hidden `policy/_<name>` fragment
    (e.g. `{ro}`) — same merge, so any conflict is caught the same way."""
    fragments = [(BASE_SETTINGS_FILE.name + " (base)", json.loads(read_text(BASE_SETTINGS_FILE)))]
    fragments += [(p.name, p.load_fragment())
                  for p in sorted(registry.policies.values(), key=lambda p: p.name) if p.always_on]
    fragments += [(p.name, p.load_fragment()) for p in inst.policies]
    fragments += [(s.name, s.load_fragment()) for s in inst.specialties if s.policy_dir]
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


def compute_resume_flag(inst: Instance) -> list[str]:
    """The claude args to resume an existing conversation (`["--continue"]`) or
    `[]` for a fresh session — shared by run.py's launch and quickie's
    `--resume`. A continuing instance with no transcript prints a notice and
    starts fresh, because `--continue` against history-only state crashes
    claude with 'No conversation found'."""
    if inst.is_brand_new:
        return []
    if inst.has_continuable_history:
        return ["--continue"]
    print(f"  (Instance '{inst.instance}' has no prior conversation; starting fresh.)")
    return []


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
    objects. None when the agent's `.md` is gone (orphan state dir).

    A store entry naming a tag that no longer resolves (a typo, or a tag
    renamed/removed since the instance was set up) does NOT crash: the bad
    names are collected on `Instance.invalid_tags` (the picker flags them and
    refuses to start the instance; `invalid_tags_report` explains the fix).
    Only the resolvable tags become objects — so F2-modify pre-checks the
    valid ones and drops the rest."""
    agent_name, _, session = instance_id.partition(SESSION_SEP)
    agent = load_agent(agent_name, AGENTS_DIR)
    if agent is None:
        return None
    entry = store.load().get(instance_id)
    build = store.entry_to_build(entry) if entry else agent.build
    clean_build, problems = registry.resolve_store_build(build)
    return Instance(
        agent=agent_name,
        md_path=agent.md_path,
        session=session,
        workspace=entry.get("workspace") if entry else None,
        is_brand_new=False,
        invalid_tags=tuple(problems),
        **resolve_build(clean_build, agent_name, registry),
    )


def invalid_tags_report(inst: Instance) -> str:
    """The multi-line, user-facing explanation for a blocked instance whose
    store entry names tags that no longer resolve. Lists, per bad tag, why it
    failed and the valid names of that kind to choose from, then how to fix
    it (edit the store file, or F2 in the picker). Callers print it and
    refuse to start the instance."""
    n = len(inst.invalid_tags)
    lines = [
        f"  Instance '{inst.instance}' can't start — its saved tags include "
        f"{n} name{plural(n)} that no longer match a known tag:",
        "",
    ]
    for p in inst.invalid_tags:
        if p.reason == "wrong_axis":
            why = f"is a {p.actual_kind} tag, so it can't sit under {p.axis}"
        else:
            why = "isn't a known tag — a typo, or the toolset changed since this instance was set up"
        options = ", ".join(p.options) or "(none defined)"
        lines.append(f"    {p.label}  (listed under {p.axis}) {why}.")
        lines.append(f"        replace it with one of these {p.kind} tags: {options}")
        lines.append("")
    lines.append(
        f"  Edit {home_relative(INSTANCES_FILE)} to swap each bad name for a valid one "
        "(or remove it), then relaunch —"
    )
    lines.append("  or open the picker and press F2 on this instance to re-pick its tags.")
    return "\n".join(lines)


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
