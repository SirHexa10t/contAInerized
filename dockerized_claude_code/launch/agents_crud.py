"""Agent state CRUD, tags edition: every operation that mutates the launcher's
persistent per-instance state, plus the factories that turn on-disk state into
the identity shapes the picker and run.py consume.

Sections:
  - list_all_instances — scan ~/.ai-agents/instances/ for `<agent>__<session>` dirs
  - persist_instance / delete_instance / modify_instance — instances.toml
    writers (load → mutate → save over tags.store) + state-dir lifecycle
  - install_latest_md — source `.md` + chain-keyed addendum section →
    state-dir CLAUDE.md in one overwrite (tags.addendums supplies the text)
  - install_settings — the instance's policies, rendered through its
    harness, → state-dir settings (and the harness's own rules file, if it
    keeps one); fixed_policy — the always-on denies for the image's fixed tier
  - compute_resume_flag — Instance → resume args (["--continue"] | [])
  - resolve_pick — name string → Agent (create) | Instance (cont) factory
    used by run.py's CLI parsing
  - creatable_agents / instance_from_store — picker-entry factories
  - _agent_sort_key — Create-row ordering (profession group, then the
    engine's capability standard via tags.engine.effort_tier_rank, then name)

Identity types (Agent / Instance) and the store primitives live in the tags
package; this module wires them to the filesystem lifecycle. menu_picker and
run.py import from here; nothing here imports them back.
"""

import json
import tomllib
from pathlib import Path
from typing import Any, NamedTuple

from .ai import Adapter, active_adapter
from .file_access import (
    copy_file, ensure_dir, force_remove, home_relative, is_dir, iter_subdirs,
    move_path, path_exists, read_text, write_text,
)
from .paths import (
    AGENTS_COMMANDS_DIR, AGENTS_DIR, INSTANCES_FILE, SHARED_COMMANDS_DIR,
    base_settings_file, instance_state_dir_path, instances_dir,
    state_commands_dir, state_settings_path,
)
from .tags import (
    Agent, Harness, Instance, Registry, Rules, TagError, addendums, load_agent,
    resolve_build, store,
)
from .tags.engine import effort_tier_rank
from .tags.identity import SESSION_SEP
from .tags.policy import merge_fragments
from .tags.policy_mapping import FIXED_FILE_HEADER, SETTINGS_FORMAT, PolicyRendering, rules_file_text
from .utils import ordering_index_or_end, plural, prompt_keypress


def list_all_instances() -> list[str]:
    """Every `{agent}__{session}` dir under ~/.ai-agents/instances/
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


def install_settings(inst: Instance, registry: Registry, harness: Adapter) -> tuple[str, ...]:
    """Render the instance's policies through `harness` (the CLI it runs in)
    and install the result in its state dir, refreshed each launch. Returns
    the rendering's notes for the operator, for the caller to print — this
    function never prints.

    Which rules: the ALWAYS-ON (static) policies — `always_on = true` in
    their tag.info, e.g. `<-su>` — straight from the registry, since they're
    never listed on the instance itself; then the instance's selected
    policies; then specialties that claim a hidden `policy/_<name>` fragment
    (e.g. `{ro}`). Each renders through the harness's policy.mapping
    (`Harness.render_policy`), and a word the harness has no row for:
      - in [deny] or [demand], refuses the launch with a TagError naming the
        tag and the word — a deny that silently evaporated would leave the
        agent less fenced than its build says;
      - in [allow], becomes a note: the agent is asked where it would have
        been spared, which is safe.
    A claimed fragment's RAW settings (`<harness>.json`, e.g. `_cowork`'s
    Stop hook) merge on their own harness only; on any other, the specialty
    refuses here, since the part of it no word can say would be missing.

    Where it lands — the harnesses FORK here:
      - `<state>/settings.json`: the harness's base (`paths.base_settings_file`),
        then every rendering's settings fragment and raw fragment, in the
        order above, deep-merged; a scalar conflict aborts the launch via
        merge_fragments' TagError, naming both culprits. Staging RO-mounts it
        over the CLI's own settings file, so the agent reads its policies but
        can't relax them (the mount shadows the state dir's rw view of the
        same path). Claude Code's rules live here, as permission lists.
      - the harness's rules file, when it keeps one (`Harness.policy_file` —
        Gemini CLI's Policy Engine): every rendering's records, written to
        `<state>/<policy_file>` and read back at once, because the CLI loads
        nothing, and says nothing, for a missing or unreadable file; staging
        RO-mounts it and the launch names it in the harness's policy args."""
    owner = registry.harnesses.get(harness.key)
    if owner is None:
        raise TagError(f"no agents/harness/{harness.key} member for the {harness.name} adapter")
    base = base_settings_file(harness)
    fragments = [(f"{base.name} (base)", json.loads(read_text(base)))] if base is not None else []
    sections: list[tuple[str, tuple[dict, ...]]] = []
    notes: list[str] = []

    def add(label: str, name: str, rules: Rules) -> None:
        rendering = owner.render_policy(name, rules)
        notes.extend(_unmapped_notes(label, owner, rendering))
        if rendering.settings:
            fragments.append((name, rendering.settings))
        sections.append((name, rendering.rules))

    for policy in [*sorted((p for p in registry.policies.values() if p.always_on), key=lambda p: p.name),
                   *inst.policies]:
        add(policy.label, policy.name, policy.rules)
    for specialty in inst.specialties:
        if (fragment := specialty.fragment) is None:
            continue
        if fragment.rules is not None:
            add(specialty.label, specialty.name, fragment.rules)
        if fragment.raw_harnesses:
            raw = fragment.raw_fragment(harness.key)
            if raw is None:
                raise TagError(f"{specialty.label} cannot run on {harness.name}: part of it is settings only "
                               f"{', '.join(fragment.raw_harnesses)} can read ({fragment.path}/"
                               f"<harness>.json), which no policy word can say yet")
            fragments.append((specialty.name, raw))
    merged = merge_fragments(fragments)
    write_text(state_settings_path(inst.state_dir), json.dumps(merged, indent=2, sort_keys=True) + "\n")
    if owner.policy_file is not None:
        _install_rules_file(inst.state_dir / owner.policy_file, sections)
    return tuple(notes)


class FixedPolicy(NamedTuple):
    """What the image bakes at a CLI's fixed tier: where, the file, and how
    many rules the launcher rendered into it — the count the Dockerfile
    checks the parsed file against, since "non-empty and parses" also passes
    a file holding only comments (gate fixed-tier)."""
    path: str
    text: str
    rules: int


def fixed_policy(harness: Harness, registry: Registry) -> FixedPolicy | None:
    """The ALWAYS-ON policies as `harness`'s fixed tier holds them, for the
    image to bake in as root at the mapping's `[fixed] path` (the harness
    Dockerfile, fed by `container_env.set_container_env`) — or None for a
    harness without a fixed tier.

    The same renderings `install_settings` makes per instance, and those
    stay: the fixed copy is additive, so a CLI that ever skips it still
    enforces every always-on rule. Only denies can arrive here, since
    `Policy.scan` holds always-on policies to stance deny. Serialised in the
    format's own shape — a settings JSON for Claude Code's managed settings,
    a rules file for Gemini CLI's system policies dir — and a deny the
    harness cannot express refuses, as it does per instance. With no
    always-on policy at all the file is still produced, empty but valid, so
    the build's check holds for every tree: it expects the count, 0
    included."""
    mapping = harness.policy_mapping
    if mapping is None or mapping.fixed_path is None:
        return None
    rendered = [(policy, harness.render_policy(policy.name, policy.rules))
                for policy in sorted(registry.policies.values(), key=lambda p: p.name) if policy.always_on]
    for policy, rendering in rendered:
        _unmapped_notes(policy.label, harness, rendering)   # refuses an unmapped deny
    if mapping.format == SETTINGS_FORMAT:
        merged = merge_fragments([(policy.name, rendering.settings) for policy, rendering in rendered])
        return FixedPolicy(mapping.fixed_path, json.dumps(merged, indent=2, sort_keys=True) + "\n",
                           _list_entries(merged))
    records = [(policy.name, rendering.rules) for policy, rendering in rendered]
    return FixedPolicy(mapping.fixed_path, rules_file_text(records, header=FIXED_FILE_HEADER),
                       sum(len(rules) for _, rules in records))


def _list_entries(settings: Any) -> int:
    """Every list entry in a settings document, at any depth: a settings-
    format rule is a string in a list (after merge_fragments' dedupe), and
    the Dockerfile counts the baked JSON the same way."""
    if isinstance(settings, dict):
        return sum(_list_entries(value) for value in settings.values())
    return len(settings) if isinstance(settings, list) else 0


def _unmapped_notes(label: str, harness: Harness, rendering: PolicyRendering) -> list[str]:
    """The words `harness` could not render: a TagError for the first deny or
    demand among them, else one note per allow."""
    for word in rendering.unmapped:
        if word.stance != "allow":
            where = (f"its policy.mapping has no row for {word}" if harness.policy_mapping
                     else "it has no policy.mapping yet")
            raise TagError(f"{label} cannot be enforced by {harness.fullname}: {where} — drop {label} "
                           f"from this build, or map the word in agents/harness/{harness.name}/policy.mapping")
    return [f"{label}: {harness.fullname} has no row for {word}, so the agent is asked "
            f"before it rather than spared" for word in rendering.unmapped]


def _install_rules_file(path: Path, sections: list[tuple[str, tuple[dict, ...]]]) -> None:
    """Write a harness's rules file into the state dir and read it back.

    `write_text` creates the file's directory host-side first, and that is
    load-bearing twice over: staging mounts the FILE read-only, and a mount
    whose parent is missing makes docker create that parent root-owned —
    where the CLI must be able to create files of its own (Gemini writes its
    auto-saved policies there, through a temp file and a rename; it would
    make the dir itself, but docker gets there first)."""
    expected = sum(len(records) for _, records in sections)
    write_text(path, rules_file_text(sections))
    try:
        loaded = tomllib.loads(read_text(path))
    except tomllib.TOMLDecodeError as e:
        raise TagError(f"{path}: the rules file the launcher just wrote does not parse ({e})") from e
    if len(loaded.get("rule", [])) != expected:
        raise TagError(f"{path}: wrote {expected} rules but reads back {len(loaded.get('rule', []))}")


def install_latest_md(inst: Instance) -> None:
    """Write the agent's source `.md` plus the active-tag addendum section
    into the state dir as CLAUDE.md, in a single overwrite. Refreshed each
    launch so a source-side edit AND any tag toggle both propagate. The
    result is launcher-owned: whatever a previous launch wrote is replaced
    wholesale, no marker-based reconciliation."""
    body = read_text(inst.md_path)
    addendum = addendums.compose(inst.active_tags)
    write_text(inst.state_md, f"{body}\n\n{addendum}" if addendum else body)


# Where "big transcript" starts to bite: `claude --continue` was observed
# silently starting a FRESH conversation over a ~92 MB transcript (resumes of
# the same file at ~75-85 MB still worked), and upstream reports hangs from
# ~50 MB — plans/ISSUES.md, 2026-08-29. The launch still resumes as asked;
# the warning turns a silent surprise into a stated risk.
RESUME_SIZE_WARN_BYTES = 50 * 1024 * 1024


def compute_resume_flag(inst: Instance) -> list[str]:
    """The args to resume an existing conversation (the adapter's
    `continue_args` — `["--continue"]` for Claude Code) or `[]` for a fresh
    session — shared by run.py's launch and quickie's
    `--resume`. A continuing instance with no transcript prints a notice and
    starts fresh, because `--continue` against history-only state crashes
    claude with 'No conversation found'. A huge transcript still resumes, but
    behind a warning — see RESUME_SIZE_WARN_BYTES."""
    if inst.is_brand_new:
        return []
    if inst.has_continuable_history:
        size = inst.continuable_history_bytes
        if size > RESUME_SIZE_WARN_BYTES:
            print(f"  WARNING: this instance's transcript is {size / 2**20:.0f} MB. "
                  f"claude has silently DROPPED the history of a ~92 MB one at "
                  f"launch (plans/ISSUES.md) — if this conversation matters, "
                  f"consider retiring it for a fresh session soon.")
        return list(active_adapter().continue_args)
    print(f"  (Instance '{inst.instance}' has no prior conversation; starting fresh.)")
    return []


def _agent_sort_key(agent: Agent, registry: Registry) -> tuple[tuple[int, ...], int, str]:
    """Create-row ordering: profession-less agents first (then by each
    profession's registry position), the engine's capability standard within a
    group (strongest first — the same rank whatever AI runs), name as the
    tiebreak."""
    prof_order = list(registry.professions)
    prof_key = tuple(sorted(ordering_index_or_end(p, prof_order) for p in agent.build.professions))
    engine = registry.engines.get(agent.build.engine or agent.name) or registry.engines.get("default")
    return (prof_key, -effort_tier_rank(engine), agent.name)


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
    # A solo instance's build lives in the `solo` scope: a tag that cannot
    # stand there ({clstr}, applied by cluster creation only) is flagged like
    # a stale name — blocked, red in the picker, F2 drops it.
    clean_build, problems = registry.resolve_store_build(build, scope="solo")
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
        elif p.reason == "forbidden":
            why = f"is a real tag a solo instance cannot carry — {p.hint}"
        else:
            why = "isn't a known tag — a typo, or the toolset changed since this instance was set up"
        lines.append(f"    {p.label}  (listed under {p.axis}) {why}.")
        if p.reason != "forbidden":   # a forbidden tag IS a valid name of its kind — offering the list would offer it back
            lines.append(f"        replace it with one of these {p.kind} tags: {', '.join(p.options) or '(none defined)'}")
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
