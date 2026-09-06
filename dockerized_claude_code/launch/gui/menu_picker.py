"""The launcher's MENUS (launch/gui): what rows the user is offered, what
each key does to them, and what a pick means.

Three of them, all built on `picker_widget.pick_with_preview`:

  select_agent(registry)
      The main menu: creatable agents, their continuable instances nested
      beneath, cluster templates and clusters, plus the "(Edit Preferences)"
      and "(Move onto deletions menu)" openers. Runs until the user picks
      something or cancels; handles the deletion submenu and the profile
      forms internally.
      -> Agent (new) | Instance (cont) | Cluster | None on cancel/empty

  prompt_stop(registry)
      `run.py --stop`'s multi-select over RUNNING containers.
      -> the picked docker ids

  _delete_submenu(registry, legend_text)
      The nested destructive menu, reachable only from select_agent.

What this module knows that the widget does not: agents, instances,
clusters, which rows may be selected, what a row's preview should say, and
the F8 composition legend. Row rendering, cursor movement and the selection
loop are `picker_widget`'s — this module was 1541 lines holding both until
they were split on 2026-09-03.

Row builders and state lookups come from `agents_crud`; the flows each key
triggers live in `picker_flows`; line prompts and inline dialogs in
`picker_prompts`; preview text in `picker_previews`. Every *form* lives in
`forms.py` / `cluster_form.py` — this module only opens them.

(The pre-launch banner lives in claude_code_config.print_launch_banner —
launch-stage output, not picker UI.)
"""

import dataclasses
import io
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from rich import box                                                       # dep — declared in pyproject.toml [project]
from rich.console import Console
from rich.markdown import Markdown
from rich.table import Table
from rich.text import Text

from ..agents_crud import (
    creatable_agents, delete_instance, instance_from_store, invalid_tags_report,
    list_all_instances, modify_instance,
)
from ..cluster import state as cluster_state
from ..cluster.legoset import (
    discover_templates, load_legoset,
    validate,
)
from ..cluster.member import ClusterError
from ..docker_config import (
    cluster_container_id, docker_running_instances_subprocess,
)
from ..file_access import (
    expand_user_path, is_dir, read_text,
    resolved_cwd, resolved_path,
)
from ..paths import (
    AGENTS_COMMANDS_DIR, AGENTS_DIR, DEFAULT_WORKSPACE, DEFAULTING_DIRS,
)
from .picker_previews import (
    _cluster_preview, _create_preview, _member_preview, _render_md, _template_preview,
)
from .picker_flows import (
    _create_cluster_flow, _destroy_cluster_flow, _edit_cluster_flow,
    _edit_member_flow, _remove_member_flow,
)
from .picker_widget import (
    ContEntry, PickerAction, PickerCwdHint, PickerEntry, PickerRowMarker,
    _cont_tags_column, _deferred_preview, _tags_column, pick_with_preview,
)
from .picker_prompts import (
    _agent_description, confirm_dialog,
    instance_fields, _report_to_picker,
)
from .form_core import FormOption, checkbox_form
from .forms import edit_profiles_menu, prompt_tags
from .styles import (
    RICH_BY_STYLE, STYLE_TAG_INVALID, tag_style,
)
from ..tags import Agent, AgentBuild, Instance, Registry, Tag, resolve_build
from ..tags.engine import engine_sort_key, sorted_engines
from ..utils import ordering_index_or_end, relative_time


# ============================================================
# UI strings
# ============================================================

# The confirm prompt's wording + accepted answers live in `picker_prompts`,
# which owns every line-prompt this module opens.


# ============================================================
# Layout
# ============================================================


# Style class names + their corresponding style strings live as the
# UiClass enum in styles (shared by every form and this picker).

# ============================================================
# Agent-picker UI strings
# ============================================================

TITLE_AGENT_PICKER = "Select an agent:"
TITLE_DELETE_MENU  = "‼️  DELETE AGENT INSTANCES  ‼️"

# Row marker glyphs + their styles live on the PickerRowMarker enum below.
# Cwd-relation labels ("(CURRENT DIR) " / "(DEFAULT DIR) ") live on
# PickerCwdHint there too.

PREFERENCES_LABEL  = "(Edit Preferences)"
DELMENU_LABEL  = "(Move onto deletions menu)"
BACK_LABEL     = "(Move back to Agent Selection)"
PREFERENCES_PREVIEW = ("One merged form, one section per profile file. Toolkits: which language "
                    "toolchains a configurable profession's shared image installs (today: [code]'s "
                    "Rust / Node / CMake) — edits ~/.claude-agents/<profession>_profile.toml; a "
                    "changed toggle rebuilds only that tool's Docker layer on the next launch. "
                    "UI configs: launcher preferences — the {mux} backend, herdr vs tmux — edits "
                    "~/.claude-agents/ui_profile.toml, read at every launch. Service CLIs "
                    "(gh, gcloud, aws, ...) are not chosen here — they install when matching creds "
                    "exist under user_extras/optional_creds/.")
DELMENU_PREVIEW = "Open the deletion sub-menu to remove agent instances and their state directories."
BACK_PREVIEW    = "Return to the main agent picker."
CONFIRM_DELETE_FMT = "Delete '{name}'?"

# ============================================================
# Agent-picker styles (inline, applied per-segment)
# ============================================================

STYLE_AGENT_NAME     = "bold fg:ansibrightblue"
STYLE_MEMBER_COUNT    = "fg:ansigreen"    # the "(N members)" column on a template row
STYLE_DEL_NAME       = "bold fg:ansired"
STYLE_WORKSPACE_HINT = "italic fg:ansibrightblack"

# A running instance's row is information-only (see PickerEntry.selectable):
# the name greys out to read as unavailable, and the red tag is what draws the
# eye. Emitted conditionally like the PickerCwdHint labels — no reserved
# column, so non-running rows keep their tighter spacing.
STYLE_RUNNING_NAME   = "fg:ansibrightblack"                      # grey — this instance can't be launched right now
RUNNING_HINT         = ("bold fg:ansibrightred", "(RUNNING) ")   # (style, label) fragment, same shape as PickerCwdHint.fragment








NO_WORKSPACE_DISPLAY = "?"            # subtitle placeholder when a Cont row's store entry is missing or stale






# Sentinel entry values signalling "open the delete submenu" / "open the
# toolkits editor" — used in the main picker where most rows hold an
# identity dataclass; these are the non-identity rows, so distinct
# singletons let the dispatcher match by `is` rather than tagging
# identities with extra metadata.
_OPEN_DELMENU = object()
_OPEN_PREFERENCES = object()


@dataclasses.dataclass(frozen=True)
class _ClusterTemplateRow:
    """A cluster-template row's value: which `.legoset` to open the membership
    form on. A dataclass rather than a singleton because there is one row PER
    template — the dispatcher matches by type, then reads the path."""
    name: str
    path: Path


@dataclasses.dataclass(frozen=True)
class _ClusterRow:
    """An EXISTING cluster's row value. Carries only the session name: every
    handler reloads the cluster from disk, so a row built before some other
    handler mutated the cluster cannot act on a stale member list."""
    session: str


@dataclasses.dataclass(frozen=True)
class _MemberRow:
    """One member's row value — the unit the picker EDITS (F2 re-tags, Del
    removes from the cluster). Session + member id; same reload-on-act rule
    as _ClusterRow."""
    session: str
    member_id: str


def continuable_instances(registry: Registry,
                          running: frozenset[str] | None = None) -> list[ContEntry]:
    """ContEntry list for the picker's Cont/DELETE rows. Orphans (missing .md)
    skipped — instance_from_store returns None for those. Sorted by active
    tag set (tag-less first, then registry order: specialties dominate,
    professions next), then engine capability, then agent/session. Marks
    instances whose workspace resolves to the current working directory (for
    the picker's CURRENT DIR hint). The contained Instance is what the picker
    hands back on selection — stored workspace + resolved tag objects baked
    in so the modify flow's pre-fill reads straight off the identity.
    A store entry naming an unknown tag fails fast here (validate_build
    raises) — `python -m launch.audit` reports the same defect non-fatally
    when the picker is the wrong place to crash on a typo.

    Also flags which instances are running right now (`is_running`). `running`
    is the docker_running_instances_subprocess snapshot to flag from — pass it
    when the caller already probed (select_agent shares ONE `docker ps` per
    menu build between these rows and the cluster rows); None probes here, so
    the marks still refresh per menu rebuild (including on return from the
    delete / toolkits submenus) without costing a subprocess per keystroke. An
    undeterminable docker state marks nothing, deliberately: over-flagging
    would wrongly lock rows the user can actually launch."""
    # Symlinks normalized via .resolve() so e.g. /home/<user> matches /var/users/<user>
    # when one symlinks to the other. Subdirs deliberately don't count — being in a
    # project under $HOME doesn't make /ai_workspace your "default" workspace.
    cwd = resolved_cwd()
    defaulting_dir_active = cwd in {resolved_path(d) for d in DEFAULTING_DIRS}
    default_workspace_resolved = resolved_path(DEFAULT_WORKSPACE)
    if running is None:
        running = docker_running_instances_subprocess() or frozenset()   # None (can't tell) → flag nothing

    out = []
    for dir_name in list_all_instances():
        inst = instance_from_store(dir_name, registry)
        if inst is None:
            continue
        ws = inst.workspace
        ws_resolved = resolved_path(ws) if ws and is_dir(ws) else None
        last_mtime = inst.last_used_mtime
        out.append(ContEntry(
            identity=inst,
            workspace_display=ws if ws else NO_WORKSPACE_DISPLAY,                                    # show stored value even when invalid; `?` sentinel only when no entry at all
            is_current_dir=ws_resolved == cwd,
            is_default_dir=defaulting_dir_active and ws_resolved == default_workspace_resolved,      # cwd ∈ DEFAULTING_DIRS and ws matches DEFAULT_WORKSPACE — tagged `(DEFAULT DIR)`
            is_invalid_dir=bool(ws) and ws_resolved is None,                                         # ws set but path doesn't exist / isn't a directory — tagged `(INVALID DIR)`
            last_used_display=relative_time(last_mtime) if last_mtime is not None else "(never)",
            is_running=dir_name in running,
        ))

    spec_order, prof_order = list(registry.specialties), list(registry.professions)

    def cont_sort_key(e: ContEntry) -> tuple[Any, ...]:
        i = e.identity
        return (
            tuple(sorted(ordering_index_or_end(s.name, spec_order) for s in i.specialties)),
            tuple(sorted(ordering_index_or_end(p.name, prof_order) for p in i.professions)),
            engine_sort_key(i.conf.get("ANTHROPIC_MODEL", "")),
            i.agent,
            i.session,
        )

    out.sort(key=cont_sort_key)
    return out






def _tag_commands(registry: Registry) -> list[tuple[Tag, str, str]]:
    """`(tag, /command, description)` for every slash command a TAG grants.

    Read from the declarations rather than any directory: a tag names its
    commands in tag.info (`commands = [...]`) and the files live centrally in
    `agents/_commands/` — one findable place, and one file grantable by several
    tags. The registry has already validated that every declared name resolves
    to a real file, so the read here cannot miss. Any KIND may declare —
    `{manager}` and `[self]` both do — which is why this iterates all four.

    The description comes from the command file's own `description:` frontmatter —
    the same line Claude Code shows in its `/help` — so the legend cannot drift
    from what the command says about itself. A file without one is listed anyway
    with an empty description; silently hiding it would be worse than a blank cell.
    """
    return [(tag, f"/{name}",
             _frontmatter_description(AGENTS_COMMANDS_DIR / f"{name}.md"))
            for tag in registry.get_all() for name in tag.commands]


def _frontmatter_description(md: Path) -> str:
    """The `description:` line from a command file's YAML frontmatter, or "".

    Deliberately a line scan rather than a YAML parse: the frontmatter here is
    two or three flat keys, and a dependency (or a hand-rolled parser) for one
    field would be more to maintain than to read."""
    for line in read_text(md).splitlines()[:12]:
        if line.startswith("description:"):
            return line.split(":", 1)[1].strip()
    return ""


def _build_composition_legend(registry: Registry) -> str:
    """Build the F8 'composition legend' shown over the preview pane — one
    table per tag kind, header = the kind's nutshell, rows = each discovered
    member's underlined fullname + short description (the fullname spells
    out what the shortname abbreviates). Built with rich Table objects and
    rich styles — injecting raw ANSI into markdown-table source made rich
    count escape bytes as width and misalign the columns."""
    buf = io.StringIO()
    console = Console(file=buf, force_terminal=True, color_system="truecolor", width=80)
    sections: list[tuple[str, str, str, Iterable[Tag]]] = [
        ("Engines",     "Engine",     "How hard the agent thinks — a model/effort budget (most advanced first).", sorted_engines(registry.engines.values())),
        ("Professions", "Profession", "Tools it can use — each is a docker image layer.", registry.professions.values()),
        ("Specialties", "Specialty",  "Exceptional access or running conditions.", registry.specialties.values()),
        # Policies sort by shortname WITH its symbol (`!` < `+` < `-`), so
        # same-stance policies group: demands, grants, denials.
        ("Policies",    "Policy",     "What it's permitted to do — orange grants, blue denies, white demands.",
         sorted(registry.policies.values(), key=lambda t: t.shortname)),
    ]
    for title, singular, nutshell, members in sections:
        console.print(Markdown(f"# {title}\n\n{nutshell}"))
        console.print()
        table = Table(box=box.SIMPLE_HEAD, header_style="cyan", pad_edge=False)
        table.add_column(singular)
        table.add_column("Description")
        for t in members:
            table.add_row(
                Text(t.label, style=RICH_BY_STYLE[tag_style(t)]),
                Text.assemble((t.fullname, "underline"), f": {t.short_description}"),
            )
        console.print(table)

    # Commands a TAG grants, if any. Omitted entirely when none do, rather than
    # printing an empty table that implies the feature is broken. "Tag", not
    # "Specialty": any kind may declare (`[self]` is a profession).
    if commands := _tag_commands(registry):
        console.print(Markdown(
            "# Tag Commands\n\n"
            "Slash commands that arrive WITH a tag — present only in instances "
            "carrying a granting tag, unlike the shared commands every agent gets."))
        console.print()
        table = Table(box=box.SIMPLE_HEAD, header_style="cyan", pad_edge=False)
        table.add_column("Tag")
        table.add_column("Command")
        table.add_column("Description")
        for tag, command, description in commands:
            table.add_row(Text(tag.label, style=RICH_BY_STYLE[tag_style(tag)]),
                          Text(command, style="bold"),
                          Text(description))
        console.print(table)
    return buf.getvalue()
















STOP_FORM_TITLE = "Stop running containers  (Space to mark, Enter to stop):"


def prompt_stop(registry: Registry) -> list[str]:
    """The `--stop` selector: every RUNNING instance and cluster as a checkbox
    row wearing the picker's own Cont-row anatomy — tags · instance name ·
    cwd hint · workspace — minus the `(RUNNING)` hint, which would say nothing
    in a list that is running by definition. `{muxer}` is emphasized wherever
    present: sticky sessions are this flag's reason to exist (a muxer
    container outlives its terminal, so this list is how one is ended without
    re-attaching). Returns the picked docker ids, CONTAINER_NAME_PREFIX
    already stripped (the running-snapshot's spelling) — empty on Esc or when
    nothing runs.

    A running id that matches no store entry and no cluster still gets a bare
    row (id only): a stray is exactly what someone reaching for --stop most
    needs to be able to stop."""
    running = docker_running_instances_subprocess() or frozenset()
    options: list[FormOption] = []
    matched: set[str] = set()

    live = [e for e in continuable_instances(registry, running) if e.is_running]
    columns = {e.identity.instance:
               _cont_tags_column(e.identity, emphasize=frozenset({"muxer"}))
               for e in live}
    col_width = max((w for _, w in columns.values()), default=0)
    name_width = max((len(e.identity.instance) for e in live), default=0)
    for entry in live:
        matched.add(entry.identity.instance)
        fragments, width = columns[entry.identity.instance]
        display = [*fragments, ("", " " * (col_width - width)),
                   (STYLE_AGENT_NAME,
                    f"{entry.identity.instance:<{name_width}}"),
                   ("", "    ")]
        if entry.is_current_dir:
            display.append(PickerCwdHint.CURRENT.fragment)
        elif entry.is_default_dir:
            display.append(PickerCwdHint.DEFAULT.fragment)
        elif entry.is_invalid_dir:
            display.append(PickerCwdHint.INVALID.fragment)
        display.append((STYLE_WORKSPACE_HINT, entry.workspace_display))
        options.append(FormOption(
            key=entry.identity.instance, label=display,
            body=[("", f"last used {entry.last_used_display}   ·   stopping "
                       "ends the container; the conversation resumes on the "
                       "next launch")]))

    for cluster in cluster_state.discover():
        container_id = cluster_container_id(cluster.session)
        if container_id not in running:
            continue
        matched.add(container_id)
        options.append(FormOption(
            key=container_id,
            label=[(STYLE_MEMBER_COUNT, f"({len(cluster.members)} members)"),
                   ("", "  "),
                   (STYLE_AGENT_NAME, cluster.session), ("", "    "),
                   (STYLE_WORKSPACE_HINT, str(cluster.project))],
            body=[("", "members: " + ", ".join(cluster.ids))]))

    for stray in sorted(running - matched):
        options.append(FormOption(
            key=stray, label=[(STYLE_AGENT_NAME, stray)],
            body=[("", "a running launcher container with no store entry — "
                       "stoppable, not otherwise known here")]))

    if not options:
        print("  Nothing is running.")
        return []
    result = checkbox_form(STOP_FORM_TITLE, options)
    # No text fields ride this form, so a non-None return IS the checked-key
    # list — the tuple variant exists only for field-carrying forms. The
    # isinstance narrows for mypy rather than assumes.
    return result if isinstance(result, list) else []


def select_agent(registry: Registry) -> "Agent | Instance | cluster_state.Cluster | None":
    """Run the agent picker (main + nested deletion submenu) until selection or cancel.
    Returns an Agent (create), an Instance (continue), a Cluster (launch it),
    or None (cancel). Caller must ensure at least one agent .md exists before
    invoking."""
    legend_text = _build_composition_legend(registry)   # built once per call — the loop below only re-scans instances
    while True:
        agents = creatable_agents(registry)
        # ONE `docker ps` per menu build, shared by the instance rows and the
        # cluster rows below — a running row of either kind greys out.
        running = docker_running_instances_subprocess() or frozenset()
        instances = continuable_instances(registry, running)

        instances_by_agent: dict[str, list[ContEntry]] = {}
        for inst in instances:
            instances_by_agent.setdefault(inst.identity.agent, []).append(inst)

        agent_name_width = max(len(a.name) for a in agents)
        instance_name_width = max((len(i.identity.instance) for i in instances), default=0)

        # Tag column (Create rows) and tag column (Cont rows) are sized
        # INDEPENDENTLY — each scoped to its own population so a row's
        # agent / instance name sits tight against its tags. Tying the
        # two together (a shared max) pushed Create-row agent names way out
        # to align with the widest cont-row tag set, even though the columns
        # don't share a row.
        #
        # Create rows show the `.lego` default professions/specialties (the
        # names resolve through the registry for warn-aware coloring); Cont
        # rows show the instance's actual resolved tag objects.
        def build_tags(build: AgentBuild) -> list[Tag]:
            names = (*build.professions, *build.specialties, *build.policies)
            return [t for n in names if (t := registry.get(n)) is not None]

        tag_by_agent = {a.name: _tags_column(build_tags(a.build)) for a in agents}
        tag_by_inst = {i.identity.instance: _cont_tags_column(i.identity) for i in instances}
        tag_col_width  = max((w for _, w in tag_by_agent.values()), default=0)
        cont_col_width = max((w for _, w in tag_by_inst.values()), default=0)

        entries: list[PickerEntry] = []
        for agent in agents:
            tag_frags, tag_len = tag_by_agent[agent.name]
            entries.append(PickerEntry(
                display=[
                    *PickerRowMarker.NEW.fragments("  "),
                    *tag_frags,
                    ("", " " * (tag_col_width - tag_len)),
                    (STYLE_AGENT_NAME, f"{agent.name:<{agent_name_width}}"),
                    ("", f" — {_agent_description(read_text(agent.md_path))}"),
                ],
                preview=_create_preview(agent),
                value=agent,
                deletable=False,
                modifiable=False,
            ))
            for inst in instances_by_agent.get(agent.name, []):
                identity = inst.identity
                cont_frags, cont_len = tag_by_inst[identity.instance]
                cont_display = [
                    *PickerRowMarker.CONT.fragments("      "),
                    *cont_frags,
                    ("", " " * (cont_col_width - cont_len)),
                    (STYLE_RUNNING_NAME if inst.is_running else STYLE_AGENT_NAME,
                     f"{identity.instance:<{instance_name_width}}"),
                    ("", "    "),
                ]
                if inst.is_running:
                    cont_display.append(RUNNING_HINT)
                if inst.is_current_dir:
                    cont_display.append(PickerCwdHint.CURRENT.fragment)
                elif inst.is_default_dir:
                    cont_display.append(PickerCwdHint.DEFAULT.fragment)
                elif inst.is_invalid_dir:
                    cont_display.append(PickerCwdHint.INVALID.fragment)
                cont_display.append((STYLE_WORKSPACE_HINT, inst.workspace_display))
                entries.append(PickerEntry(
                    display=cont_display,
                    preview=_deferred_preview(inst),   # reads transcripts on first highlight, not at menu open
                    preview_quick=_deferred_preview(inst, quick=True),
                    value=identity,
                    # A live container owns the name (Enter would hit a docker
                    # name conflict) and owns the state dir rw (Del would delete
                    # under it), so the row is information-only. selectable=False
                    # is what actually blocks all three keys; the other two are
                    # explicit belt-and-braces.
                    selectable=not inst.is_running,
                    deletable=not inst.is_running,
                    modifiable=not inst.is_running,
                ))

        # One row per `.legoset` — a cluster is CREATED from here (the
        # membership form). A template that fails to parse or names unknown
        # agents renders as an unselectable red row instead of crashing the
        # picker: templates are hand-authored files, and the picker is where
        # the author is.
        known_agents = frozenset(a.name for a in agents)
        for template_name, template_path in discover_templates(AGENTS_DIR).items():
            try:
                template = load_legoset(template_path)
                validate(template, known_agents)
            except ClusterError as error:
                entries.append(PickerEntry(
                    display=[*PickerRowMarker.CLUSTER.fragments("  "),
                             (STYLE_TAG_INVALID, template_name),
                             ("", f" — broken template: {error}")],
                    preview=_render_md(f"*`agents/{template_path.name}` failed to "
                                       f"load:*\n\n```\n{error}\n```"),
                    value=None, selectable=False, deletable=False, modifiable=False,
                ))
                continue
            # Same anatomy as an agent row — lead, a metadata column, the NAME
            # in the agents' name column, then " — description". The metadata
            # column holds the member count where agent rows hold tags, padded
            # so the name lands exactly where agent names do (the cluster tab
            # is wider, so the pad is measured, not copied).
            count = f"({len(template.members)} members)"
            name_column = PickerRowMarker.NEW.width("  ") + tag_col_width
            pad = max(name_column - PickerRowMarker.CLUSTER.width("  ")
                      - len(count), 1)
            entries.append(PickerEntry(
                display=[
                    *PickerRowMarker.CLUSTER.fragments("  "),
                    (STYLE_MEMBER_COUNT, count),
                    ("", " " * pad),
                    (STYLE_AGENT_NAME, f"{template_name:<{agent_name_width}}"),
                    ("", f" — {template.description or ', '.join(m.id for m in template.members)}"),
                ],
                preview=_template_preview(template, template_path),
                value=_ClusterTemplateRow(template_name, template_path),
                deletable=False,
                modifiable=False,
            ))

        # Existing clusters nest under the template rows, their members one
        # level deeper — the same parent/child shape agents and instances use.
        # Enter on the cluster row launches it, Del destroys it; a member row
        # is the editing unit: F2 re-tags, Del removes.
        for cluster in cluster_state.discover():
            cluster_running = cluster_container_id(cluster.session) in running
            # The CLUSTER's own tags live here now — every member carries
            # them, so showing them once beside the cluster says it without
            # repeating {mux}{clstr} down every member row (2026-09-02).
            shared_frags, _ = _tags_column(
                build_tags(cluster.tags),
                emphasize=frozenset({"cluster-cowork"}))
            cluster_display = [
                *PickerRowMarker.CLSTR.fragments("  "),
                (STYLE_RUNNING_NAME if cluster_running else STYLE_AGENT_NAME,
                 cluster.session),
                ("", "  "), *shared_frags,
                ("", f" ({len(cluster.members)} members)    "),
            ]
            if cluster_running:
                cluster_display.append(RUNNING_HINT)
            cluster_display.append((STYLE_WORKSPACE_HINT, str(cluster.project)))
            entries.append(PickerEntry(
                display=cluster_display,
                preview=_cluster_preview(registry, cluster),
                value=_ClusterRow(cluster.session),
                # The same information-only rule running instances get: the
                # live container owns the name (Enter → docker name conflict)
                # and has the state dir mounted at /cluster (F2's rename would
                # move it out from under the container; Del would delete it).
                selectable=not cluster_running,
                deletable=not cluster_running,
                modifiable=not cluster_running,
            ))
            # Rows in picker order — the same derived order the windows will
            # launch in, so the list here IS the `^b 1..9` numbering.
            for member in cluster_state.picker_order(cluster.members, registry):
                member_tags, _ = _tags_column(build_tags(member.build))
                entries.append(PickerEntry(
                    display=[
                        *PickerRowMarker.MEMBER.fragments(""),
                        (STYLE_RUNNING_NAME if cluster_running else STYLE_AGENT_NAME,
                         member.id),
                        ("", "  "),
                        *member_tags,
                    ],
                    preview=_member_preview(registry, cluster, member),
                    value=_MemberRow(cluster.session, member.id),
                    # Members follow their cluster: F2/Del write cluster.toml
                    # inside the very dir the live container has mounted.
                    selectable=not cluster_running,
                    deletable=not cluster_running,
                    modifiable=not cluster_running,
                ))

        # Unconditional, unlike the toolkit-only era: the form's UI section
        # (the {mux} backend pick) is profession-independent, so the row must
        # exist even when no configurable profession does.
        entries.append(PickerEntry(
            display=[
                *PickerRowMarker.TOOLS.fragments("  "),
                ("", PREFERENCES_LABEL),
            ],
            preview=PREFERENCES_PREVIEW,
            value=_OPEN_PREFERENCES,
            deletable=False,
            modifiable=False,
        ))

        entries.append(PickerEntry(
            display=[
                *PickerRowMarker.DELMNU.fragments("  "),
                ("", DELMENU_LABEL),
            ],
            preview=DELMENU_PREVIEW,
            value=_OPEN_DELMENU,
            deletable=False,
            modifiable=False,
        ))

        action, value = pick_with_preview(TITLE_AGENT_PICKER, entries, allow_delete=True, allow_modify=True, legend_text=legend_text)
        if action is None:
            return None

        if action == PickerAction.DELETE:  # picker enforces deletability — cont, cluster, and member rows reach here
            if isinstance(value, _ClusterRow):
                _destroy_cluster_flow(value.session)
            elif isinstance(value, _MemberRow):
                _remove_member_flow(value.session, value.member_id)
            elif confirm_dialog(CONFIRM_DELETE_FMT.format(name=value.instance)):
                delete_instance(value)
            continue

        if action == PickerAction.MODIFY and isinstance(value, _ClusterRow):
            _edit_cluster_flow(registry, value.session)
            continue

        if action == PickerAction.MODIFY and isinstance(value, _MemberRow):
            _edit_member_flow(registry, value.session, value.member_id)
            continue

        if action == PickerAction.MODIFY:  # instance cont rows — the only other modifiable kind
            old_inst = value
            # ONE form: workspace + name as text fields above the tags — the
            # same no-terminal-prompt shape cluster editing has.
            result = prompt_tags(
                registry, old_inst.build, instance=old_inst.agent,
                fields=instance_fields(old_inst.agent,
                                       workspace=old_inst.workspace,
                                       suffix=old_inst.session,
                                       current=old_inst.session))
            if result is None:   # Esc — abort the modify, back to the picker
                continue
            values, new_build = result
            new_inst = dataclasses.replace(
                old_inst, session=values["session"],
                workspace=expand_user_path(values["workspace"]),
                invalid_tags=(),   # re-picking against the live registry clears any stale/typo'd tags
                **resolve_build(new_build, old_inst.agent, registry),
            )  # is_brand_new stays False via the dataclass replace
            modify_instance(old_inst, new_inst)
            continue

        if value is _OPEN_PREFERENCES:
            edit_profiles_menu(registry)
            continue

        if value is _OPEN_DELMENU:
            _delete_submenu(registry, legend_text)
            continue

        if isinstance(value, _ClusterTemplateRow):
            _create_cluster_flow(registry, value.path)
            continue   # created (or cancelled) — back to the picker either way

        if isinstance(value, _ClusterRow):
            # Enter on a cluster LAUNCHES it — run.py owns docker, so hand the
            # loaded cluster back the same way an Agent/Instance is handed.
            # Reloaded from disk (the row may predate an edit); vanished means
            # someone destroyed it underneath — explain, don't crash.
            if (picked_cluster := cluster_state.load(value.session)) is not None:
                return picked_cluster
            _report_to_picker(
                f"  Cluster '{value.session}' is gone from disk.")
            continue

        if isinstance(value, _MemberRow):
            # A member alone is not launchable — say what the row is FOR
            # instead of silently ignoring the key.
            _report_to_picker(
                "  A member launches with its cluster — Enter on the"
                "\n  cluster row above. Here: F2 edits this member's tags,"
                "\n  Del removes it from the cluster.")
            continue

        if isinstance(value, Instance) and not value.is_startable:
            # A Cont row whose stored tags no longer resolve — explain and
            # bounce back to the picker (F2 re-picks; Del removes it) rather
            # than starting a half-resolved instance.
            _report_to_picker(invalid_tags_report(value) + "\n")
            continue

        return value  # Agent (new) | Instance (cont)


def _delete_submenu(registry: Registry, legend_text: str) -> None:
    """Flat deletion submenu — every row red. Loops until Esc / Back."""
    while True:
        instances = continuable_instances(registry)
        if not instances:
            return
        entries: list[PickerEntry] = []
        for inst in instances:
            identity = inst.identity
            row = [*PickerRowMarker.DLET.fragments("  "),
                   (STYLE_RUNNING_NAME if inst.is_running else STYLE_DEL_NAME, identity.instance)]
            if inst.is_running:
                row.append(("", "  "))
                row.append(RUNNING_HINT)
            entries.append(PickerEntry(
                display=row,
                preview=_deferred_preview(inst),       # deferred, as in the main picker
                preview_quick=_deferred_preview(inst, quick=True),
                value=identity,
                # Deleting a live container's state dir (bind-mounted rw) could
                # corrupt the running session — information-only here too.
                selectable=not inst.is_running,
                deletable=not inst.is_running,
                modifiable=not inst.is_running,
            ))
        entries.append(PickerEntry(
            display=PickerRowMarker.BACK.fragments(f"  {BACK_LABEL}"),
            preview=BACK_PREVIEW,
            value=None,
            deletable=False,
        ))

        action, value = pick_with_preview(TITLE_DELETE_MENU, entries, allow_delete=True, legend_text=legend_text)
        if action is None or value is None:
            return
        if confirm_dialog(CONFIRM_DELETE_FMT.format(name=value.instance)):
            delete_instance(value)
